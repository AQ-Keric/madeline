"""Subsistema agnóstico de servicios LLM para Madeline.

Provee la interfaz común LLMProvider, modelos de dominio LLMMessage, LLMRole,
LLMToolDefinition, LLMToolChoice, LLMRequest, LLMResponse y LLMToolCall,
la jerarquía canónica de excepciones, el coordinador local OllamaCoordinator,
la fábrica crear_proveedor_llm y adaptadores concretos (GroqProvider, OllamaProvider).
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
from .coordinator import (
    EstadoEndpoint,
    EstadoTicket,
    OllamaCoordinator,
    SlotTicket,
)
from .exceptions import (
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
from .factory import crear_proveedor_llm
from .providers.groq import GroqProvider
from .providers.ollama import OllamaProvider
from .providers.ollama_lifecycle import OllamaServerManager

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
    "OllamaProvider",
    "OllamaServerManager",
    "OllamaCoordinator",
    "EstadoTicket",
    "EstadoEndpoint",
    "SlotTicket",
    "crear_proveedor_llm",
    "LLMError",
    "LLMConfigurationError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "LLMConnectionError",
    "LLMToolCallRejectionError",
    "LLMProviderError",
    "LLMValidationError",
    "LLMMalformedResponseError",
]
