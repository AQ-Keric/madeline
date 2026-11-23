"""Adaptador para el proveedor de inferencia Groq Cloud.

Implementa LLMProvider envolviendo el SDK oficial de Groq (`groq.Groq`)
de forma no bloqueante a través de `asyncio.to_thread`.
"""

import asyncio
import base64
import json
import os
import re
from typing import Any, Optional, Union

import groq
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
from ..exceptions import (
    LLMConfigurationError,
    LLMConnectionError,
    LLMError,
    LLMMalformedResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMToolCallRejectionError,
    LLMValidationError,
)

DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_VISION_MODEL = "qwen/qwen3.6-27b"
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_RETRIES = 1

EXTENSIONES_VISION_PERMITIDAS: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}

PROMPT_VISION_INSTRUCCION = (
    "Respondé SIEMPRE en español, en una o dos frases breves, "
    "en texto plano sin markdown (sin asteriscos ni negritas) "
    "porque tu respuesta se va a leer en voz alta. Pregunta: "
)


def _limpiar_texto_para_voz(texto: str) -> str:
    """Limpia markdown de una respuesta antes de hablarla."""
    if not texto:
        return ""

    lineas = texto.split("\n")
    lineas_limpias = []

    for linea in lineas:
        if "|" in linea and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", linea):
            continue

        if linea.count("|") >= 2:
            celdas = [c.strip() for c in linea.split("|") if c.strip()]
            if celdas:
                linea = ", ".join(celdas) + "."

        lineas_limpias.append(linea)

    texto = "\n".join(lineas_limpias)
    texto = re.sub(r"^#{1,6}\s*", "", texto, flags=re.MULTILINE)
    texto = re.sub(r"\*\*(.+?)\*\*", r"\1", texto)
    texto = re.sub(r"\*(.+?)\*", r"\1", texto)
    texto = re.sub(r"__(.+?)__", r"\1", texto)
    texto = re.sub(r"^\s*[-*]\s+", "", texto, flags=re.MULTILINE)
    texto = re.sub(r"\n{2,}", ". ", texto)
    texto = re.sub(r"\n", " ", texto)
    texto = re.sub(r"\s{2,}", " ", texto)

    return texto.strip()


def _limpiar_respuesta_vision(texto: str) -> str:
    """Limpia bloques de razonamiento (<think>...</think>) y formato de visión."""
    if not texto:
        return ""

    texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.DOTALL | re.IGNORECASE)

    if re.search(r"<think>", texto, flags=re.IGNORECASE):
        return ""

    return _limpiar_texto_para_voz(texto)


def _mapear_error_groq(exc: Exception) -> Exception:
    """Mapea excepciones del SDK de Groq y transporte HTTP a excepciones del dominio LLM.

    Prioriza estrictamente todos los tipos conocidos del SDK y transporte antes de interpretar
    mensajes de error:
    1. Errores tipados de rate limit (groq.RateLimitError).
    2. Errores tipados de timeout (groq.APITimeoutError, httpx.TimeoutException).
    3. Errores tipados de conexión/transporte (groq.APIConnectionError, httpx.ConnectError).
    4. Errores tipados de autenticación (groq.AuthenticationError).
    5. Otros errores ya pertenecientes al contrato neutral (LLMError).
    6. Rechazo recuperable de function calling (restringido a errores HTTP 400 groq.BadRequestError).
    7. Errores generales de API o transporte HTTP (groq.APIError, httpx.HTTPError).
    8. Error inesperado.
    """
    # 1. Errores tipados de rate limit
    if isinstance(exc, groq.RateLimitError):
        return LLMRateLimitError(str(exc), provider="groq")

    # 2. Errores tipados de timeout
    if isinstance(exc, (groq.APITimeoutError, httpx.TimeoutException)):
        return LLMTimeoutError(str(exc), provider="groq")

    # 3. Errores tipados de conexión/transporte
    if isinstance(exc, (groq.APIConnectionError, httpx.ConnectError)):
        return LLMConnectionError(str(exc), provider="groq")

    # 4. Errores tipados de autenticación
    if isinstance(exc, groq.AuthenticationError):
        return LLMConfigurationError(f"Error de autenticación con Groq: {exc}", provider="groq")

    # 5. Otros errores ya pertenecientes al contrato neutral
    if isinstance(exc, LLMError):
        return exc

    # 6. Rechazo recuperable de function calling: estrictamente restringido a errores HTTP 400 (groq.BadRequestError)
    es_error_400 = isinstance(exc, groq.BadRequestError) or (
        isinstance(exc, groq.APIStatusError) and getattr(exc, "status_code", None) == 400
    )
    if es_error_400:
        es_rechazo = False
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error_dict = body.get("error")
            if isinstance(error_dict, dict):
                code = error_dict.get("code")
                if code in ("tool_use_failed",):
                    es_rechazo = True
                err_msg = error_dict.get("message")
                if isinstance(err_msg, str) and ("tool_use_failed" in err_msg or "Tool choice is none" in err_msg):
                    es_rechazo = True

        if not es_rechazo:
            msg = str(exc)
            if "tool_use_failed" in msg or "Tool choice is none" in msg:
                es_rechazo = True

        if es_rechazo:
            return LLMToolCallRejectionError(str(exc), provider="groq")

        return LLMProviderError(str(exc), provider="groq")

    # 7. Errores generales de API de Groq (500, 404, etc.)
    if isinstance(exc, groq.APIError):
        return LLMProviderError(str(exc), provider="groq")

    # 8. Errores de transporte HTTP genéricos
    if isinstance(exc, httpx.HTTPError):
        return LLMProviderError(str(exc), provider="groq")

    # 9. Error inesperado
    return LLMProviderError(f"Error inesperado del proveedor Groq: {exc}", provider="groq")


class GroqProvider(LLMProvider):
    """Adaptador que implementa LLMProvider comunicándose con la API de Groq Cloud."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = DEFAULT_MODEL,
        default_vision_model: str = DEFAULT_VISION_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: Optional[Any] = None,
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._default_vision_model = default_vision_model
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: Optional[Any] = client

    @property
    def nombre(self) -> str:
        return "groq"

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def default_vision_model(self) -> str:
        return self._default_vision_model

    def _get_client(self) -> groq.Groq:
        """Inicializa o retorna la instancia perezosa de groq.Groq."""
        if self._client is not None:
            return self._client

        api_key = self._api_key or os.getenv("GROQ_API_KEY")
        if not api_key:
            raise LLMConfigurationError(
                "GROQ_API_KEY no está configurada ni provista al inicializar GroqProvider.",
                provider="groq",
            )

        try:
            self._client = groq.Groq(
                api_key=api_key,
                timeout=self._timeout,
                max_retries=self._max_retries,
            )
            return self._client
        except Exception as exc:
            raise LLMConfigurationError(
                f"Error al inicializar cliente Groq: {exc}",
                provider="groq",
            ) from exc

    def _convertir_mensajes(self, mensajes: list[LLMMessage]) -> list[dict[str, Any]]:
        """Convierte una lista de LLMMessage del dominio al formato de mensajes de Groq/OpenAI."""
        groq_messages: list[dict[str, Any]] = []

        for msg in mensajes:
            rol = msg.rol.value if isinstance(msg.rol, LLMRole) else str(msg.rol)

            if rol == LLMRole.SYSTEM.value:
                groq_messages.append({
                    "role": "system",
                    "content": msg.contenido or "",
                })
            elif rol == LLMRole.USER.value:
                groq_messages.append({
                    "role": "user",
                    "content": msg.contenido or "",
                })
            elif rol == LLMRole.ASSISTANT.value:
                payload: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.contenido,
                }
                if msg.tool_calls:
                    payload["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.nombre,
                                "arguments": json.dumps(tc.argumentos),
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                groq_messages.append(payload)
            elif rol == LLMRole.TOOL.value:
                payload = {
                    "role": "tool",
                    "content": msg.contenido or "",
                    "tool_call_id": msg.tool_call_id or "",
                }
                if msg.nombre:
                    payload["name"] = msg.nombre
                groq_messages.append(payload)

        return groq_messages

    def _convertir_tools(self, tools: list[LLMToolDefinition]) -> list[dict[str, Any]]:
        """Convierte definiciones neutrales de herramientas al esquema de funciones de Groq."""
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

    def _convertir_tool_choice(self, choice: Union[LLMToolChoice, str]) -> Union[str, dict[str, Any]]:
        """Convierte una configuración neutral de tool_choice a la convención de Groq."""
        if isinstance(choice, str):
            choice = LLMToolChoice(modo=choice)

        if choice.modo == LLMToolChoiceMode.AUTO:
            return "auto"
        elif choice.modo == LLMToolChoiceMode.NONE:
            return "none"
        elif choice.modo == LLMToolChoiceMode.REQUIRED:
            return "required"
        elif choice.modo == LLMToolChoiceMode.SPECIFIC:
            return {
                "type": "function",
                "function": {"name": choice.nombre_herramienta},
            }
        return "auto"

    def _procesar_respuesta(self, response: Any) -> LLMResponse:
        """Convierte una respuesta de Groq en un LLMResponse tipado.

        Aplica validaciones estrictas sobre la estructura del payload y los tool calls:
        no fabrica IDs ni nombres artificiales ante datos ausentes o malformados.
        """
        try:
            choices = getattr(response, "choices", None)
            if not choices or not isinstance(choices, (list, tuple)):
                raise LLMMalformedResponseError(
                    "La respuesta de Groq no contiene una lista válida de 'choices'.",
                    provider="groq",
                )

            choice = choices[0]
            message = getattr(choice, "message", None)
            if message is None:
                raise LLMMalformedResponseError(
                    "La primera opción de respuesta ('choice') no contiene el atributo 'message'.",
                    provider="groq",
                )

            texto_raw = getattr(message, "content", None)
            if texto_raw is not None and not isinstance(texto_raw, str):
                raise LLMMalformedResponseError(
                    f"El contenido del mensaje ('content') debe ser str o None, recibido: {type(texto_raw).__name__}.",
                    provider="groq",
                )
            texto = texto_raw

            finish_reason_raw = getattr(choice, "finish_reason", None)
            if finish_reason_raw is not None:
                if not isinstance(finish_reason_raw, str) or not finish_reason_raw.strip():
                    raise LLMMalformedResponseError(
                        f"El motivo de finalización ('finish_reason') debe ser una cadena no vacía o None, recibido: {type(finish_reason_raw).__name__}.",
                        provider="groq",
                    )
                if finish_reason_raw != finish_reason_raw.strip():
                    raise LLMMalformedResponseError(
                        f"El motivo de finalización ('{finish_reason_raw}') no debe contener espacios al inicio o al final.",
                        provider="groq",
                    )
                finish_reason = finish_reason_raw
            else:
                finish_reason = None

            modelo_raw = getattr(response, "model", self._default_model)
            if not isinstance(modelo_raw, str) or not modelo_raw.strip():
                modelo = self._default_model
            else:
                modelo = modelo_raw.strip()

            raw_tool_calls = getattr(message, "tool_calls", None)
            tool_calls: list[LLMToolCall] = []

            if raw_tool_calls is not None:
                if not isinstance(raw_tool_calls, (list, tuple)):
                    raise LLMMalformedResponseError(
                        f"tool_calls esperado como lista o tupla, recibido: {type(raw_tool_calls).__name__}",
                        provider="groq",
                    )

                for tc in raw_tool_calls:
                    tc_id = getattr(tc, "id", None)
                    if not isinstance(tc_id, str) or not tc_id.strip():
                        raise LLMMalformedResponseError(
                            "Tool call recibido sin identificador ('id') válido.",
                            provider="groq",
                        )
                    if tc_id != tc_id.strip():
                        raise LLMMalformedResponseError(
                            f"El identificador del tool call ('{tc_id}') no debe contener espacios al inicio o al final.",
                            provider="groq",
                        )

                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        fn_name = getattr(fn, "name", None)
                        fn_args = getattr(fn, "arguments", None)
                    else:
                        fn_name = getattr(tc, "name", None)
                        fn_args = getattr(tc, "arguments", None)

                    if not isinstance(fn_name, str) or not fn_name.strip():
                        raise LLMMalformedResponseError(
                            "Tool call recibido sin nombre de herramienta válido.",
                            provider="groq",
                        )
                    if fn_name != fn_name.strip():
                        raise LLMMalformedResponseError(
                            f"El nombre del tool call ('{fn_name}') no debe contener espacios al inicio o al final.",
                            provider="groq",
                        )

                    if fn_args is None:
                        raise LLMMalformedResponseError(
                            f"Tool call '{fn_name}' recibido sin argumentos ('arguments' es None).",
                            provider="groq",
                        )

                    try:
                        tool_call = LLMToolCall.from_raw(
                            id=tc_id,
                            nombre=fn_name,
                            raw_arguments=fn_args,
                            provider="groq",
                        )
                    except LLMValidationError as val_err:
                        raise LLMMalformedResponseError(
                            f"Argumentos malformados en tool call '{fn_name}': {val_err.mensaje}",
                            provider="groq",
                        ) from val_err

                    tool_calls.append(tool_call)

            if texto is None and not tool_calls:
                raise LLMMalformedResponseError(
                    "La respuesta de Groq no contiene contenido textual ('content') ni llamadas a herramientas ('tool_calls') válidas.",
                    provider="groq",
                )

            return LLMResponse(
                texto=texto,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                modelo=modelo,
                proveedor="groq",
            )
        except LLMError:
            raise
        except Exception as exc:
            raise LLMProviderError(
                f"Error al procesar la respuesta de Groq: {exc}",
                provider="groq",
            ) from exc

    async def generar_respuesta(
        self,
        solicitud: LLMRequest,
    ) -> LLMResponse:
        """Genera una respuesta usando el SDK de Groq en un hilo no bloqueante.

        Nota sobre cancelación y concurrencia:
        La llamada síncrona a Groq se ejecuta en un worker thread mediante asyncio.to_thread.
        Si la corrutina que espera esta llamada es cancelada (asyncio.CancelledError), la espera
        en el event loop se interrumpe de inmediato; sin embargo, el hilo secundario continuará
        ejecutando la petición HTTP subyacente hasta su finalización o hasta que expire el socket
        o timeout del cliente. Cancelar la corrutina NO aborta la inferencia en ejecución en el
        proveedor ni interrumpe inmediatamente el socket en el hilo secundario.
        """
        client = self._get_client()

        target_model = solicitud.model or self._default_model
        params: dict[str, Any] = {
            "model": target_model,
            "messages": self._convertir_mensajes(solicitud.mensajes),
        }

        if solicitud.tools:
            params["tools"] = self._convertir_tools(solicitud.tools)
        if solicitud.tool_choice is not None:
            params["tool_choice"] = self._convertir_tool_choice(solicitud.tool_choice)
        if solicitud.temperature is not None:
            params["temperature"] = solicitud.temperature
        if solicitud.max_tokens is not None:
            if solicitud.reasoning_effort is not None:
                params["max_completion_tokens"] = solicitud.max_tokens
            else:
                params["max_tokens"] = solicitud.max_tokens
        if solicitud.reasoning_effort is not None:
            params["reasoning_effort"] = solicitud.reasoning_effort
        if solicitud.timeout is not None:
            params["timeout"] = solicitud.timeout

        def _sync_call():
            return client.chat.completions.create(**params)

        try:
            response = await asyncio.to_thread(_sync_call)
        except Exception as exc:
            mapped = _mapear_error_groq(exc)
            if mapped is exc:
                raise
            raise mapped from exc

        return self._procesar_respuesta(response)

    async def analizar_vision(
        self,
        imagen_bytes: bytes,
        extension: str,
        pregunta: str,
        model: Optional[str] = None,
    ) -> str:
        """Analiza una imagen codificada en base64 con el modelo de visión de Groq.

        Valida explícitamente los argumentos y la extensión (PNG, JPG, JPEG) antes
        de inicializar el cliente o acceder a credenciales.
        """
        # 1. Validación de argumentos antes de inicializar cliente o consultar credenciales
        if not isinstance(extension, str) or not extension.strip():
            raise LLMValidationError(
                "La extensión de imagen debe ser una cadena no vacía.",
                provider="groq",
            )

        ext_limpia = extension.strip().lstrip(".").lower()
        if ext_limpia not in EXTENSIONES_VISION_PERMITIDAS:
            raise LLMValidationError(
                f"Extensión de imagen '{extension}' no admitida. Formatos permitidos: PNG, JPG, JPEG.",
                provider="groq",
            )
        mime_type = EXTENSIONES_VISION_PERMITIDAS[ext_limpia]

        if not isinstance(imagen_bytes, bytes) or len(imagen_bytes) == 0:
            raise LLMValidationError(
                "imagen_bytes debe ser un objeto bytes no vacío.",
                provider="groq",
            )

        if not isinstance(pregunta, str) or not pregunta.strip():
            raise LLMValidationError(
                "pregunta debe ser una cadena de texto no vacía.",
                provider="groq",
            )

        # 2. Inicialización perezosa del cliente
        client = self._get_client()

        imagen_b64 = base64.b64encode(imagen_bytes).decode("utf-8")
        data_uri = f"data:{mime_type};base64,{imagen_b64}"

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT_VISION_INSTRUCCION + pregunta},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_uri,
                        },
                    },
                ],
            }
        ]

        target_model = model or self._default_vision_model
        params: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": 0.3,
            "max_completion_tokens": 300,
            "reasoning_effort": "none",
        }

        def _sync_call():
            return client.chat.completions.create(**params)

        try:
            response = await asyncio.to_thread(_sync_call)
        except Exception as exc:
            mapped = _mapear_error_groq(exc)
            if mapped is exc:
                raise
            raise mapped from exc

        try:
            raw_text = response.choices[0].message.content or ""
            return _limpiar_respuesta_vision(raw_text)
        except Exception as exc:
            raise LLMProviderError(
                f"Error al procesar la respuesta de visión de Groq: {exc}",
                provider="groq",
            ) from exc
