"""Excepciones agnósticas del dominio LLM para Madeline.

Jerarquía de excepciones puras, desacopladas de cualquier SDK o proveedor
específico (sin dependencias de Groq ni bibliotecas externas).
"""

from typing import Optional


class LLMError(Exception):
    """Excepción base para todos los errores relacionados con LLM en Madeline."""

    def __init__(self, mensaje: str, provider: Optional[str] = None) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.provider = provider

    def __str__(self) -> str:
        if self.provider:
            return f"[{self.provider}] {self.mensaje}"
        return self.mensaje


class LLMConfigurationError(LLMError):
    """Error de configuración en el proveedor LLM (ej. API key ausente o inválida)."""


class LLMRateLimitError(LLMError):
    """Límite de tasa (rate limit / HTTP 429) excedido en el proveedor LLM."""


class LLMTimeoutError(LLMError):
    """Tiempo de espera agotado al comunicarse con el proveedor LLM."""


class LLMConnectionError(LLMError):
    """Fallo de conexión de red o transporte con el proveedor LLM."""


class LLMProviderError(LLMError):
    """Error genérico del proveedor LLM o fallo al procesar la respuesta."""


class LLMValidationError(LLMProviderError):
    """Error de validación en datos de entrada del dominio LLM (mensajes, esquemas, extensiones)."""
