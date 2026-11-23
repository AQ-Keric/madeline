"""Contratos y modelos agnósticos del dominio LLM para Madeline.

Define abstracciones neutrales y fuertemente tipadas sin dependencias
de proveedores particulares ni convenciones de APIs propietarias.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Any, Optional, Union

from .exceptions import LLMProviderError, LLMValidationError


class LLMRole(str, Enum):
    """Rol del participante en la conversación."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class LLMToolCall:
    """Representación normalizada de una llamada a herramienta generada por el LLM."""

    id: str
    nombre: str
    argumentos: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.argumentos, dict):
            raise LLMValidationError(
                f"Los argumentos del tool call '{self.nombre}' deben ser un diccionario (dict), "
                f"recibido: {type(self.argumentos).__name__}."
            )

    @classmethod
    def from_raw(
        cls,
        id: str,
        nombre: str,
        raw_arguments: Union[str, dict[str, Any]],
        provider: Optional[str] = None,
    ) -> "LLMToolCall":
        """Construye un LLMToolCall a partir de argumentos crudos (dict o JSON string).

        Aplica deserialización estricta. Si raw_arguments es una cadena no JSON o
        no deserializa a un diccionario (objeto JSON), lanza LLMValidationError.
        """
        if isinstance(raw_arguments, dict):
            return cls(id=id, nombre=nombre, argumentos=raw_arguments)

        if isinstance(raw_arguments, str):
            try:
                parsed = json.loads(raw_arguments)
            except Exception as exc:
                raise LLMValidationError(
                    f"Los argumentos del tool call '{nombre}' no son un JSON válido: {exc}",
                    provider=provider,
                ) from exc

            if not isinstance(parsed, dict):
                raise LLMValidationError(
                    f"Los argumentos del tool call '{nombre}' deben ser un objeto JSON (dict), "
                    f"recibido: {type(parsed).__name__}.",
                    provider=provider,
                )
            return cls(id=id, nombre=nombre, argumentos=parsed)

        raise LLMValidationError(
            f"Tipo inesperado para los argumentos del tool call '{nombre}': "
            f"{type(raw_arguments).__name__}.",
            provider=provider,
        )


@dataclass
class LLMMessage:
    """Mensaje neutral en una conversación con el LLM."""

    rol: Union[LLMRole, str]
    contenido: Optional[str] = None
    nombre: Optional[str] = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.rol, LLMRole):
            pass
        elif isinstance(self.rol, str):
            rol_str = self.rol.lower()
            valid_roles = {r.value for r in LLMRole}
            if rol_str not in valid_roles:
                raise LLMValidationError(
                    f"Rol de mensaje '{self.rol}' no reconocido. Roles válidos: {valid_roles}."
                )
            self.rol = LLMRole(rol_str)
        else:
            raise LLMValidationError(
                f"Tipo inesperado para el rol de mensaje: {type(self.rol).__name__}."
            )

        if self.contenido is None and not self.tool_calls and not self.tool_call_id:
            raise LLMValidationError(
                "Un mensaje de LLM no puede tener contenido None sin tool_calls ni tool_call_id."
            )

        if self.contenido is not None and not isinstance(self.contenido, str):
            raise LLMValidationError(
                f"El contenido del mensaje debe ser str o None, recibido: {type(self.contenido).__name__}."
            )

    @classmethod
    def system(cls, contenido: str) -> "LLMMessage":
        """Crea un mensaje con rol del sistema."""
        return cls(rol=LLMRole.SYSTEM, contenido=contenido)

    @classmethod
    def user(cls, contenido: str) -> "LLMMessage":
        """Crea un mensaje con rol de usuario."""
        return cls(rol=LLMRole.USER, contenido=contenido)

    @classmethod
    def assistant(
        cls,
        contenido: Optional[str] = None,
        tool_calls: Optional[list[LLMToolCall]] = None,
    ) -> "LLMMessage":
        """Crea un mensaje con rol del asistente."""
        return cls(
            rol=LLMRole.ASSISTANT,
            contenido=contenido,
            tool_calls=tool_calls or [],
        )

    @classmethod
    def tool_result(
        cls,
        tool_call_id: str,
        contenido: str,
        nombre: Optional[str] = None,
    ) -> "LLMMessage":
        """Crea un mensaje con el resultado de ejecución de una herramienta."""
        return cls(
            rol=LLMRole.TOOL,
            contenido=contenido,
            tool_call_id=tool_call_id,
            nombre=nombre,
        )


@dataclass
class LLMToolDefinition:
    """Definición neutral de una herramienta disponible para el LLM."""

    nombre: str
    descripcion: str
    parametros: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.nombre or not isinstance(self.nombre, str):
            raise LLMValidationError("El nombre de la herramienta debe ser una cadena no vacía.")
        if not isinstance(self.descripcion, str):
            raise LLMValidationError("La descripción de la herramienta debe ser una cadena.")
        if not isinstance(self.parametros, dict):
            raise LLMValidationError(
                f"Los parámetros de la herramienta deben ser un diccionario (JSON Schema), "
                f"recibido: {type(self.parametros).__name__}."
            )


class LLMToolChoiceMode(str, Enum):
    """Modo neutral de selección de herramientas."""

    AUTO = "auto"
    NONE = "none"
    REQUIRED = "required"
    SPECIFIC = "specific"


@dataclass
class LLMToolChoice:
    """Configuración neutral para la selección de herramientas por parte del LLM."""

    modo: Union[LLMToolChoiceMode, str] = LLMToolChoiceMode.AUTO
    nombre_herramienta: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.modo, str):
            modo_str = self.modo.lower()
            valid_modes = {m.value for m in LLMToolChoiceMode}
            if modo_str not in valid_modes:
                raise LLMValidationError(
                    f"Modo de tool_choice '{self.modo}' no reconocido. Modos válidos: {valid_modes}."
                )
            self.modo = LLMToolChoiceMode(modo_str)

        if self.modo == LLMToolChoiceMode.SPECIFIC:
            if not self.nombre_herramienta or not isinstance(self.nombre_herramienta, str):
                raise LLMValidationError(
                    "El modo de tool_choice SPECIFIC requiere especificar nombre_herramienta."
                )

    @classmethod
    def auto(cls) -> "LLMToolChoice":
        return cls(modo=LLMToolChoiceMode.AUTO)

    @classmethod
    def none(cls) -> "LLMToolChoice":
        return cls(modo=LLMToolChoiceMode.NONE)

    @classmethod
    def required(cls) -> "LLMToolChoice":
        return cls(modo=LLMToolChoiceMode.REQUIRED)

    @classmethod
    def specific(cls, nombre_herramienta: str) -> "LLMToolChoice":
        return cls(modo=LLMToolChoiceMode.SPECIFIC, nombre_herramienta=nombre_herramienta)


@dataclass
class LLMRequest:
    """Solicitud neutral y fuertemente tipada de inferencia para un LLM."""

    mensajes: list[LLMMessage]
    tools: Optional[list[LLMToolDefinition]] = None
    tool_choice: Optional[Union[LLMToolChoice, str]] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None
    timeout: Optional[float] = None
    model: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.mensajes, list) or not self.mensajes:
            raise LLMValidationError("La solicitud debe incluir una lista no vacía de mensajes.")
        for i, msg in enumerate(self.mensajes):
            if not isinstance(msg, LLMMessage):
                raise LLMValidationError(
                    f"El elemento {i} de mensajes debe ser una instancia de LLMMessage, "
                    f"recibido: {type(msg).__name__}."
                )
        if self.tools is not None:
            if not isinstance(self.tools, list):
                raise LLMValidationError("tools debe ser una lista de LLMToolDefinition o None.")
            for j, t in enumerate(self.tools):
                if not isinstance(t, LLMToolDefinition):
                    raise LLMValidationError(
                        f"El elemento {j} de tools debe ser una instancia de LLMToolDefinition, "
                        f"recibido: {type(t).__name__}."
                    )
        if self.tool_choice is not None and isinstance(self.tool_choice, str):
            self.tool_choice = LLMToolChoice(modo=self.tool_choice)


@dataclass
class LLMResponse:
    """Respuesta normalizada producida por un proveedor LLM."""

    texto: Optional[str] = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    modelo: str = ""
    proveedor: str = ""


class LLMProvider(ABC):
    """Interfaz abstracta canónica para proveedores de modelos de lenguaje (LLM)."""

    @property
    @abstractmethod
    def nombre(self) -> str:
        """Nombre identificador único del proveedor (ej. 'groq', 'ollama')."""

    @abstractmethod
    async def generar_respuesta(
        self,
        solicitud: LLMRequest,
    ) -> LLMResponse:
        """Genera una respuesta de texto o invocaciones a herramientas a partir de una solicitud neutral."""

    @abstractmethod
    async def analizar_vision(
        self,
        imagen_bytes: bytes,
        extension: str,
        pregunta: str,
        model: Optional[str] = None,
    ) -> str:
        """Analiza una imagen estática con una pregunta en lenguaje natural."""
