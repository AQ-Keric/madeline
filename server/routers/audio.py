"""Endpoints de interacción principal por audio y texto con Madeline."""

import asyncio
import os
import re
from groq import RateLimitError
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

import madeline_server as server

router = APIRouter(tags=["audio"])


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    response: str
    audio_url: str | None = None
    image_path: str | None = None
    transcripcion: str | None = None


class ComandoTexto(BaseModel):
    texto: str


@router.post("/callar")
async def callar_endpoint():
    """Corta lo que Madeline esté diciendo en el PC de inmediato."""
    server.interrumpir_voz_pc()
    server.poner_estado_avatar("escuchando")
    return {"ok": True}


@router.post("/bienvenida")
async def bienvenida_endpoint():
    """Dice la frase de bienvenida + clima por los parlantes del PC."""
    texto = await server.dar_bienvenida()
    return {"mensaje": texto}


@router.post("/comando_audio", response_model=ChatResponse)
async def comando_audio_endpoint(
    audio: UploadFile = File(...),
    origen: str = "pc"
):
    """Recibe audio, lo transcribe con Whisper y lo envía al cerebro."""
    origen = origen.lower().strip()
    if origen not in ("pc", "telefono"):
        raise HTTPException(
            status_code=400,
            detail="El origen debe ser 'pc' o 'telefono'."
        )

    if origen == "pc":
        server.interrumpir_voz_pc()

    try:
        contenido = await audio.read()
        transcripcion = await asyncio.to_thread(
            server.client.audio.transcriptions.create,
            file=(audio.filename or "audio.wav", contenido),
            model=os.getenv("MADELINE_WHISPER_MODELO", "whisper-large-v3-turbo"),
            language="es",
            prompt=server.PISTA_VOCABULARIO,
        )
        mensaje_usuario = transcripcion.text.strip()
    except RateLimitError:
        return await server.responder_sin_cuota(origen)
    except Exception as e:
        print(f"[ERROR WHISPER] {e}")
        raise HTTPException(status_code=500, detail=f"Error procesando el audio: {e}")

    if re.search(server.ALUCINACIONES_WHISPER, mensaje_usuario, re.IGNORECASE):
        mensaje_usuario = ""

    if not mensaje_usuario:
        raise HTTPException(status_code=400, detail="No se detectó voz.")

    print(f"[🎙️ Voz detectada - {origen}]: {mensaje_usuario}")
    request_data = server.ChatRequest(message=mensaje_usuario)
    resultado = await server.procesar_mensaje(request_data, origen=origen)
    resultado.transcripcion = mensaje_usuario
    return resultado


@router.post("/comando_texto", response_model=ChatResponse)
async def recibir_texto(
    comando: ComandoTexto,
    origen: str = "pc"
):
    origen = origen.lower().strip()
    if origen not in ("pc", "telefono"):
        raise HTTPException(
            status_code=400,
            detail="El origen debe ser 'pc' o 'telefono'."
        )

    if not comando.texto.strip():
        raise HTTPException(
            status_code=400,
            detail="El texto no puede estar vacío."
        )

    print(f"[⌨️ Texto - {origen}]: {comando.texto}")
    request_data = server.ChatRequest(message=comando.texto)
    resultado = await server.procesar_mensaje(request_data, origen=origen)
    return resultado
