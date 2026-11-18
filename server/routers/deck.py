"""Endpoints para el panel de Stream Deck en la app móvil."""

import os
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from core.models import AccionDeck

router = APIRouter(prefix="/deck", tags=["deck"])

import deck
import teclado_control
from services import hyprland
import madeline_server as server


@router.get("/apps")
def deck_apps_endpoint():
    """Las apps de DICCIONARIO_APPS con su nombre para mostrar."""
    return {"apps": deck.catalogo(server.DICCIONARIO_APPS, server.COMANDOS_APERTURA)}


@router.get("/icono/{app_id}")
def deck_icono_endpoint(app_id: str):
    if app_id not in server.APPS_PERMITIDAS:
        raise HTTPException(status_code=404, detail="Esa app no está en la lista.")
    ruta = deck.icono_png(app_id, server.COMANDOS_APERTURA.get(app_id, app_id))
    if not ruta:
        raise HTTPException(status_code=404, detail="Esa app no tiene ícono.")
    return FileResponse(ruta, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@router.get("/estado")
def deck_estado_endpoint():
    """Escritorios en uso, el activo, dispositivos Bluetooth y avatar."""
    escritorios, activo = hyprland.leer_escritorios()
    bluetooth = list({mac: nombre for nombre, mac in reversed(server.DISPOSITIVOS_BLUETOOTH.items())}.values())[::-1]
    return {
        "escritorios": escritorios,
        "activo": activo,
        "bluetooth": bluetooth,
        "avatar": os.path.exists(server.MARCA_AVATAR_ACTIVO),
    }


@router.post("/accion")
def deck_accion_endpoint(accion: AccionDeck):
    """Ejecuta una acción directa del deck sin llamar al LLM."""
    permitidos = server.DECK_ACCIONES.get(accion.tool)
    if permitidos is None:
        raise HTTPException(status_code=400, detail="Ese botón no existe.")
    if not set(accion.args) <= permitidos or not all(
        isinstance(v, (str, int)) for v in accion.args.values()
    ):
        raise HTTPException(status_code=400, detail="Argumentos inválidos para ese botón.")

    print(f"[🎛️ Deck] {accion.tool} {accion.args}")
    try:
        if accion.tool == "aplicar_tema_teclado":
            ok, msg = teclado_control.aplicar_tema(
                str(accion.args.get("tema", "")),
                brillo=int(accion.args["brillo"]) if "brillo" in accion.args else None,
                modelo_id=str(accion.args["modelo"]) if "modelo" in accion.args else None,
            )
            resultado = msg
        elif accion.tool == "apagar_teclado":
            ok, msg = teclado_control.apagar_luces()
            resultado = msg
        elif accion.tool == "control_energia":
            resultado = server.control_energia(**accion.args)
        else:
            funcion = server.TOOLS_FUNCIONES[accion.tool]
            resultado = funcion(**accion.args)
    except TypeError:
        raise HTTPException(status_code=400, detail="Faltan argumentos para ese botón.")

    image_path = None
    if str(resultado).startswith("CAPTURA_LISTA"):
        captura_id = getattr(resultado, "evidencia", {}).get("captura_id") or str(resultado).split(":", 1)[1]
        if server.ruta_captura_disponible(captura_id):
            image_path = f"/captura/{captura_id}"
        resultado = "Captura lista."
    return {"response": str(resultado), "image_path": image_path}
