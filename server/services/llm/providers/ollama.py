"""Adaptador para el proveedor local Ollama (capacidad complementaria CPU-only).

Implementa LLMProvider comunicándose vía HTTP (/api/chat, /api/ps, /api/tags) con una
instancia aislada mediante systemd --user (OllamaServerManager) o con un endpoint
externo explícitamente configurado, aplicando verificación fail-closed de GPU/VRAM,
streaming cancelable con ResourceManager y coordinación estricta de slots.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any, Optional, Union
from urllib.parse import urlparse

import httpx

from ..base import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMRole,
    LLMToolCall,
    LLMToolChoice,
    LLMToolChoiceMode,
    LLMToolDefinition,
)
from ..coordinator import EstadoEndpoint, OllamaCoordinator
from ..exceptions import (
    LLMConfigurationError,
    LLMConnectionError,
    LLMError,
    LLMMalformedResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMValidationError,
)
from .ollama_lifecycle import (
    DEFAULT_OLLAMA_HOST,
    DEFAULT_OLLAMA_KEEP_ALIVE,
    DEFAULT_OLLAMA_MANAGED_PORT,
    OllamaServerManager,
    validar_keep_alive,
)

DEFAULT_OLLAMA_MODEL = os.getenv("MADELINE_OLLAMA_MODEL", "qwen2.5:3b")
DEFAULT_OLLAMA_TIMEOUT = float(os.getenv("MADELINE_OLLAMA_TIMEOUT", "60.0"))
DEFAULT_OLLAMA_NUM_THREAD = int(os.getenv("MADELINE_OLLAMA_NUM_THREAD", "3"))


def _normalizar_nombre_modelo(nombre: str) -> str:
    """Normaliza el nombre del modelo para comparar etiquetas `:latest` en /api/ps."""
    limpio = nombre.strip()
    if ":" not in limpio:
        return f"{limpio}:latest"
    return limpio


def _mapear_error_httpx(exc: Exception) -> LLMError:
    """Mapea excepciones de httpx/transporte a excepciones canónicas del dominio LLM."""
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return LLMTimeoutError(str(exc), provider="ollama")
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return LLMConnectionError(str(exc), provider="ollama")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return LLMRateLimitError(str(exc), provider="ollama")
        return LLMProviderError(f"Error HTTP {status} de Ollama: {exc}", provider="ollama")
    if isinstance(exc, httpx.HTTPError):
        return LLMProviderError(f"Error de transporte HTTP con Ollama: {exc}", provider="ollama")
    return LLMProviderError(f"Error inesperado en proveedor Ollama: {exc}", provider="ollama")


class OllamaProvider(LLMProvider):
    """Adaptador LLMProvider para inferencia local CPU-only mediante Ollama."""

    def __init__(
        self,
        default_model: str = DEFAULT_OLLAMA_MODEL,
        timeout: float = DEFAULT_OLLAMA_TIMEOUT,
        keep_alive: Union[int, float, str] = DEFAULT_OLLAMA_KEEP_ALIVE,
        num_thread: int = DEFAULT_OLLAMA_NUM_THREAD,
        base_url: Optional[str] = None,
        modo_gestionado: bool = True,
        server_manager: Optional[OllamaServerManager] = None,
        coordinator: Optional[OllamaCoordinator] = None,
        resource_manager: Optional[Any] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        intervalo_continuidad_s: float = 0.5,
        coordinator_timeout_s: float = 5.0,
    ) -> None:
        if num_thread <= 0:
            raise LLMConfigurationError("num_thread debe ser un entero positivo.", provider="ollama")
        if timeout <= 0:
            raise LLMConfigurationError("timeout debe ser un número positivo.", provider="ollama")
        keep_alive_validado = validar_keep_alive(keep_alive)

        self._default_model = default_model
        self._timeout = float(timeout)
        self._keep_alive = keep_alive_validado
        self._num_thread = int(num_thread)
        self._coordinator = coordinator or (
            server_manager.coordinator if server_manager is not None else OllamaCoordinator()
        )
        self._resource_manager = resource_manager
        self._external_client = http_client
        self._owned_client: Optional[httpx.AsyncClient] = None
        self._intervalo_continuidad_s = max(0.0, float(intervalo_continuidad_s))
        self._coordinator_timeout_s = float(coordinator_timeout_s)
        self._ops_lock = threading.Lock()
        self._tareas_arranque: set[asyncio.Task[Any]] = set()
        self._tareas_drenaje: set[asyncio.Task[Any]] = set()
        self._operaciones_arranque: set[asyncio.Future[Any]] = set()
        self._cerrando: bool = False
        self._ultimo_ps_verificado: bool = False

        if server_manager is not None:
            self._server_manager = server_manager
        else:
            host = DEFAULT_OLLAMA_HOST
            port = DEFAULT_OLLAMA_MANAGED_PORT
            if base_url is not None:
                parsed = urlparse(base_url)
                if not parsed.hostname or not parsed.port:
                    raise LLMConfigurationError(
                        f"base_url inválida para Ollama: '{base_url}'. Debe incluir host y puerto.",
                        provider="ollama",
                    )
                host = parsed.hostname
                port = parsed.port
            self._server_manager = OllamaServerManager(
                coordinator=self._coordinator,
                modo_gestionado=modo_gestionado,
                host=host,
                port=port,
                keep_alive=keep_alive,
            )

    @property
    def nombre(self) -> str:
        return "ollama"

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def base_url(self) -> str:
        return self._server_manager.base_url

    @property
    def coordinator(self) -> OllamaCoordinator:
        return self._coordinator

    @property
    def server_manager(self) -> OllamaServerManager:
        return self._server_manager

    @property
    def ultima_telemetria_gpu(self) -> str:
        """Retorna el último estado observado de nvidia-smi ('DESCONOCIDA', 'SIN_PROCESOS_GPU', 'VIOLACION_GPU')."""
        return self._server_manager.nvidia_telemetria_estado

    @property
    def cpu_only_verificado(self) -> bool:
        """True únicamente si /api/ps confirmó size_vram=0 y nvidia-smi confirmó ausencia de procesos GPU."""
        return (
            self._ultimo_ps_verificado
            and self._server_manager.nvidia_telemetria_estado == "SIN_PROCESOS_GPU"
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None or self._owned_client.is_closed:
            self._owned_client = httpx.AsyncClient(
                base_url=self._server_manager.base_url,
                timeout=self._timeout,
            )
        return self._owned_client

    def _convertir_mensajes(self, mensajes: list[LLMMessage]) -> list[dict[str, Any]]:
        """Convierte mensajes del dominio neutral al esquema /api/chat de Ollama."""
        ollama_messages: list[dict[str, Any]] = []
        for msg in mensajes:
            rol = msg.rol.value if isinstance(msg.rol, LLMRole) else str(msg.rol)
            if rol in (LLMRole.SYSTEM.value, LLMRole.USER.value):
                ollama_messages.append({"role": rol, "content": msg.contenido or ""})
            elif rol == LLMRole.ASSISTANT.value:
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.contenido if msg.contenido is not None else "",
                }
                if msg.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.nombre,
                                "arguments": tc.argumentos,
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                ollama_messages.append(entry)
            elif rol == LLMRole.TOOL.value:
                entry = {
                    "role": "tool",
                    "content": msg.contenido or "",
                }
                if msg.nombre:
                    entry["tool_name"] = msg.nombre
                ollama_messages.append(entry)
        return ollama_messages

    def _convertir_tools(self, tools: list[LLMToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.nombre,
                    "description": t.descripcion,
                    "parameters": t.parametros,
                },
            }
            for t in tools
        ]

    def _construir_payload(self, solicitud: LLMRequest) -> dict[str, Any]:
        """Construye el payload /api/chat CPU-only.

        Limitación de Ollama: `/api/chat` no soporta modos de selección forzada
        `REQUIRED` ni `SPECIFIC`; dichos modos se rechazan explícitamente con
        LLMValidationError para no degradar silenciosamente la intención del llamador.
        """
        if solicitud.tool_choice is not None and isinstance(solicitud.tool_choice, LLMToolChoice):
            if solicitud.tool_choice.modo in (LLMToolChoiceMode.REQUIRED, LLMToolChoiceMode.SPECIFIC):
                raise LLMValidationError(
                    f"Ollama no soporta tool_choice en modo '{solicitud.tool_choice.modo.value}'; "
                    "únicamente admite 'auto' y 'none'.",
                    provider="ollama",
                )

        target_model = solicitud.model or self._default_model
        options: dict[str, Any] = {
            "num_gpu": 0,  # Aislamiento CPU-only obligatorio
            "num_thread": self._num_thread,
        }
        if solicitud.temperature is not None:
            options["temperature"] = solicitud.temperature
        if solicitud.max_tokens is not None:
            options["num_predict"] = solicitud.max_tokens

        payload: dict[str, Any] = {
            "model": target_model,
            "messages": self._convertir_mensajes(solicitud.mensajes),
            "stream": True,
            "keep_alive": self._keep_alive,
            "options": options,
        }

        if solicitud.tools:
            ocultar_tools = (
                solicitud.tool_choice is not None
                and isinstance(solicitud.tool_choice, LLMToolChoice)
                and solicitud.tool_choice.modo == LLMToolChoiceMode.NONE
            )
            if not ocultar_tools:
                payload["tools"] = self._convertir_tools(solicitud.tools)

        return payload

    def _validar_ps_payload(
        self,
        data: Any,
        target_model: str,
        exigir_modelo_presente: bool,
    ) -> None:
        """Valida la respuesta de /api/ps aplicando criterio estricto fail-closed."""
        if not isinstance(data, dict):
            raise LLMProviderError(
                "Aislamiento GPU no verificable: /api/ps devolvió payload no JSON objeto.",
                provider="ollama",
            )
        models = data.get("models")
        if not isinstance(models, list):
            raise LLMProviderError(
                "Aislamiento GPU no verificable: /api/ps no contiene lista 'models'.",
                provider="ollama",
            )

        objetivo_norm = _normalizar_nombre_modelo(target_model)
        encontrado = None
        for entry in models:
            if not isinstance(entry, dict):
                continue
            nombre_entry = entry.get("model") or entry.get("name") or ""
            if not isinstance(nombre_entry, str):
                continue
            if _normalizar_nombre_modelo(nombre_entry) == objetivo_norm or nombre_entry == target_model:
                encontrado = entry
                break

        if encontrado is None:
            if exigir_modelo_presente:
                raise LLMProviderError(
                    f"Aislamiento GPU no verificable: el modelo '{target_model}' no figura en /api/ps durante la inferencia.",
                    provider="ollama",
                )
            return

        size_vram = encontrado.get("size_vram")
        if isinstance(size_vram, bool) or not isinstance(size_vram, int):
            raise LLMProviderError(
                f"Aislamiento GPU no verificable: campo 'size_vram' inválido ({size_vram!r}) en /api/ps.",
                provider="ollama",
            )
        if size_vram != 0:
            raise LLMProviderError(
                f"Violación de aislamiento GPU: el modelo '{target_model}' reportó size_vram={size_vram} bytes en /api/ps.",
                provider="ollama",
            )

    async def _consultar_ps(
        self,
        client: httpx.AsyncClient,
        target_model: str,
        exigir_modelo_presente: bool,
        timeout_s: float = 2.0,
    ) -> None:
        try:
            resp = await client.get("/api/ps", timeout=timeout_s)
        except Exception as exc:
            mapped = _mapear_error_httpx(exc)
            raise LLMProviderError(
                f"Aislamiento GPU no verificable (/api/ps no respondió): {mapped}",
                provider="ollama",
            ) from exc

        if resp.status_code >= 400:
            raise LLMProviderError(
                f"Aislamiento GPU no verificable (/api/ps HTTP {resp.status_code}).",
                provider="ollama",
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise LLMProviderError(
                f"Aislamiento GPU no verificable (/api/ps JSON inválido): {exc}",
                provider="ollama",
            ) from exc

        self._validar_ps_payload(
            data=data,
            target_model=target_model,
            exigir_modelo_presente=exigir_modelo_presente,
        )

    def _normalizar_tool_calls(self, raw_tool_calls: list[Any]) -> list[LLMToolCall]:
        tool_calls: list[LLMToolCall] = []
        for tc in raw_tool_calls:
            if not isinstance(tc, dict):
                raise LLMMalformedResponseError(
                    f"Elemento tool_call debe ser dict, recibido: {type(tc).__name__}.",
                    provider="ollama",
                )
            tc_id = tc.get("id")
            if not isinstance(tc_id, str) or not tc_id.strip():
                raise LLMMalformedResponseError(
                    "Tool call recibido sin identificador ('id') válido.",
                    provider="ollama",
                )
            if tc_id != tc_id.strip():
                raise LLMMalformedResponseError(
                    f"El identificador del tool call ('{tc_id}') no debe contener espacios al inicio o al final.",
                    provider="ollama",
                )

            fn = tc.get("function")
            if isinstance(fn, dict):
                fn_name = fn.get("name")
                fn_args = fn.get("arguments")
            else:
                fn_name = tc.get("name")
                fn_args = tc.get("arguments")

            if not isinstance(fn_name, str) or not fn_name.strip():
                raise LLMMalformedResponseError(
                    "Tool call recibido sin nombre de herramienta válido.",
                    provider="ollama",
                )
            if fn_name != fn_name.strip():
                raise LLMMalformedResponseError(
                    f"El nombre del tool call ('{fn_name}') no debe contener espacios al inicio o al final.",
                    provider="ollama",
                )
            if fn_args is None:
                raise LLMMalformedResponseError(
                    f"Tool call '{fn_name}' recibido sin argumentos ('arguments' es None).",
                    provider="ollama",
                )

            try:
                tool_call = LLMToolCall.from_raw(
                    id=tc_id,
                    nombre=fn_name,
                    raw_arguments=fn_args,
                    provider="ollama",
                )
            except LLMValidationError as val_err:
                raise LLMMalformedResponseError(
                    f"Argumentos malformados en tool call '{fn_name}': {val_err.mensaje}",
                    provider="ollama",
                ) from val_err
            tool_calls.append(tool_call)
        return tool_calls

    def _construir_llm_response(
        self,
        texto_chunks: list[str],
        hubo_texto_str: bool,
        hubo_content_invalido: Any,
        raw_tool_calls: list[Any],
        finish_reason_raw: Any,
        modelo: str,
    ) -> LLMResponse:
        if hubo_content_invalido is not None:
            raise LLMMalformedResponseError(
                f"El contenido del mensaje ('content') debe ser str o None, recibido: {type(hubo_content_invalido).__name__}.",
                provider="ollama",
            )

        texto: Optional[str] = "".join(texto_chunks) if hubo_texto_str else None
        tool_calls = self._normalizar_tool_calls(raw_tool_calls) if raw_tool_calls else []

        if finish_reason_raw is not None:
            if not isinstance(finish_reason_raw, str) or not finish_reason_raw.strip():
                raise LLMMalformedResponseError(
                    f"El motivo de finalización ('finish_reason') debe ser una cadena no vacía o None, recibido: {type(finish_reason_raw).__name__}.",
                    provider="ollama",
                )
            if finish_reason_raw != finish_reason_raw.strip():
                raise LLMMalformedResponseError(
                    f"El motivo de finalización ('{finish_reason_raw}') no debe contener espacios al inicio o al final.",
                    provider="ollama",
                )
            finish_reason: Optional[str] = finish_reason_raw
        else:
            finish_reason = None

        if texto is None and not tool_calls:
            raise LLMMalformedResponseError(
                "La respuesta de Ollama no contiene contenido textual ('content') ni llamadas a herramientas ('tool_calls') válidas.",
                provider="ollama",
            )

        return LLMResponse(
            texto=texto,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            modelo=modelo,
            proveedor="ollama",
        )

    def _evaluar_admision_recursos(self) -> None:
        if self._resource_manager is None:
            return
        decision = self._resource_manager.evaluar_admision()
        if not decision.admitida:
            raise LLMProviderError(
                f"Inferencia local denegada por ResourceManager ({decision.motivo.value}): {decision.detalle}",
                provider="ollama",
            )

    def _evaluar_continuidad_recursos(self) -> None:
        if self._resource_manager is None:
            return
        decision = self._resource_manager.evaluar_continuidad()
        if decision.debe_cancelar:
            raise LLMProviderError(
                f"Inferencia local cancelada por ResourceManager ({decision.motivo.value}): {decision.detalle}",
                provider="ollama",
            )

    @staticmethod
    def _consumir_excepcion_task(task: asyncio.Task[Any]) -> None:
        """Recupera cualquier excepción pendiente para evitar 'Task exception was never retrieved'."""
        if task.done() and not task.cancelled():
            try:
                task.exception()
            except Exception:
                pass

    def _registrar_operacion(self, registro: set[Any], item: Any) -> None:
        with self._ops_lock:
            registro.add(item)

    def _descartar_operacion(self, registro: set[Any], item: Any) -> None:
        with self._ops_lock:
            registro.discard(item)

    def _tiene_operaciones_pendientes(self) -> bool:
        with self._ops_lock:
            return bool(self._operaciones_arranque or self._tareas_arranque or self._tareas_drenaje)

    async def _esperar_tarea_sin_cancelar(
        self,
        task: asyncio.Task[Any],
        registro: set[asyncio.Task[Any]],
    ) -> bool:
        """Espera `task` reteniendo referencia fuerte y absorbiendo CancelledError repetidos."""
        self._registrar_operacion(registro, task)
        task.add_done_callback(lambda t: self._descartar_operacion(registro, t))
        cancelacion_recibida = False
        try:
            while not task.done():
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    cancelacion_recibida = True
            return cancelacion_recibida
        finally:
            if task.done():
                self._descartar_operacion(registro, task)

    async def _detener_y_revalidar_protegido(self, *, restaurar_disponible: bool = True) -> bool:
        """Ejecuta la detención gestionada protegiéndola ante cancelaciones repetidas.

        No usa asyncio.shield() superficialmente: mantiene una referencia fuerte en
        `self._tareas_drenaje`, soporta múltiples CancelledError sobre la corutina
        solicitante sin cancelar `stop_task`, no bloquea el event loop y solo restaura
        DISPONIBLE tras confirmar limpieza completa y revalidación (si corresponde).
        """
        stop_task: asyncio.Task[bool] = asyncio.create_task(
            asyncio.to_thread(self._server_manager.detener)
        )
        cancelacion_recibida = False
        try:
            cancelacion_recibida = await self._esperar_tarea_sin_cancelar(
                stop_task,
                self._tareas_drenaje,
            )

            limpio = stop_task.result()
            revalidado = False
            if limpio:
                revalidado = await asyncio.to_thread(
                    self._server_manager.revalidar_limpieza_tras_detencion
                )

            if limpio and revalidado and restaurar_disponible:
                self._coordinator.marcar_disponible(
                    "Unidad gestionada detenida, cgroup vacío y limpieza verificada tras drenaje."
                )
            elif not (limpio and revalidado):
                if self._coordinator.esta_disponible():
                    self._coordinator.marcar_degradado(
                        "Limpieza de unidad gestionada incompleta o no revalidada tras drenaje."
                    )

            if cancelacion_recibida:
                raise asyncio.CancelledError()
            return bool(limpio and revalidado)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if not stop_task.done():
                stop_task.add_done_callback(self._consumir_excepcion_task)
            else:
                self._consumir_excepcion_task(stop_task)
            if self._coordinator.esta_disponible():
                self._coordinator.marcar_degradado(f"Error durante drenaje protegido: {exc}")
            if cancelacion_recibida:
                raise asyncio.CancelledError() from exc
            raise

    async def _asegurar_servidor_protegido(self, ticket: Any) -> None:
        """Ejecuta el arranque gestionado sin dejar hilos ni unidades huérfanos ante cancelación."""
        op_done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._registrar_operacion(self._operaciones_arranque, op_done)
        start_task: asyncio.Task[None] = asyncio.create_task(
            asyncio.to_thread(self._server_manager.asegurar_servidor_activo)
        )
        cancelacion_recibida = False
        try:
            cancelacion_recibida = await self._esperar_tarea_sin_cancelar(
                start_task,
                self._tareas_arranque,
            )
            exc = start_task.exception() if not start_task.cancelled() else None

            if not cancelacion_recibida:
                if start_task.cancelled():
                    raise asyncio.CancelledError()
                if exc is not None:
                    raise exc
                return

            # Cancelación en fase ACQUIRED: retener ticket en DRAINING y resolver limpieza antes de liberar.
            self._coordinator.marcar_draining(ticket)
            if self._server_manager.modo_gestionado:
                if exc is not None:
                    estado_actual = self._coordinator.estado_endpoint
                    if estado_actual == EstadoEndpoint.DISPONIBLE:
                        self._coordinator.marcar_degradado(
                            f"Arranque cancelado con error pendiente: {exc}"
                        )
                    if (
                        self._server_manager.unit_name is not None
                        and estado_actual != EstadoEndpoint.NO_DISPONIBLE
                    ):
                        propia_task = asyncio.create_task(
                            asyncio.to_thread(self._server_manager.verificar_propiedad_unidad_activa)
                        )
                        await self._esperar_tarea_sin_cancelar(propia_task, self._tareas_drenaje)
                        propia, razon_propiedad = propia_task.result()
                        if propia:
                            await self._detener_y_revalidar_protegido(restaurar_disponible=False)
                        else:
                            self._coordinator.marcar_degradado(
                                f"Unidad '{self._server_manager.unit_name}' sin propiedad verificable "
                                f"tras cancelación ({razon_propiedad}); se omite detención especulativa."
                            )
                elif self._server_manager.unit_name is not None:
                    self._coordinator.marcar_degradado(
                        "Cancelación durante arranque en ACQUIRED; ejecutando limpieza verificada."
                    )
                    await self._detener_y_revalidar_protegido(restaurar_disponible=True)

            if exc is not None:
                raise asyncio.CancelledError() from exc
            raise asyncio.CancelledError()
        except BaseException:
            if not start_task.done():
                start_task.add_done_callback(self._consumir_excepcion_task)
            else:
                self._consumir_excepcion_task(start_task)
            raise
        finally:
            self._descartar_operacion(self._operaciones_arranque, op_done)
            if not op_done.done():
                op_done.set_result(None)

    async def generar_respuesta(
        self,
        solicitud: LLMRequest,
    ) -> LLMResponse:
        """Genera una respuesta CPU-only vía /api/chat en streaming verificando aislamiento."""
        self._evaluar_admision_recursos()
        target_model = solicitud.model or self._default_model
        req_timeout = float(solicitud.timeout) if solicitud.timeout is not None else self._timeout
        payload = self._construir_payload(solicitud)
        client = self._get_client()
        self._ultimo_ps_verificado = False

        async with self._coordinator.slot_conversacion(timeout=self._coordinator_timeout_s) as ticket:
            # Fase ACQUIRED: arranque y pre-chequeos protegidos antes de abrir transporte /api/chat
            await self._asegurar_servidor_protegido(ticket)
            if self._cerrando:
                raise LLMProviderError(
                    "Proveedor Ollama en cierre ordenado.",
                    provider="ollama",
                )
            try:
                await asyncio.to_thread(self._server_manager.verificar_nvidia_smi_sin_procesos)
            except LLMProviderError as gpu_err:
                if self._server_manager.nvidia_telemetria_estado == "VIOLACION_GPU":
                    self._ultimo_ps_verificado = False
                    self._coordinator.marcar_draining(ticket)
                    self._coordinator.marcar_degradado(
                        "Violación de aislamiento GPU detectada antes de iniciar inferencia."
                    )
                    if self._server_manager.modo_gestionado:
                        try:
                            await self._detener_y_revalidar_protegido(restaurar_disponible=False)
                        except asyncio.CancelledError:
                            raise gpu_err
                raise

            if not self._server_manager.modo_gestionado:
                # Pre-chequeo en servidor externo antes de enviar prompt
                await self._consultar_ps(client, target_model, exigir_modelo_presente=False, timeout_s=min(req_timeout, 2.0))

            # Transición formal ACQUIRED -> RUNNING antes de abrir el transporte HTTP
            self._coordinator.marcar_running(ticket)

            done_recibido = False
            error_http_limpio = False
            primer_chunk_verificado = False
            texto_chunks: list[str] = []
            hubo_texto_str = False
            hubo_content_invalido: Any = None
            raw_tool_calls: list[Any] = []
            finish_reason_raw: Any = None
            modelo_resp = target_model
            ultimo_check_continuidad = time.monotonic()

            try:
                async with client.stream(
                    "POST",
                    "/api/chat",
                    json=payload,
                    timeout=req_timeout,
                ) as response:
                    if response.status_code >= 400:
                        raw_err = await response.aread()
                        msg_err = raw_err.decode("utf-8", errors="replace").strip()
                        error_http_limpio = True
                        if response.status_code == 429:
                            raise LLMRateLimitError(f"Rate limit HTTP 429 de Ollama: {msg_err}", provider="ollama")
                        raise LLMProviderError(
                            f"Error HTTP {response.status_code} de Ollama: {msg_err}",
                            provider="ollama",
                        )

                    try:
                        async for linea in response.aiter_lines():
                            if not linea or not linea.strip():
                                continue
                            try:
                                chunk = json.loads(linea)
                            except Exception as exc:
                                raise LLMMalformedResponseError(
                                    f"Línea JSON malformada en stream de Ollama: {exc}",
                                    provider="ollama",
                                ) from exc

                            if not isinstance(chunk, dict):
                                raise LLMMalformedResponseError(
                                    f"Chunk de Ollama debe ser objeto JSON, recibido: {type(chunk).__name__}.",
                                    provider="ollama",
                                )

                            if "error" in chunk and chunk["error"]:
                                raise LLMProviderError(str(chunk["error"]), provider="ollama")

                            if not primer_chunk_verificado:
                                primer_chunk_verificado = True
                                await self._consultar_ps(
                                    client,
                                    target_model,
                                    exigir_modelo_presente=True,
                                    timeout_s=min(req_timeout, 2.0),
                                )
                                self._ultimo_ps_verificado = True
                                await asyncio.to_thread(self._server_manager.descubrir_runners)
                                await asyncio.to_thread(self._server_manager.verificar_nvidia_smi_sin_procesos)

                            ahora = time.monotonic()
                            if ahora - ultimo_check_continuidad >= self._intervalo_continuidad_s:
                                ultimo_check_continuidad = ahora
                                self._evaluar_continuidad_recursos()

                            modelo_chunk = chunk.get("model")
                            if isinstance(modelo_chunk, str) and modelo_chunk.strip():
                                modelo_resp = modelo_chunk.strip()

                            msg_obj = chunk.get("message")
                            if msg_obj is not None:
                                if not isinstance(msg_obj, dict):
                                    raise LLMMalformedResponseError(
                                        f"Atributo 'message' debe ser objeto dict, recibido: {type(msg_obj).__name__}.",
                                        provider="ollama",
                                    )
                                content = msg_obj.get("content")
                                if content is not None:
                                    if not isinstance(content, str):
                                        hubo_content_invalido = content
                                    else:
                                        hubo_texto_str = True
                                        texto_chunks.append(content)

                                chunk_tcs = msg_obj.get("tool_calls")
                                if chunk_tcs is not None:
                                    if not isinstance(chunk_tcs, list):
                                        raise LLMMalformedResponseError(
                                            f"tool_calls esperado como lista, recibido: {type(chunk_tcs).__name__}.",
                                            provider="ollama",
                                        )
                                    raw_tool_calls.extend(chunk_tcs)

                            if chunk.get("done") is True:
                                done_recibido = True
                                finish_reason_raw = chunk.get("done_reason")
                                break
                    finally:
                        await response.aclose()

                if not done_recibido:
                    raise LLMMalformedResponseError(
                        "La respuesta en streaming de Ollama cerró antes de emitir 'done: true'.",
                        provider="ollama",
                    )

                return self._construir_llm_response(
                    texto_chunks=texto_chunks,
                    hubo_texto_str=hubo_texto_str,
                    hubo_content_invalido=hubo_content_invalido,
                    raw_tool_calls=raw_tool_calls,
                    finish_reason_raw=finish_reason_raw,
                    modelo=modelo_resp,
                )
            except LLMError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                mapped = _mapear_error_httpx(exc)
                if mapped is exc:
                    raise
                raise mapped from exc
            finally:
                if not done_recibido and not error_http_limpio:
                    # Marcar DRAINING y DEGRADADO preventivamente antes de iniciar limpieza protegida
                    self._ultimo_ps_verificado = False
                    self._coordinator.marcar_draining(ticket)
                    self._coordinator.marcar_degradado(
                        "Drenaje preventivo en curso tras interrupción de inferencia RUNNING."
                    )
                    if self._server_manager.modo_gestionado:
                        restaurar = self._server_manager.nvidia_telemetria_estado != "VIOLACION_GPU"
                        await self._detener_y_revalidar_protegido(restaurar_disponible=restaurar)
                    else:
                        self._coordinator.marcar_degradado(
                            "Inferencia interrumpida en servidor externo antes de confirmar 'done: true'."
                        )

    async def analizar_vision(
        self,
        imagen_bytes: bytes,
        extension: str,
        pregunta: str,
        model: Optional[str] = None,
    ) -> str:
        """Rechaza visión en Ollama CPU-only; la visión de Madeline utiliza exclusivamente Groq."""
        raise LLMProviderError(
            "Visión local en CPU no soportada en Ollama; la visión de Madeline requiere Groq.",
            provider="ollama",
        )

    async def revalidar_proveedor_externo(self, confirmacion_usuario: bool = False) -> bool:
        """Rehabilita un servidor externo DEGRADADO únicamente tras confirmación explícita.

        Las sondas HTTP (/api/tags, /api/ps) complementan la comprobación, pero jamás
        rehabilitan por sí solas sin confirmación explícita de reinicio o cese previo.
        """
        if self._server_manager.modo_gestionado:
            return self._coordinator.esta_disponible()
        if not confirmacion_usuario:
            return False

        client = self._get_client()
        try:
            resp = await client.get("/api/tags", timeout=min(self._timeout, 2.0))
            if resp.status_code >= 400:
                return False
            await self._consultar_ps(client, self._default_model, exigir_modelo_presente=False, timeout_s=2.0)
        except Exception:
            return False

        self._coordinator.marcar_disponible(
            "Servidor externo revalidado tras confirmación explícita del operador."
        )
        return True

    async def descargar_modelo(self, model: Optional[str] = None) -> bool:
        """Descarga controlada del modelo serializada mediante el coordinador."""
        if not self._server_manager.modo_gestionado:
            raise LLMProviderError(
                "Operación rechazada: prohibido descargar modelos en un servidor Ollama externo.",
                provider="ollama",
            )
        target_model = model or self._default_model
        client = self._get_client()
        async with self._coordinator.slot_mantenimiento(timeout=0.0) as ticket:
            self._coordinator.marcar_running(ticket)
            try:
                resp = await client.post(
                    "/api/generate",
                    json={"model": target_model, "keep_alive": 0},
                    timeout=min(self._timeout, 5.0),
                )
                return resp.status_code < 400
            except Exception as exc:
                raise _mapear_error_httpx(exc) from exc

    async def recuperar_modo_gestionado(self, *, confirmar_reintento_gpu: bool = False) -> bool:
        """Recuperación explícita desde DEGRADADO verificando limpieza real y ausencia de tareas/tickets."""
        if self._cerrando or self._tiene_operaciones_pendientes():
            return False
        return await asyncio.to_thread(
            self._server_manager.recuperar_desde_degradado,
            permitir_tras_violacion_gpu=confirmar_reintento_gpu,
            verificador_operaciones=lambda: not self._cerrando and not self._tiene_operaciones_pendientes(),
        )

    async def revalidar_limpieza(self, *, confirmar_reintento_gpu: bool = False) -> bool:
        """Alias explícito para revalidación y recuperación atómica del modo gestionado."""
        return await self.recuperar_modo_gestionado(confirmar_reintento_gpu=confirmar_reintento_gpu)

    async def aclose(self) -> None:
        """Cierre ordenado esperando arranques/drenajes pendientes, cliente HTTP y unidad."""
        self._cerrando = True
        while True:
            with self._ops_lock:
                pendientes = list(
                    self._operaciones_arranque | self._tareas_arranque | self._tareas_drenaje
                )
            if not pendientes:
                break
            await asyncio.gather(*pendientes, return_exceptions=True)
        if self._owned_client is not None and not self._owned_client.is_closed:
            await self._owned_client.aclose()
            self._owned_client = None
        await self._server_manager.aclose()
