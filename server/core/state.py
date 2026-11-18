"""Estado central y estructuras mutables de Madeline aisladas por origen."""

import asyncio
import time
from typing import Any, Dict, List, Optional

from core.models import AccionLocalReciente, TurnoConversacion


def _calcular_edad(timestamp: float) -> float:
    """Calcula la edad en segundos soportando tanto time.monotonic() como time.time()."""
    if timestamp > 1_000_000_000:
        return max(0.0, time.time() - timestamp)
    return max(0.0, time.monotonic() - timestamp)


historiales_por_origen: Dict[str, List[TurnoConversacion]] = {
    "pc": [],
    "telefono": [],
}
MAX_TURNOS = 1

acciones_locales_recientes: Dict[str, Optional[AccionLocalReciente]] = {
    "pc": None,
    "telefono": None,
}

_LOCKS_ORIGEN = {
    "pc": asyncio.Lock(),
    "telefono": asyncio.Lock(),
}


def obtener_lock_origen(origen: str) -> asyncio.Lock:
    """Devuelve el lock de asyncio para serializar solicitudes del origen dado.
    Nota de concurrencia: Protege la secuencia de corrutinas en el event loop.
    Si una solicitud es cancelada, sale de 'async with' y libera este lock,
    aunque un hilo de trabajo previo (e.g. en to_thread) siga ejecutando."""
    if origen not in _LOCKS_ORIGEN:
        _LOCKS_ORIGEN[origen] = asyncio.Lock()
    return _LOCKS_ORIGEN[origen]


class HistorialChatCompat(list):
    """Proyección de compatibilidad de historiales_por_origen['pc'] en formato lista de dicts.
    Permite que los tests que llaman a j.historial_chat.clear() o inspeccionan j.historial_chat
    sigan funcionando sin alteraciones."""

    def clear(self):
        super().clear()
        historiales_por_origen["pc"].clear()
        acciones_locales_recientes["pc"] = None
        memoria_contexto.fijar_ultimo_resultado(None, "pc")

    def sincronizar_desde_turnos(self, turnos: list):
        super().clear()
        for t in turnos:
            super().append({"role": "user", "content": t.usuario})
            super().append({"role": "assistant", "content": t.asistente})


historial_chat = HistorialChatCompat()
MAX_HISTORIAL = 3


class MemoriaContextoDict(dict):
    """Diccionario de compatibilidad para memoria_contexto que aísla confirmaciones
    pendientes y últimos resultados por origen ('pc', 'telefono') y aplica un TTL monótono
    de 30.0 segundos a las confirmaciones.

    Nota de diseño sobre multiorigen:
    El acceso por interfaz de diccionario (__getitem__, __setitem__, get) está
    reservado como adaptador de compatibilidad legacy para el cliente local de escritorio ('pc').
    Para interacciones concurrentes multiorigen (ej. 'telefono'), los consumidores deben
    utilizar explícitamente los métodos:
    - obtener_confirmacion(origen) / fijar_confirmacion(datos, origen)
    - obtener_ultimo_resultado(origen) / fijar_ultimo_resultado(resultado, origen)
    Esto garantiza un aislamiento estricto y previene la fuga de estado entre canales."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._confirmaciones = {
            "pc": None,
            "telefono": None,
        }
        self._ultimos_resultados = {
            "pc": None,
            "telefono": None,
        }

    def clear(self):
        super().clear()
        self._confirmaciones = {
            "pc": None,
            "telefono": None,
        }
        self._ultimos_resultados = {
            "pc": None,
            "telefono": None,
        }
        super().__setitem__("cancion", None)
        super().__setitem__("ultimo_resultado", None)
        super().__setitem__("confirmacion_pendiente", None)

    def obtener_confirmacion(self, origen: str = "pc") -> Optional[dict]:
        conf = self._confirmaciones.get(origen)
        if not conf:
            return None
        # Validar TTL monótono de 30.0 segundos
        if time.monotonic() - conf.get("timestamp", 0.0) > 30.0:
            self._confirmaciones[origen] = None
            if origen == "pc":
                super().__setitem__("confirmacion_pendiente", None)
            return None
        return conf.get("data")

    def fijar_confirmacion(self, datos: Optional[dict], origen: str = "pc"):
        if datos is None:
            self._confirmaciones[origen] = None
            if origen == "pc":
                super().__setitem__("confirmacion_pendiente", None)
        else:
            self._confirmaciones[origen] = {
                "data": datos,
                "timestamp": time.monotonic(),
            }
            if origen == "pc":
                super().__setitem__("confirmacion_pendiente", datos)

    def obtener_ultimo_resultado(self, origen: str = "pc") -> Any:
        return self._ultimos_resultados.get(origen)

    def fijar_ultimo_resultado(self, resultado: Any, origen: str = "pc"):
        self._ultimos_resultados[origen] = resultado
        if origen == "pc":
            super().__setitem__("ultimo_resultado", resultado)

    def __getitem__(self, key):
        if key == "confirmacion_pendiente":
            return self.obtener_confirmacion("pc")
        if key == "ultimo_resultado":
            return self.obtener_ultimo_resultado("pc")
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        if key == "confirmacion_pendiente":
            self.fijar_confirmacion(value, "pc")
        elif key == "ultimo_resultado":
            self.fijar_ultimo_resultado(value, "pc")
        else:
            super().__setitem__(key, value)

    def get(self, key, default=None):
        if key == "confirmacion_pendiente":
            val = self.obtener_confirmacion("pc")
            return val if val is not None else default
        if key == "ultimo_resultado":
            val = self.obtener_ultimo_resultado("pc")
            return val if val is not None else default
        return super().get(key, default)


# Memoria contextual ligera: no se envía a Groq salvo que una pregunta
# realmente necesite contexto.
memoria_contexto = MemoriaContextoDict({
    "cancion": None,
    "ultimo_resultado": None,
    # Acción destructiva esperando un "sí"/"no" del usuario antes de
    # ejecutarse (ver sección de energía en router_local). None = no
    # hay nada pendiente.
    "confirmacion_pendiente": None,
})
