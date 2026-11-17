"""Sistema de resultados estructurados para herramientas y verificaciones en Madeline.

Cada herramienta devuelve un objeto Resultado que encapsula:
- ok: Booleano que indica si la operación fue exitosa o válida.
- mensaje: Cadena de texto legible para el usuario o síntesis de voz.
- evidencia: Diccionario con datos concretos que comprueban el resultado (IDs, pids, workspaces, etc.).
- estado: Estado semántico (SUCCESS, FAILURE, PENDING, NEEDS_CONFIRMATION).

Hereda de str para garantizar compatibilidad hacia atrás total con código y tests preexistentes
que esperan un string (ej. startswith, equal, formateo, serialización en FastAPI).
"""

from enum import Enum
from typing import Any, Dict, Optional


class EstadoResultado(str, Enum):
    SUCCESS = "success"                      # Operación completada y verificada
    FAILURE = "failure"                      # Operación fallida o verificación negativa
    PENDING = "pending"                      # Operación despachada/en segundo plano (no completada aún)
    NEEDS_CONFIRMATION = "needs_confirmation"# Requiere confirmación explícita del usuario


class Resultado(str):
    """Resultado estructurado con compatibilidad nativa como cadena de texto."""

    ok: bool
    mensaje: str
    evidencia: Dict[str, Any]
    estado: EstadoResultado

    def __new__(
        cls,
        ok: bool,
        mensaje: str,
        evidencia: Optional[Dict[str, Any]] = None,
        estado: Optional[EstadoResultado] = None,
    ):
        mensaje_str = str(mensaje)
        instance = super().__new__(cls, mensaje_str)
        instance.ok = bool(ok)
        instance.mensaje = mensaje_str
        instance.evidencia = dict(evidencia) if evidencia is not None else {}

        if estado is not None:
            if isinstance(estado, str) and not isinstance(estado, EstadoResultado):
                estado = EstadoResultado(estado)
            instance.estado = estado
        else:
            instance.estado = EstadoResultado.SUCCESS if ok else EstadoResultado.FAILURE

        return instance

    def a_dict(self) -> Dict[str, Any]:
        """Representación serializable en JSON."""
        return {
            "ok": self.ok,
            "estado": self.estado.value,
            "mensaje": self.mensaje,
            "evidencia": self.evidencia,
        }

    def __repr__(self) -> str:
        return (
            f"Resultado(ok={self.ok}, estado={self.estado.value!r}, "
            f"mensaje={self.mensaje!r}, evidencia={self.evidencia!r})"
        )

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Resultado):
            return (
                self.ok == other.ok
                and self.estado == other.estado
                and self.mensaje == other.mensaje
                and self.evidencia == other.evidencia
            )
        if isinstance(other, str):
            return self.mensaje == other
        return False

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash((self.mensaje, self.ok, self.estado.value))


PREFIJOS_ERROR_DEFAULT = ("Error", "Pucha", "Falló", "No pude", "Hubo un drama", "Cancelado")


def normalizar_resultado(valor: Any, nombre_comando: str = None) -> Resultado:
    """Convierte cualquier respuesta (legacy str u otro) en un Resultado tipado."""
    if isinstance(valor, Resultado):
        return valor

    if isinstance(valor, tuple) and len(valor) >= 2 and isinstance(valor[0], bool):
        # Soporte para tuplas (ok, mensaje, [evidencia])
        ok = valor[0]
        msg = str(valor[1])
        evidencia = valor[2] if len(valor) > 2 and isinstance(valor[2], dict) else {}
        return Resultado(ok=ok, mensaje=msg, evidencia=evidencia)

    msg = str(valor)
    es_error = msg.startswith(PREFIJOS_ERROR_DEFAULT)
    return Resultado(ok=not es_error, mensaje=msg)
