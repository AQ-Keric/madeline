"""Contratos y modelos agnósticos del dominio LLM para Madeline.

Define abstracciones neutrales y fuertemente tipadas sin dependencias
de proveedores particulares ni convenciones de APIs propietarias.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
import json
import math
from typing import Any, Optional, Union

from .exceptions import LLMValidationError


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
        if not isinstance(self.id, str) or not self.id.strip():
            raise LLMValidationError(
                "El identificador del tool call ('id') debe ser una cadena no vacía."
            )
        if self.id != self.id.strip():
            raise LLMValidationError(
                f"El identificador del tool call ('{self.id}') no debe contener espacios al inicio o al final."
            )

        if not isinstance(self.nombre, str) or not self.nombre.strip():
            raise LLMValidationError(
                "El nombre del tool call debe ser una cadena no vacía."
            )
        if self.nombre != self.nombre.strip():
            raise LLMValidationError(
                f"El nombre del tool call ('{self.nombre}') no debe contener espacios al inicio o al final."
            )

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
        if not isinstance(id, str) or not id.strip():
            raise LLMValidationError(
                "El identificador del tool call ('id') debe ser una cadena no vacía.",
                provider=provider,
            )
        if id != id.strip():
            raise LLMValidationError(
                f"El identificador del tool call ('{id}') no debe contener espacios al inicio o al final.",
                provider=provider,
            )

        if not isinstance(nombre, str) or not nombre.strip():
            raise LLMValidationError(
                "El nombre del tool call debe ser una cadena no vacía.",
                provider=provider,
            )
        if nombre != nombre.strip():
            raise LLMValidationError(
                f"El nombre del tool call ('{nombre}') no debe contener espacios al inicio o al final.",
                provider=provider,
            )

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
        # 1. Validación del rol
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

        # 2. Validación de nombre opcional
        if self.nombre is not None:
            if not isinstance(self.nombre, str) or not self.nombre.strip():
                raise LLMValidationError("El nombre del mensaje debe ser una cadena no vacía.")
            if self.nombre != self.nombre.strip():
                raise LLMValidationError(
                    f"El nombre del mensaje ('{self.nombre}') no debe contener espacios al inicio o al final."
                )

        # 3. Validación de tool_calls
        if not isinstance(self.tool_calls, list):
            raise LLMValidationError(
                f"tool_calls debe ser una lista de LLMToolCall, recibido: {type(self.tool_calls).__name__}."
            )
        for idx, tc in enumerate(self.tool_calls):
            if not isinstance(tc, LLMToolCall):
                raise LLMValidationError(
                    f"El elemento {idx} de tool_calls debe ser una instancia de LLMToolCall, "
                    f"recibido: {type(tc).__name__}."
                )

        # 4. Restricciones por rol
        if self.rol != LLMRole.ASSISTANT and self.tool_calls:
            raise LLMValidationError(
                f"Solo los mensajes con rol ASSISTANT pueden contener tool_calls; "
                f"rol recibido: {self.rol.value}."
            )

        if self.rol == LLMRole.TOOL:
            if not isinstance(self.tool_call_id, str) or not self.tool_call_id.strip():
                raise LLMValidationError(
                    "Los mensajes con rol TOOL requieren un 'tool_call_id' no vacío que identifique la llamada."
                )
            if self.tool_call_id != self.tool_call_id.strip():
                raise LLMValidationError(
                    f"El 'tool_call_id' ('{self.tool_call_id}') no debe contener espacios al inicio o al final."
                )
        elif self.tool_call_id is not None:
            raise LLMValidationError(
                f"Solo los mensajes con rol TOOL pueden tener 'tool_call_id'; "
                f"rol recibido: {self.rol.value}."
            )

        # 5. Validación de contenido
        if self.rol in (LLMRole.SYSTEM, LLMRole.USER):
            if self.contenido is None or not isinstance(self.contenido, str):
                raise LLMValidationError(
                    f"Los mensajes con rol {self.rol.value.upper()} deben tener contenido de texto (str)."
                )
        elif self.rol == LLMRole.ASSISTANT:
            if self.contenido is None and not self.tool_calls:
                raise LLMValidationError(
                    "Un mensaje ASSISTANT no puede tener contenido None sin tool_calls."
                )
            if self.contenido is not None and not isinstance(self.contenido, str):
                raise LLMValidationError(
                    f"El contenido del mensaje debe ser str o None, recibido: {type(self.contenido).__name__}."
                )
        elif self.rol == LLMRole.TOOL:
            if self.contenido is None or not isinstance(self.contenido, str):
                raise LLMValidationError(
                    "Los mensajes con rol TOOL deben tener contenido de texto (str con el resultado)."
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
        if not isinstance(self.nombre, str) or not self.nombre.strip():
            raise LLMValidationError("El nombre de la herramienta debe ser una cadena no vacía.")
        if self.nombre != self.nombre.strip():
            raise LLMValidationError(
                f"El nombre de la herramienta ('{self.nombre}') no debe contener espacios al inicio o al final."
            )

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
        if isinstance(self.modo, LLMToolChoiceMode):
            pass
        elif isinstance(self.modo, str):
            modo_str = self.modo.lower()
            valid_modes = {m.value for m in LLMToolChoiceMode}
            if modo_str not in valid_modes:
                raise LLMValidationError(
                    f"Modo de tool_choice '{self.modo}' no reconocido. Modos válidos: {valid_modes}."
                )
            self.modo = LLMToolChoiceMode(modo_str)
        else:
            raise LLMValidationError(
                f"Tipo inesperado para 'modo' en LLMToolChoice: {type(self.modo).__name__}. "
                f"Debe ser LLMToolChoiceMode o str."
            )

        if self.modo == LLMToolChoiceMode.SPECIFIC:
            if not isinstance(self.nombre_herramienta, str) or not self.nombre_herramienta.strip():
                raise LLMValidationError(
                    "El modo de tool_choice SPECIFIC requiere especificar un 'nombre_herramienta' no vacío."
                )
            if self.nombre_herramienta != self.nombre_herramienta.strip():
                raise LLMValidationError(
                    f"El 'nombre_herramienta' ('{self.nombre_herramienta}') no debe contener espacios al inicio o al final."
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
    """Solicitud neutral y fuertemente tipada de inferencia para un LLM.

    Atributos:
        mensajes: Lista ordenada de mensajes de la conversación.
        tools: Definición opcional de herramientas que el modelo puede invocar.
        tool_choice: Modo opcional de selección de herramientas.
        temperature: Temperatura opcional de muestreo (número finito no negativo).
        max_tokens: Límite máximo opcional de tokens en la respuesta (entero positivo).
        reasoning_effort: Capacidad opcional y específica de proveedores compatibles
            con modelos de razonamiento (ej. Groq con Qwen/DeepSeek/o-series: 'none',
            'low', 'medium', 'high'). Proveedores que no admitan este parámetro
            deben ignorarlo de forma segura sin que su ausencia invalide el contrato.
        timeout: Límite opcional de tiempo de espera en segundos para esta solicitud (número finito positivo).
        model: Identificador opcional de modelo para sobreescribir el predeterminado del proveedor.
    """

    mensajes: list[LLMMessage]
    tools: Optional[list[LLMToolDefinition]] = None
    tool_choice: Optional[Union[LLMToolChoice, str]] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None
    timeout: Optional[float] = None
    model: Optional[str] = None

    def __post_init__(self) -> None:
        # Validación de mensajes
        if not isinstance(self.mensajes, list) or not self.mensajes:
            raise LLMValidationError("La solicitud debe incluir una lista no vacía de mensajes.")
        for i, msg in enumerate(self.mensajes):
            if not isinstance(msg, LLMMessage):
                raise LLMValidationError(
                    f"El elemento {i} de mensajes debe ser una instancia de LLMMessage, "
                    f"recibido: {type(msg).__name__}."
                )

        # Validación de tools
        if self.tools is not None:
            if not isinstance(self.tools, list):
                raise LLMValidationError("tools debe ser una lista de LLMToolDefinition o None.")
            for j, t in enumerate(self.tools):
                if not isinstance(t, LLMToolDefinition):
                    raise LLMValidationError(
                        f"El elemento {j} de tools debe ser una instancia de LLMToolDefinition, "
                        f"recibido: {type(t).__name__}."
                    )

            # Comprobar unicidad de nombres de herramientas
            nombres_vistos: set[str] = set()
            duplicados: list[str] = []
            for t in self.tools:
                if t.nombre in nombres_vistos:
                    if t.nombre not in duplicados:
                        duplicados.append(t.nombre)
                else:
                    nombres_vistos.add(t.nombre)

            if duplicados:
                raise LLMValidationError(
                    f"No se permiten herramientas con nombres duplicados en 'tools'. "
                    f"Nombres duplicados encontrados: {sorted(duplicados)}."
                )

        # Validación de tool_choice
        if self.tool_choice is not None:
            if isinstance(self.tool_choice, str):
                self.tool_choice = LLMToolChoice(modo=self.tool_choice)
            elif not isinstance(self.tool_choice, LLMToolChoice):
                raise LLMValidationError(
                    f"tool_choice debe ser LLMToolChoice, str o None, recibido: {type(self.tool_choice).__name__}."
                )

            # Consistencia para modo SPECIFIC: debe existir en tools declaradas
            if self.tool_choice.modo == LLMToolChoiceMode.SPECIFIC:
                if not self.tools:
                    raise LLMValidationError(
                        "El modo de tool_choice SPECIFIC requiere que 'tools' contenga al menos una herramienta declarada."
                    )
                nombres_disponibles = {t.nombre for t in self.tools}
                if self.tool_choice.nombre_herramienta not in nombres_disponibles:
                    raise LLMValidationError(
                        f"La herramienta '{self.tool_choice.nombre_herramienta}' especificada en tool_choice "
                        f"no existe entre las herramientas declaradas: {sorted(list(nombres_disponibles))}."
                    )

        # Validación de temperature (no booleano, numérico, finito si float, >= 0)
        if self.temperature is not None:
            if (
                isinstance(self.temperature, bool)
                or not isinstance(self.temperature, (int, float))
                or (isinstance(self.temperature, float) and not math.isfinite(self.temperature))
                or self.temperature < 0
            ):
                raise LLMValidationError(
                    f"temperature debe ser un número finito no negativo, recibido: {self.temperature}."
                )

        # Validación de max_tokens (entero positivo)
        if self.max_tokens is not None:
            if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool) or self.max_tokens <= 0:
                raise LLMValidationError(
                    f"max_tokens debe ser un entero positivo, recibido: {self.max_tokens}."
                )

        # Validación de timeout (no booleano, numérico, finito si float, > 0)
        if self.timeout is not None:
            if (
                isinstance(self.timeout, bool)
                or not isinstance(self.timeout, (int, float))
                or (isinstance(self.timeout, float) and not math.isfinite(self.timeout))
                or self.timeout <= 0
            ):
                raise LLMValidationError(
                    f"timeout debe ser un número finito positivo, recibido: {self.timeout}."
                )

        # Validación de model
        if self.model is not None:
            if not isinstance(self.model, str) or not self.model.strip():
                raise LLMValidationError("model debe ser una cadena no vacía.")
            if self.model != self.model.strip():
                raise LLMValidationError(
                    f"El identificador del modelo ('{self.model}') no debe contener espacios al inicio o al final."
                )

        # Validación de reasoning_effort
        if self.reasoning_effort is not None:
            if not isinstance(self.reasoning_effort, str) or not self.reasoning_effort.strip():
                raise LLMValidationError("reasoning_effort debe ser una cadena no vacía o None.")
            if self.reasoning_effort != self.reasoning_effort.strip():
                raise LLMValidationError(
                    f"El valor de reasoning_effort ('{self.reasoning_effort}') no debe contener espacios al inicio o al final."
                )


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
