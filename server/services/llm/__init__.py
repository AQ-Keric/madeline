"""Subsistema agnóstico de servicios LLM para Madeline.

Provee la interfaz común LLMProvider, modelos de dominio LLMMessage, LLMRole,
LLMToolDefinition, LLMToolChoice, LLMRequest, LLMResponse y LLMToolCall,
la jerarquía canónica de excepciones y adaptadores concretos como GroqProvider.
"""

from .base import (
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
from .exceptions import (
    LLMConfigurationError,
    LLMConnectionError,
    LLMError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMValidationError,
)
from .providers.groq import GroqProvider

__all__ = [
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMToolCall",
    "LLMMessage",
    "LLMRole",
    "LLMToolDefinition",
    "LLMToolChoice",
    "LLMToolChoiceMode",
    "GroqProvider",
    "LLMError",
    "LLMConfigurationError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "LLMConnectionError",
    "LLMProviderError",
    "LLMValidationError",
]
