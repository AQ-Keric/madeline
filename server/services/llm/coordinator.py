"""Coordinador de concurrencia, prioridad y ciclo de vida de slots para Ollama.

Implementa exclusión mutua estricta entre corutinas asyncio (inferencia conversacional)
e hilos síncronos (futuro cableado de embeddings en U-4.3C), utilizando una máquina
de estados explícita de 5 estados para evitar adquisiciones huérfanas y liberaciones
prematuras durante la cancelación.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import Enum
import threading
import time
from typing import AsyncIterator, Callable, Iterator, Optional

from .exceptions import LLMProviderError, LLMTimeoutError


class EstadoTicket(str, Enum):
    """Estados posibles del ticket de acceso al slot de inferencia local."""

    WAITING = "WAITING"
    ACQUIRED = "ACQUIRED"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    RELEASED = "RELEASED"


class EstadoEndpoint(str, Enum):
    """Estado de disponibilidad compartido para el endpoint de Ollama."""

    DISPONIBLE = "DISPONIBLE"
    DEGRADADO = "DEGRADADO"
    NO_DISPONIBLE = "NO_DISPONIBLE"


@dataclass
class SlotTicket:
    """Ticket nominal e inmutable en identidad para rastrear la titularidad del slot."""

    id: int
    tipo: str  # "conversacion" | "embedding" | "mantenimiento"
    estado: EstadoTicket = EstadoTicket.WAITING
    adquirido: bool = False
    cancelado: bool = False
    cancelacion_solicitada: bool = False


class OllamaCoordinator:
    """Monitor híbrido (sync/async) sin RLock para serializar inferencias locales en CPU.

    Principios de diseño:
    - Usa threading.Lock anónimo + threading.Condition (nunca threading.RLock, que
      permite reentradas indebidas entre corutinas ejecutándose en el mismo hilo).
    - Las corutinas esperan el slot mediante asyncio.to_thread sin bloquear el event loop.
    - La prioridad conversacional hace que los embeddings cedan el paso cuando hay una
      solicitud conversacional activa o esperando.
    - El estado del endpoint (DISPONIBLE / DEGRADADO / NO_DISPONIBLE) está desacoplado
      del bloqueo del ticket para no dejar hilos atrapados indefinidamente.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._titular_id: Optional[int] = None
        self._ticket_actual: Optional[SlotTicket] = None
        self._esperando_conversacion: int = 0
        self._seq: int = 0
        self._estado_endpoint: EstadoEndpoint = EstadoEndpoint.DISPONIBLE
        self._motivo_estado: str = ""

    # ------------------------------------------------------------------
    # Gestión de disponibilidad compartida del endpoint
    # ------------------------------------------------------------------

    @property
    def estado_endpoint(self) -> EstadoEndpoint:
        """Retorna el estado actual de salud/aislamiento del endpoint."""
        with self._lock:
            return self._estado_endpoint

    @property
    def motivo_estado(self) -> str:
        """Retorna el detalle descriptivo del último cambio de estado."""
        with self._lock:
            return self._motivo_estado

    def esta_disponible(self) -> bool:
        """True únicamente si el endpoint se encuentra en estado DISPONIBLE."""
        with self._lock:
            return self._estado_endpoint == EstadoEndpoint.DISPONIBLE

    def marcar_disponible(self, motivo: str = "") -> None:
        """Marca el endpoint como DISPONIBLE y notifica a posibles observadores."""
        with self._lock:
            self._estado_endpoint = EstadoEndpoint.DISPONIBLE
            self._motivo_estado = motivo
            self._cond.notify_all()

    def marcar_degradado(self, motivo: str) -> None:
        """Marca el endpoint como DEGRADADO y despierta waiters pendientes."""
        with self._lock:
            self._estado_endpoint = EstadoEndpoint.DEGRADADO
            self._motivo_estado = motivo
            self._cond.notify_all()

    def marcar_no_disponible(self, motivo: str) -> None:
        """Marca el endpoint como NO_DISPONIBLE (fail-closed) y despierta waiters."""
        with self._lock:
            self._estado_endpoint = EstadoEndpoint.NO_DISPONIBLE
            self._motivo_estado = motivo
            self._cond.notify_all()

    def tiene_consumidor_activo(self) -> bool:
        """True si actualmente existe un ticket poseyendo el slot."""
        with self._lock:
            return self._titular_id is not None

    def estado_actual_snapshot(self) -> dict[str, object]:
        """Devuelve una instantánea thread-safe útil para diagnóstico y tests."""
        with self._lock:
            return {
                "titular_id": self._titular_id,
                "ticket_estado": self._ticket_actual.estado.value if self._ticket_actual else None,
                "ticket_tipo": self._ticket_actual.tipo if self._ticket_actual else None,
                "esperando_conversacion": self._esperando_conversacion,
                "estado_endpoint": self._estado_endpoint.value,
                "motivo_estado": self._motivo_estado,
            }

    # ------------------------------------------------------------------
    # Ciclo interno de tickets
    # ------------------------------------------------------------------

    def _crear_ticket(self, tipo: str) -> SlotTicket:
        with self._lock:
            self._seq += 1
            return SlotTicket(id=self._seq, tipo=tipo)

    def _adquirir_sync(
        self,
        ticket: SlotTicket,
        timeout: float,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            if ticket.tipo == "conversacion":
                self._esperando_conversacion += 1
            try:
                while True:
                    if ticket.cancelado:
                        ticket.estado = EstadoTicket.RELEASED
                        self._cond.notify_all()
                        return False

                    if self._estado_endpoint != EstadoEndpoint.DISPONIBLE:
                        ticket.estado = EstadoTicket.RELEASED
                        self._cond.notify_all()
                        return False

                    disponible_slot = (
                        self._titular_id is None
                        and not (
                            ticket.tipo in ("embedding", "mantenimiento")
                            and self._esperando_conversacion > 0
                        )
                    )
                    if disponible_slot:
                        if ticket.cancelado:
                            ticket.estado = EstadoTicket.RELEASED
                            self._cond.notify_all()
                            return False
                        self._titular_id = ticket.id
                        self._ticket_actual = ticket
                        ticket.adquirido = True
                        ticket.estado = EstadoTicket.ACQUIRED
                        return True

                    restante = deadline - time.monotonic()
                    if restante <= 0:
                        ticket.estado = EstadoTicket.RELEASED
                        return False
                    # Despertares acotados para detectar cancelación de corutinas en espera
                    self._cond.wait(timeout=min(restante, 0.05))
            finally:
                if ticket.tipo == "conversacion":
                    self._esperando_conversacion = max(0, self._esperando_conversacion - 1)
                    self._cond.notify_all()

    def marcar_running(self, ticket: SlotTicket) -> None:
        """Transiciona un ticket válido de ACQUIRED a RUNNING justo al iniciar I/O."""
        with self._lock:
            if self._titular_id != ticket.id or ticket.estado != EstadoTicket.ACQUIRED:
                raise LLMProviderError(
                    "No se puede iniciar inferencia sobre un ticket sin titularidad vigente en ACQUIRED.",
                    provider="ollama",
                )
            ticket.estado = EstadoTicket.RUNNING

    def marcar_draining(self, ticket: SlotTicket) -> None:
        """Transiciona ACQUIRED o RUNNING -> DRAINING reteniendo titularidad durante limpieza."""
        with self._lock:
            if self._titular_id == ticket.id and ticket.estado in (
                EstadoTicket.ACQUIRED,
                EstadoTicket.RUNNING,
            ):
                ticket.cancelacion_solicitada = True
                ticket.estado = EstadoTicket.DRAINING

    def recuperar_disponible(
        self,
        motivo: str = "",
        verificador_seguro: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Transición atómica DEGRADADO -> DISPONIBLE sin tickets ni operaciones incompatibles."""
        with self._lock:
            if self._estado_endpoint != EstadoEndpoint.DEGRADADO:
                return False
            if (
                self._titular_id is not None
                or self._ticket_actual is not None
                or self._esperando_conversacion > 0
            ):
                return False
            if verificador_seguro is not None and not verificador_seguro():
                return False
            self._estado_endpoint = EstadoEndpoint.DISPONIBLE
            self._motivo_estado = motivo
            self._cond.notify_all()
            return True

    def _manejar_cancelacion_conversacion(self, ticket: SlotTicket) -> None:
        """Resuelve la carrera de cancelación según la fase actual del ticket."""
        with self._lock:
            if ticket.estado == EstadoTicket.WAITING:
                # Todavía en espera: impedir adquisición huérfana y despertar monitor
                ticket.cancelado = True
                ticket.estado = EstadoTicket.RELEASED
                self._cond.notify_all()
            elif ticket.estado == EstadoTicket.ACQUIRED:
                # Adquirido antes de iniciar petición HTTP: liberación inmediata segura
                ticket.cancelado = True
                ticket.estado = EstadoTicket.RELEASED
                if self._titular_id == ticket.id:
                    self._titular_id = None
                    self._ticket_actual = None
                self._cond.notify_all()
            elif ticket.estado == EstadoTicket.RUNNING:
                # Petición HTTP potencialmente ejecutándose: conservar titularidad hasta finalizar limpieza
                ticket.cancelacion_solicitada = True
                ticket.estado = EstadoTicket.DRAINING

    def liberar_slot(self, ticket: SlotTicket) -> bool:
        """Libera el slot validando estrictamente la identidad del ticket titular."""
        with self._lock:
            if ticket.estado == EstadoTicket.RELEASED:
                return False
            if self._titular_id != ticket.id:
                return False
            ticket.estado = EstadoTicket.RELEASED
            self._titular_id = None
            self._ticket_actual = None
            self._cond.notify_all()
            return True

    def _finalizar_contexto_conversacion(self, ticket: SlotTicket) -> None:
        with self._lock:
            if ticket.estado == EstadoTicket.WAITING and not ticket.adquirido:
                ticket.cancelado = True
                ticket.estado = EstadoTicket.RELEASED
                self._cond.notify_all()
                return
            if self._titular_id == ticket.id and ticket.estado != EstadoTicket.RELEASED:
                ticket.estado = EstadoTicket.RELEASED
                self._titular_id = None
                self._ticket_actual = None
                self._cond.notify_all()

    # ------------------------------------------------------------------
    # Interfaces públicas (Async Conversacional y Sync Embeddings)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def slot_conversacion(self, timeout: float = 5.0) -> AsyncIterator[SlotTicket]:
        """Adquiere de forma no bloqueante el slot prioritario para inferencia conversacional."""
        with self._lock:
            if self._estado_endpoint != EstadoEndpoint.DISPONIBLE:
                raise LLMProviderError(
                    f"Endpoint Ollama no disponible ({self._estado_endpoint.value}): {self._motivo_estado}",
                    provider="ollama",
                )

        ticket = self._crear_ticket("conversacion")
        try:
            adquirido = await asyncio.to_thread(self._adquirir_sync, ticket, timeout)
            if not adquirido:
                with self._lock:
                    if self._estado_endpoint != EstadoEndpoint.DISPONIBLE:
                        raise LLMProviderError(
                            f"Endpoint Ollama no disponible ({self._estado_endpoint.value}): {self._motivo_estado}",
                            provider="ollama",
                        )
                raise LLMTimeoutError(
                    "Tiempo de espera agotado intentando adquirir slot conversacional en Ollama.",
                    provider="ollama",
                )
            yield ticket
        except asyncio.CancelledError:
            self._manejar_cancelacion_conversacion(ticket)
            raise
        finally:
            self._finalizar_contexto_conversacion(ticket)

    def adquirir_slot_embedding(self, timeout: float = 0.2) -> Optional[SlotTicket]:
        """Intenta adquirir síncronamente un slot secundario para embedding con timeout acotado."""
        if not self.esta_disponible():
            return None
        ticket = self._crear_ticket("embedding")
        if self._adquirir_sync(ticket, timeout):
            return ticket
        return None

    @contextmanager
    def slot_embedding(self, timeout: float = 0.2) -> Iterator[Optional[SlotTicket]]:
        """Context manager síncrono que cede inmediatamente si el slot está ocupado o no disponible."""
        ticket = self.adquirir_slot_embedding(timeout=timeout)
        try:
            yield ticket
        finally:
            if ticket is not None:
                self.liberar_slot(ticket)

    @asynccontextmanager
    async def slot_mantenimiento(self, timeout: float = 0.0) -> AsyncIterator[SlotTicket]:
        """Adquiere exclusividad para operaciones de mantenimiento (ej. descarga de modelo).

        Rechaza inmediatamente con LLMProviderError si existe una inferencia activa,
        una solicitud conversacional esperando o el endpoint no está DISPONIBLE.
        """
        with self._lock:
            if self._estado_endpoint != EstadoEndpoint.DISPONIBLE:
                raise LLMProviderError(
                    f"Endpoint Ollama no disponible ({self._estado_endpoint.value}): {self._motivo_estado}",
                    provider="ollama",
                )
        ticket = self._crear_ticket("mantenimiento")
        try:
            adquirido = await asyncio.to_thread(self._adquirir_sync, ticket, timeout)
            if not adquirido:
                raise LLMProviderError(
                    "Operación rechazada: existe una inferencia o consumidor activo en curso.",
                    provider="ollama",
                )
            yield ticket
        except asyncio.CancelledError:
            self._manejar_cancelacion_conversacion(ticket)
            raise
        finally:
            self._finalizar_contexto_conversacion(ticket)
