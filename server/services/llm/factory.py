"""Fábrica desacoplada para selección explícita del proveedor LLM de Madeline.

Mantiene Groq como proveedor predeterminado e inmutable ante ausencia de
configuración, permitiendo seleccionar explícitamente Ollama como capacidad
local complementaria mediante `MADELINE_LLM_PROVIDER=ollama`.
No implementa fallback automático ni conmutación ante pérdida de red.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .base import LLMProvider
from .coordinator import OllamaCoordinator
from .exceptions import LLMConfigurationError
from .providers.groq import GroqProvider
from .providers.ollama import OllamaProvider

DEFAULT_LLM_PROVIDER = "groq"


def _obtener_resource_manager_si_existe() -> Optional[Any]:
    """Obtiene el ResourceManager global respetando compatibilidad de rutas de import."""
    try:
        from ..resource_manager import obtener_resource_manager

        return obtener_resource_manager()
    except ImportError:
        try:
            from server.services.resource_manager import obtener_resource_manager

            return obtener_resource_manager()
        except ImportError:
            return None


def crear_proveedor_llm(
    provider_name: Optional[str] = None,
    *,
    groq_api_key: Optional[str] = None,
    groq_client: Optional[Any] = None,
    ollama_base_url: Optional[str] = None,
    ollama_modo_gestionado: bool = True,
    ollama_coordinator: Optional[OllamaCoordinator] = None,
    resource_manager: Optional[Any] = None,
    default_model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> LLMProvider:
    """Instancia el proveedor LLM configurado explícitamente sin fallback automático.

    Args:
        provider_name: Nombre explícito ('groq' u 'ollama'). Si es None, lee la
            variable de entorno `MADELINE_LLM_PROVIDER` (predeterminado: 'groq').
        groq_api_key: API key opcional para GroqProvider.
        groq_client: Cliente groq.Groq opcional inyectable para pruebas.
        ollama_base_url: URL explícita opcional para OllamaProvider.
        ollama_modo_gestionado: True para ciclo aislado vía systemd --user; False para
            endpoint externo explícito.
        ollama_coordinator: Coordinador compartido opcional para OllamaProvider.
        resource_manager: ResourceManager inyectable para gobernanza local en Ollama.
        default_model: Sobreescritura opcional del modelo por defecto.
        timeout: Sobreescritura opcional del timeout en segundos.
    """
    nombre_raw = (
        provider_name
        if provider_name is not None
        else os.getenv("MADELINE_LLM_PROVIDER", DEFAULT_LLM_PROVIDER)
    )
    if not isinstance(nombre_raw, str) or not nombre_raw.strip():
        raise LLMConfigurationError(
            "El identificador del proveedor LLM no puede estar vacío.",
        )

    nombre = nombre_raw.strip().lower()

    if nombre == "groq":
        kwargs: dict[str, Any] = {}
        if groq_api_key is not None:
            kwargs["api_key"] = groq_api_key
        if groq_client is not None:
            kwargs["client"] = groq_client
        if default_model is not None:
            kwargs["default_model"] = default_model
        if timeout is not None:
            kwargs["timeout"] = timeout
        return GroqProvider(**kwargs)

    if nombre == "ollama":
        rm = resource_manager if resource_manager is not None else _obtener_resource_manager_si_existe()
        kwargs_ollama: dict[str, Any] = {
            "modo_gestionado": ollama_modo_gestionado,
            "resource_manager": rm,
        }
        if ollama_base_url is not None:
            kwargs_ollama["base_url"] = ollama_base_url
        if ollama_coordinator is not None:
            kwargs_ollama["coordinator"] = ollama_coordinator
        if default_model is not None:
            kwargs_ollama["default_model"] = default_model
        if timeout is not None:
            kwargs_ollama["timeout"] = timeout
        return OllamaProvider(**kwargs_ollama)

    raise LLMConfigurationError(
        f"Proveedor LLM '{nombre_raw}' no soportado. Valores válidos: 'groq', 'ollama'.",
        provider=nombre,
    )
