"""Recados: cosas que Keric no quiere olvidar ("recuérdame que debo X").

No tienen hora (para eso está el recordatorio con minutos o la agenda).
Se guardan en un JSON chico que también lee el widget de la barra
(noctalia/madeline-recados), que lo relee solo cada 2 segundos.
"""
import json
import os
import re
import unicodedata
from datetime import datetime

RUTA_RECADOS = os.getenv(
    "MADELINE_RECADOS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "recados.json"),
)
_PALABRAS_VACIAS = {
    "que", "debo", "tengo", "hay", "el", "la", "los", "las", "un", "una",
    "de", "del", "a", "al", "y", "mi", "mis", "lo", "recado", "recados",
}


def _sin_tildes(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(c) != "Mn"
    )


def _palabras(texto: str) -> set:
    return set(re.findall(r"\w{2,}", _sin_tildes(texto))) - _PALABRAS_VACIAS


def listar() -> list:
    try:
        with open(RUTA_RECADOS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _guardar(recados: list):
    os.makedirs(os.path.dirname(RUTA_RECADOS), exist_ok=True)
    # Escritura atómica: la barra puede estar leyendo el archivo justo ahora.
    temporal = RUTA_RECADOS + ".tmp"
    with open(temporal, "w", encoding="utf-8") as f:
        json.dump(recados, f, ensure_ascii=False, indent=1)
    os.replace(temporal, RUTA_RECADOS)


def agregar(texto: str) -> dict:
    recados = listar()
    recado = {
        "id": max((r["id"] for r in recados), default=0) + 1,
        "texto": texto.strip(" .¡!")[:1].upper() + texto.strip(" .¡!")[1:],
        "creado": datetime.now().isoformat(timespec="minutes"),
    }
    recados.append(recado)
    _guardar(recados)
    return recado


def borrar(id_recado: int) -> dict | None:
    recados = listar()
    restantes = [r for r in recados if r["id"] != id_recado]
    if len(restantes) == len(recados):
        return None
    _guardar(restantes)
    return next(r for r in recados if r["id"] == id_recado)


def buscar(descripcion: str) -> dict | None:
    """El recado que más palabras comparte con lo dicho ("ya compré el pan")."""
    buscadas = _palabras(descripcion)
    mejor, puntaje = None, 0
    for recado in listar():
        comunes = len(buscadas & _palabras(recado["texto"]))
        if comunes > puntaje:
            mejor, puntaje = recado, comunes
    return mejor


def borrar_todos() -> int:
    cantidad = len(listar())
    if cantidad:
        _guardar([])
    return cantidad
