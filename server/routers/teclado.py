"""Endpoints para control de iluminación del teclado RGB (Pegasum KAI) en el Deck."""

from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import teclado_control

router = APIRouter(prefix="/deck/teclado", tags=["teclado"])


class TecladoAplicar(BaseModel):
    tema: str
    brillo: Optional[int] = None
    modelo: Optional[str] = None


class TecladoFavoritos(BaseModel):
    favoritos: list[str]


class TecladoModelo(BaseModel):
    modelo: str


@router.get("/estado")
def deck_teclado_estado_endpoint():
    """Estado del teclado (modelo activo, tema activo, brillo, favoritos, conexión)."""
    return teclado_control.obtener_estado()


@router.get("/temas")
def deck_teclado_temas_endpoint():
    """Catálogo de los temas de iluminación con sus paletas de colores."""
    return {"temas": teclado_control.listar_temas()}


@router.post("/aplicar")
def deck_teclado_aplicar_endpoint(datos: TecladoAplicar):
    """Aplica un tema temático zonal al Pegasum KAI o perfil activo."""
    ok, msg = teclado_control.aplicar_tema(
        datos.tema, brillo=datos.brillo, modelo_id=datos.modelo
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "mensaje": msg, "estado": teclado_control.obtener_estado()}


@router.post("/favoritos")
def deck_teclado_favoritos_endpoint(datos: TecladoFavoritos):
    """Guarda la lista de temas favoritos fijados arriba en el celular."""
    ok, msg = teclado_control.guardar_favoritos(datos.favoritos)
    return {"ok": ok, "mensaje": msg}


@router.post("/modelo")
def deck_teclado_modelo_endpoint(datos: TecladoModelo):
    """Cambia el tipo o perfil de teclado seleccionado."""
    ok, msg = teclado_control.cambiar_modelo(datos.modelo)
    return {"ok": ok, "mensaje": msg}
