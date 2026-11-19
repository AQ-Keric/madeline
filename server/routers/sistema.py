"""Endpoints para sensores de hardware y capturas de pantalla / bandeja."""

import os
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse

from services import screen
from services import system

router = APIRouter(tags=["sistema"])


@router.get("/captura/{captura_id}")
async def obtener_captura(captura_id: str, background_tasks: BackgroundTasks):
    """Sirve la captura con ese ID específico."""
    ruta = screen.ruta_captura_disponible(captura_id)
    if not ruta:
        raise HTTPException(
            status_code=404,
            detail="Esa captura ya no está disponible (puede que haya expirado)."
        )

    media_type = "image/jpeg" if ruta.endswith(".jpg") else "image/png"
    background_tasks.add_task(os.remove, ruta)
    return FileResponse(ruta, media_type=media_type, background=background_tasks)


@router.get("/bandeja")
def bandeja_endpoint():
    """Capturas pedidas desde el PC que el celular todavía no bajó."""
    return {"capturas": [{"id": c["id"], "creada": c["creada"]} for c in screen.capturas_en_bandeja()]}


@router.get("/bandeja/{captura_id}")
def bandeja_captura_endpoint(captura_id: str, background_tasks: BackgroundTasks):
    """Entrega una captura de la bandeja y la borra (se baja una vez)."""
    captura = next((c for c in screen.capturas_en_bandeja() if c["id"] == captura_id), None)
    if not captura:
        raise HTTPException(status_code=404, detail="Esa captura ya no está en la bandeja.")
    background_tasks.add_task(os.remove, captura["ruta"])
    media_type = "image/jpeg" if captura["ruta"].endswith(".jpg") else "image/png"
    return FileResponse(captura["ruta"], media_type=media_type, background=background_tasks)


@router.get("/sensores")
def sensores_endpoint():
    """Estado de hardware en tiempo real para el panel del celular."""
    try:
        s = system.leer_sensores()
        return {
            "ram_usada": str(s["ram_usada"]),
            "ram_total": str(s["ram_total"]),
            "cpu_uso": str(s["cpu_uso"]),
            "cpu_temp": str(s["cpu_temp"]) if s["cpu_temp"] is not None else "N/A",
            "gpu_uso": s["gpu_uso"] or "0",
            "gpu_temp": s["gpu_temp"] or "N/A",
        }
    except Exception as e:
        return {"error": str(e)}
