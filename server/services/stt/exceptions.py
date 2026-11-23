"""Jerarquía canónica y agnóstica de excepciones para el dominio Speech-To-Text (STT).

Desacoplada de cualquier SDK o proveedor específico (sin dependencias directas de Groq,
Whisper local ni bibliotecas de red externas).
"""

from typing import Optional


class STTError(Exception):
    """Excepción base para todos los errores del dominio STT en Madeline."""

    def __init__(self, mensaje: str, provider: Optional[str] = None) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.provider = provider

    def __str__(self) -> str:
        if self.provider:
            return f"[{self.provider}] {self.mensaje}"
        return self.mensaje


class STTConfigurationError(STTError):
    """Error de configuración o credenciales en el proveedor STT (ej. API key ausente o inválida)."""


class STTRateLimitError(STTError):
    """Límite de tasa de peticiones o cuota excedida en el proveedor STT (HTTP 429)."""


class STTTimeoutError(STTError):
    """Tiempo de espera agotado al transcribir audio en el proveedor STT."""


class STTConnectionError(STTError):
    """Fallo de red o transporte al comunicarse con el proveedor STT."""


class STTProviderError(STTError):
    """Error genérico devuelto por el proveedor STT o fallo al procesar la respuesta."""


class STTValidationError(STTError):
    """Error de validación en parámetros de entrada, metadatos o formato de audio."""


class STTMalformedResponseError(STTProviderError):
    """Respuesta del proveedor STT corrupta o que viola el contrato esperado."""


class STTEmptyTranscriptionError(STTMalformedResponseError):
    """Respuesta válida del proveedor cuyo contenido de transcripción está vacío o carece de texto utilizable."""
