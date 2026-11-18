"""Modelos de datos y estructuras de intercambio de Madeline."""

import time
from dataclasses import dataclass, field
from typing import Any, Optional
from pydantic import BaseModel, model_validator


@dataclass
class TurnoConversacion:
    usuario: str
    asistente: str
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class AccionLocalReciente:
    comando: str
    args: dict
    resultado: Any
    timestamp: float = field(default_factory=time.monotonic)
    origen: str = "pc"


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    response: str
    image_path: Optional[str] = None
    audio_path: Optional[str] = None
    audio_url: Optional[str] = None
    # Lo que entendió Whisper (solo en /comando_audio): la app lo
    # muestra en la burbuja del usuario.
    transcripcion: Optional[str] = None

    @model_validator(mode="after")
    def sincronizar_audio_campos(self) -> "ChatResponse":
        """Mantiene sincronizados audio_path y audio_url para compatibilidad
        con la app móvil Flutter (audio_path) y contratos de routers (audio_url)."""
        if self.audio_path and not self.audio_url:
            self.audio_url = self.audio_path
        elif self.audio_url and not self.audio_path:
            self.audio_path = self.audio_url
        return self


class ComandoTexto(BaseModel):
    texto: str


class AccionDeck(BaseModel):
    tool: str
    args: dict = {}
