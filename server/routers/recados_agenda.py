"""Endpoints para la gestión de recados rápidos y eventos de Google Calendar."""

from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import agenda
import recados

router = APIRouter(tags=["recados_agenda"])


class RecadoNuevo(BaseModel):
    texto: str


@router.get("/recados")
def recados_endpoint():
    return {"recados": recados.listar()}


@router.post("/recados")
def recados_agregar_endpoint(nuevo: RecadoNuevo):
    texto = nuevo.texto.strip()
    if not texto or len(texto) > 300:
        raise HTTPException(status_code=400, detail="El recado está vacío o es muy largo.")
    return recados.agregar(texto)


@router.delete("/recados/{id_recado}")
def recados_borrar_endpoint(id_recado: int):
    if recados.borrar(id_recado) is None:
        raise HTTPException(status_code=404, detail="Ese recado ya no está.")
    return {"ok": True}


@router.get("/agenda")
def agenda_endpoint(dias: int = 7):
    """Eventos desde hoy. Es 'def' para correr en threadpool sin bloquear async."""
    hoy = datetime.now().date()
    try:
        eventos = agenda.eventos_entre(hoy, hoy + timedelta(days=max(1, min(dias, 31))))
    except agenda.SinAgenda as e:
        return {"eventos": [], "error": str(e)}
    return {
        "eventos": [
            {"fecha": e["fecha"].isoformat(), "hora": e["hora"], "titulo": e["titulo"]}
            for e in eventos
        ],
        "error": None,
    }
