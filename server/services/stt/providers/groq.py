"""Adaptador de Speech-To-Text para Groq Whisper.

Implementa STTProvider encapsulando el SDK oficial de Groq (`groq.Groq`)
de forma no bloqueante a través de `asyncio.to_thread`.
"""

import asyncio
import os
from typing import Any, Optional

import groq
import httpx

from ..base import STTProvider, validar_parametros_transcripcion
from ..exceptions import (
    STTConfigurationError,
    STTConnectionError,
    STTEmptyTranscriptionError,
    STTError,
    STTMalformedResponseError,
    STTProviderError,
    STTRateLimitError,
    STTTimeoutError,
)

DEFAULT_MODEL = "whisper-large-v3-turbo"
DEFAULT_TIMEOUT = 25.0
DEFAULT_MAX_RETRIES = 1


def _mapear_error_groq(exc: Exception) -> Exception:
    """Mapea excepciones del SDK de Groq y transporte HTTP a la jerarquía neutral STT."""
    if isinstance(exc, groq.RateLimitError):
        return STTRateLimitError(str(exc), provider="groq")

    if isinstance(exc, (groq.APITimeoutError, httpx.TimeoutException)):
        return STTTimeoutError(str(exc), provider="groq")

    if isinstance(exc, (groq.APIConnectionError, httpx.ConnectError)):
        return STTConnectionError(str(exc), provider="groq")

    if isinstance(exc, groq.AuthenticationError):
        return STTConfigurationError(f"Error de autenticación con Groq: {exc}", provider="groq")

    if isinstance(exc, (groq.BadRequestError, groq.APIStatusError)):
        return STTProviderError(f"Error de API Groq: {exc}", provider="groq")

    if isinstance(exc, groq.APIError):
        return STTProviderError(f"Error de API Groq: {exc}", provider="groq")

    if isinstance(exc, STTError):
        return exc

    return STTProviderError(f"Error inesperado en STT Groq: {exc}", provider="groq")


class GroqSTTProvider(STTProvider):
    """Adaptador de transcripción de audio basado en Groq Whisper Cloud."""

    def __init__(
        self,
        client: Optional[Any] = None,
        api_key: Optional[str] = None,
        default_model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._default_model = default_model
        self._timeout = timeout
        self._max_retries = max_retries

    @property
    def nombre(self) -> str:
        return "groq"

    @property
    def default_model(self) -> str:
        return self._default_model

    def _get_client(self) -> groq.Groq:
        """Inicializa o retorna la instancia perezosa de groq.Groq."""
        if self._client is not None:
            return self._client

        api_key = self._api_key or os.getenv("GROQ_API_KEY")
        if not api_key:
            raise STTConfigurationError(
                "GROQ_API_KEY no está configurada ni provista al inicializar GroqSTTProvider.",
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
            raise STTConfigurationError(
                f"Error al inicializar cliente Groq: {exc}",
                provider="groq",
            ) from exc

    def _normalizar_respuesta(self, raw_response: Any) -> str:
        """Valida y extrae el texto de la respuesta cruda del SDK de Groq."""
        if raw_response is None:
            raise STTMalformedResponseError(
                "El proveedor STT retornó una respuesta nula.",
                provider="groq",
            )

        texto: Optional[str] = None
        if hasattr(raw_response, "text"):
            texto = raw_response.text
        elif isinstance(raw_response, dict):
            texto = raw_response.get("text")
        elif isinstance(raw_response, str):
            texto = raw_response
        else:
            raise STTMalformedResponseError(
                f"Tipo de respuesta no reconocido ({type(raw_response).__name__}).",
                provider="groq",
            )

        if texto is None or not isinstance(texto, str):
            raise STTMalformedResponseError(
                "La respuesta del proveedor no contiene un campo 'text' válido de tipo str.",
                provider="groq",
            )

        texto_limpio = texto.strip()
        if not texto_limpio:
            raise STTEmptyTranscriptionError(
                "La transcripción retornada por el proveedor está vacía.",
                provider="groq",
            )

        return texto_limpio

    async def transcribir(
        self,
        audio_bytes: bytes,
        filename: str,
        language: str = "es",
        prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """Transcribe un fragmento de audio mediante Groq Whisper de forma no bloqueante.

        NOTA DE CONCURRENCIA, TIMEOUT Y CANCELACIÓN:
        La llamada síncrona al SDK `client.audio.transcriptions.create` se ejecuta en un hilo
        worker mediante `asyncio.to_thread`. Si la corrutina que invoca `transcribir()` es
        cancelada antes o durante el await, el event loop levantará `asyncio.CancelledError`.
        Sin embargo, la cancelación de la tarea asyncio NO interrumpe forzosamente la ejecución
        del socket de red en el worker thread subyacente. El hilo continuará ejecutándose hasta
        que la conexión finalice o expire el timeout de transporte HTTP (`self._timeout`).
        """
        # 1. Validación agnóstica estricta
        validar_parametros_transcripcion(
            audio_bytes=audio_bytes,
            filename=filename,
            language=language,
            prompt=prompt,
            model=model,
        )

        # 2. Resolución del modelo
        modelo_a_usar = model or os.getenv("MADELINE_WHISPER_MODELO", self._default_model)

        # 3. Obtención del cliente
        client = self._get_client()

        # 4. Función de ejecución síncrona para to_thread
        def _ejecutar():
            kwargs: dict[str, Any] = {
                "file": (filename, audio_bytes),
                "model": modelo_a_usar,
                "language": language,
                "timeout": self._timeout,
            }
            if prompt is not None and prompt.strip():
                kwargs["prompt"] = prompt.strip()

            return client.audio.transcriptions.create(**kwargs)

        # 5. Ejecución en worker thread con captura y mapeo de excepciones
        try:
            raw_response = await asyncio.to_thread(_ejecutar)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mapped = _mapear_error_groq(exc)
            raise mapped from exc

        # 6. Normalización y validación de la respuesta
        return self._normalizar_respuesta(raw_response)
