"""Endpoints de interacción principal por audio y texto con Madeline."""

import asyncio
import os
import re
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from core.models import ChatRequest, ChatResponse, ComandoTexto
from services.stt import (
    STTConfigurationError,
    STTConnectionError,
    STTEmptyTranscriptionError,
    STTError,
    STTMalformedResponseError,
    STTProvider,
    STTProviderError,
    STTRateLimitError,
    STTTimeoutError,
    STTValidationError,
)

router = APIRouter(tags=["audio"])

import madeline_server as server


def get_stt_provider() -> STTProvider:
    """Resuelve la instancia del proveedor STT configurada en el servidor."""
    return server.obtener_stt_provider()


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
    origen: str = "pc",
    provider: STTProvider = Depends(get_stt_provider),
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
        filename = audio.filename or "audio.wav"
        mensaje_usuario = await provider.transcribir(
            audio_bytes=contenido,
            filename=filename,
            language="es",
            prompt=server.PISTA_VOCABULARIO,
            model=os.getenv("MADELINE_WHISPER_MODELO", "whisper-large-v3-turbo"),
        )
    except STTRateLimitError:
        return await server.responder_sin_cuota(origen)
    except STTValidationError as e:
        print(f"[ERROR WHISPER] Validación de audio fallida: {e}")
        raise HTTPException(status_code=400, detail=f"Audio inválido: {e.mensaje}")
    except STTTimeoutError as e:
        print(f"[ERROR WHISPER] Timeout en transcripción: {e}")
        raise HTTPException(
            status_code=504,
            detail="Error procesando el audio: tiempo de espera agotado.",
        )
    except STTConfigurationError as e:
        print(f"[ERROR WHISPER] Error de configuración: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error de configuración del servicio de audio: {e.mensaje}",
        )
    except STTConnectionError as e:
        print(f"[ERROR WHISPER] Error de conexión: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Error de conexión con el proveedor de audio: {e.mensaje}",
        )
    except STTEmptyTranscriptionError as e:
        print(f"[ERROR WHISPER] Transcripción vacía / sin voz: {e}")
        raise HTTPException(status_code=400, detail="No se detectó voz.")
    except STTMalformedResponseError as e:
        print(f"[ERROR WHISPER] Respuesta malformada: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error procesando el audio: {e.mensaje}",
        )
    except STTProviderError as e:
        print(f"[ERROR WHISPER] Error del proveedor: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error procesando el audio: {e.mensaje}",
        )
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR WHISPER] Error inesperado: {e}")
        raise HTTPException(status_code=500, detail=f"Error procesando el audio: {e}")

    mensaje_usuario = mensaje_usuario.strip()

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
