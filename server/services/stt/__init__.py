"""Subsistema agnóstico de Speech-To-Text (STT) para Madeline.

Provee el contrato abstracto `STTProvider`, validaciones de entrada, la jerarquía
neutral de excepciones y adaptadores concretos como `GroqSTTProvider`.
"""

from .base import EXTENSIONES_AUDIO_PERMITIDAS, STTProvider, validar_parametros_transcripcion
from .exceptions import (
    STTConfigurationError,
    STTConnectionError,
    STTEmptyTranscriptionError,
    STTError,
    STTMalformedResponseError,
    STTProviderError,
    STTRateLimitError,
    STTTimeoutError,
    STTValidationError,
)
from .providers.groq import GroqSTTProvider

__all__ = [
    "STTProvider",
    "GroqSTTProvider",
    "EXTENSIONES_AUDIO_PERMITIDAS",
    "validar_parametros_transcripcion",
    "STTError",
    "STTConfigurationError",
    "STTRateLimitError",
    "STTTimeoutError",
    "STTConnectionError",
    "STTProviderError",
    "STTValidationError",
    "STTMalformedResponseError",
    "STTEmptyTranscriptionError",
]
