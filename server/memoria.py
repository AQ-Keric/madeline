"""Memoria a largo plazo de Madeline, 100% local: 0 tokens de Groq.

Los recuerdos van a un SQLite con un embedding calculado por Ollama
(embeddinggemma, en CPU: ~25 ms por frase y ~700 MB de RAM mientras
está cargado). Buscar es comparar vectores con numpy.

Si Ollama no responde, el recuerdo se guarda igual sin embedding y la
búsqueda cae a palabras clave: la memoria nunca rompe una respuesta.

Hay dos tipos de recuerdo:
- "dato": algo que Keric pidió recordar o que contó de sí mismo.
- "conversacion": una pregunta que fue a Groq con su respuesta.
"""
import os
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime

import numpy as np
import requests

RUTA_DB = os.getenv(
    "MADELINE_MEMORIA_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "memoria.db"),
)
OLLAMA_URL = os.getenv("MADELINE_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODELO_EMBED = os.getenv("MADELINE_MODELO_EMBED", "embeddinggemma")
# Cuánto queda el modelo en RAM después del último uso. Pasado ese
# tiempo Ollama lo descarga y la próxima búsqueda tarda ~1.5 s más.
KEEP_ALIVE = os.getenv("MADELINE_EMBED_KEEP_ALIVE", "10m")

# embeddinggemma rinde mejor con estos prefijos. Calibrado con frases
# reales: lo relacionado da 0.30-0.60 y lo que no tiene nada que ver
# ~0.10, así que 0.25 deja pasar solo lo que viene al caso.
PREFIJO_CONSULTA = "task: search result | query: "
PREFIJO_DOCUMENTO = "title: none | text: "
UMBRAL_RELEVANCIA = 0.25
UMBRAL_OLVIDAR = 0.35
UMBRAL_DUPLICADO = 0.90

# Las conversaciones de los últimos minutos ya van en historial_chat:
# repetirlas como "recuerdo" gastaría tokens dos veces.
SEGUNDOS_HISTORIAL_RECIENTE = 600

MAX_CARACTERES_RECUERDO = 160

_lock = threading.Lock()
_conexion = None
# Copia en RAM de los recuerdos con embedding, para no leer el SQLite
# entero en cada pregunta. Se invalida al guardar o borrar.
_cache = None


def _db() -> sqlite3.Connection:
    global _conexion
    if _conexion is None:
        os.makedirs(os.path.dirname(RUTA_DB), exist_ok=True)
        _conexion = sqlite3.connect(RUTA_DB, check_same_thread=False)
        _conexion.execute(
            "CREATE TABLE IF NOT EXISTS recuerdos ("
            " id INTEGER PRIMARY KEY,"
            " tipo TEXT NOT NULL,"
            " texto TEXT NOT NULL,"
            " embedding BLOB,"
            " creado REAL NOT NULL)"
        )
        _conexion.commit()
    return _conexion


def _embedding(texto: str, consulta: bool = False):
    """Vector normalizado del texto, o None si Ollama no está."""
    prefijo = PREFIJO_CONSULTA if consulta else PREFIJO_DOCUMENTO
    try:
        respuesta = requests.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": MODELO_EMBED, "input": prefijo + texto, "keep_alive": KEEP_ALIVE},
            timeout=5,
        )
        respuesta.raise_for_status()
        vector = np.array(respuesta.json()["embeddings"][0], dtype=np.float32)
    except Exception as e:
        print(f"[!] Memoria sin embeddings (¿Ollama apagado?): {e}")
        return None

    norma = np.linalg.norm(vector)
    return vector / norma if norma else None


def _cargar_cache():
    global _cache
    if _cache is None:
        filas = _db().execute(
            "SELECT id, tipo, texto, embedding, creado FROM recuerdos WHERE embedding IS NOT NULL"
        ).fetchall()
        if filas:
            matriz = np.vstack([np.frombuffer(f[3], dtype=np.float32) for f in filas])
        else:
            matriz = np.zeros((0, 0), dtype=np.float32)
        _cache = ([f[0] for f in filas], [f[1] for f in filas],
                  [f[2] for f in filas], [f[4] for f in filas], matriz)
    return _cache


def _similares(vector, tipos=None):
    """[(similitud, id, tipo, texto, creado)] de mayor a menor."""
    ids, tipos_fila, textos, creados, matriz = _cargar_cache()
    if not ids or matriz.shape[1] != vector.shape[0]:
        return []
    similitudes = matriz @ vector
    orden = np.argsort(-similitudes)
    return [
        (float(similitudes[i]), ids[i], tipos_fila[i], textos[i], creados[i])
        for i in orden
        if tipos is None or tipos_fila[i] in tipos
    ]


def _sin_tildes(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(c) != "Mn"
    )


def _palabras_clave(texto: str) -> set:
    return {p for p in re.findall(r"\w+", _sin_tildes(texto)) if len(p) >= 4}


def _por_palabras_clave(consulta: str, tipos=None):
    """Respaldo sin Ollama: [(coincidencias, id, tipo, texto, creado)]."""
    claves = _palabras_clave(consulta)
    if not claves:
        return []
    filas = _db().execute("SELECT id, tipo, texto, creado FROM recuerdos").fetchall()
    puntajes = []
    for id_, tipo, texto, creado in filas:
        if tipos and tipo not in tipos:
            continue
        coincidencias = len(claves & _palabras_clave(texto))
        if coincidencias:
            puntajes.append((coincidencias, id_, tipo, texto, creado))
    return sorted(puntajes, key=lambda p: (-p[0], -p[4]))


def guardar(texto: str, tipo: str = "dato") -> str:
    """Guarda un recuerdo. Un dato casi igual a otro lo reemplaza
    (ej: "mi hermana se llama Ana" dos veces no queda repetido)."""
    global _cache
    texto = texto.strip()
    vector = _embedding(texto)

    with _lock:
        db = _db()
        if vector is not None and tipo == "dato":
            parecidos = _similares(vector, tipos={"dato"})
            if parecidos and parecidos[0][0] >= UMBRAL_DUPLICADO:
                db.execute("DELETE FROM recuerdos WHERE id = ?", (parecidos[0][1],))

        db.execute(
            "INSERT INTO recuerdos (tipo, texto, embedding, creado) VALUES (?, ?, ?, ?)",
            (tipo, texto, vector.tobytes() if vector is not None else None, time.time()),
        )
        db.commit()
        _cache = None
    return texto


def guardar_en_segundo_plano(texto: str, tipo: str = "dato"):
    """Para no atrasar la respuesta hablada esperando a Ollama."""
    threading.Thread(target=guardar, args=(texto, tipo), daemon=True).start()


def guardar_conversacion(pregunta: str, respuesta: str):
    pregunta = pregunta.strip()[:150]
    respuesta = respuesta.strip()[:250]
    guardar_en_segundo_plano(f"Keric preguntó: {pregunta} — Madeline respondió: {respuesta}", "conversacion")


def buscar(consulta: str, k: int = 3) -> list:
    """Los k recuerdos más relevantes para la consulta (puede ser
    ninguno). Cada uno con su fecha, recortado para gastar poco."""
    limite_reciente = time.time() - SEGUNDOS_HISTORIAL_RECIENTE
    vector = _embedding(consulta, consulta=True)

    with _lock:
        if vector is not None:
            candidatos = [c for c in _similares(vector) if c[0] >= UMBRAL_RELEVANCIA]
        else:
            candidatos = _por_palabras_clave(consulta)

    elegidos = [
        c for c in candidatos
        if not (c[2] == "conversacion" and c[4] > limite_reciente)
    ][:k]
    return [_formatear(c[3], c[4]) for c in elegidos]


def _formatear(texto: str, creado: float) -> str:
    fecha = datetime.fromtimestamp(creado).strftime("%d/%m/%Y")
    if len(texto) > MAX_CARACTERES_RECUERDO:
        texto = texto[:MAX_CARACTERES_RECUERDO].rsplit(" ", 1)[0] + "…"
    return f"({fecha}) {texto}"


def contexto_recuerdos(consulta: str) -> str:
    """Mensaje de sistema con los recuerdos relevantes, o "" si no
    hay ninguno (y entonces no se gasta ni un token)."""
    recuerdos = buscar(consulta)
    if not recuerdos:
        return ""
    lineas = "\n".join(f"- {r}" for r in recuerdos)
    return f"Recuerdos de Keric (úsalos solo si vienen al caso):\n{lineas}"


def recuerdos_sobre(tema: str, k: int = 5) -> list:
    """Para "¿qué recuerdas de X?". Sin tema (o "de mí"), los últimos datos."""
    if not tema or _sin_tildes(tema).strip(" ?¿.") in ("mi", "mi mismo", "mi misma"):
        filas = _db().execute(
            "SELECT texto FROM recuerdos WHERE tipo = 'dato' ORDER BY creado DESC LIMIT ?", (k,)
        ).fetchall()
        return [f[0] for f in filas]

    vector = _embedding(tema, consulta=True)
    with _lock:
        if vector is not None:
            candidatos = [c for c in _similares(vector) if c[0] >= UMBRAL_RELEVANCIA]
        else:
            candidatos = _por_palabras_clave(tema)
    return [c[3] for c in candidatos[:k]]


def olvidar(tema: str):
    """Borra el recuerdo que mejor coincide con el tema. Devuelve su
    texto, o None si no había nada suficientemente parecido."""
    global _cache
    vector = _embedding(tema, consulta=True)

    with _lock:
        if vector is not None:
            candidatos = [c for c in _similares(vector) if c[0] >= UMBRAL_OLVIDAR]
        else:
            candidatos = _por_palabras_clave(tema)
        if not candidatos:
            return None

        _, id_, _, texto, _ = candidatos[0]
        borrar = [id_]

        # Las charlas que repiten ese dato ("¿qué fondo me recomiendas?"
        # -> "uno morado") también se van: si no, volvería a aparecer.
        claves = _palabras_clave(texto)
        minimo = min(2, len(claves))
        if minimo:
            for otro_id, otro_texto in _db().execute(
                "SELECT id, texto FROM recuerdos WHERE tipo = 'conversacion'"
            ):
                if len(claves & _palabras_clave(otro_texto)) >= minimo:
                    borrar.append(otro_id)

        _db().executemany("DELETE FROM recuerdos WHERE id = ?", [(i,) for i in set(borrar)])
        _db().commit()
        _cache = None
    return texto


def rellenar_embeddings():
    """Calcula el embedding de los recuerdos que se guardaron con
    Ollama apagado. Se llama al arrancar el servidor."""
    global _cache
    with _lock:
        pendientes = _db().execute(
            "SELECT id, texto FROM recuerdos WHERE embedding IS NULL"
        ).fetchall()

    for id_, texto in pendientes:
        vector = _embedding(texto)
        if vector is None:
            return
        with _lock:
            _db().execute("UPDATE recuerdos SET embedding = ? WHERE id = ?", (vector.tobytes(), id_))
            _db().commit()
            _cache = None


# ------------------------------------------------------------
# DATOS PERSONALES DICHOS AL PASAR (sin "recuerda que")
# ------------------------------------------------------------
# Solo frases en primera persona que casi siempre son un dato estable
# sobre Keric. Se guardan además de procesar el mensaje normalmente.
PATRONES_DATO_PERSONAL = [
    r"\bme\s+llamo\s+\w+",
    r"\bmi\s+(?:[\wáéíóúñ]+\s+){1,3}se\s+llama\s+\w+",
    r"\b(?:no\s+)?me\s+(?:gusta|gustan|encanta|encantan)\s+(?:el|la|los|las|mucho|ver|jugar|escuchar|comer|programar)\b.+",
    r"\b(?:odio|detesto|prefiero)\s+(?:el|la|los|las)\s+.+",
    r"\b(?:trabajo|estudio)\s+(?:en|como|de)\s+.+",
    r"\bestoy\s+(?:aprendiendo|estudiando|trabajando\s+en|haciendo\s+un|haciendo\s+una)\s+.+",
    r"\bvivo\s+en\s+.+",
    r"\bmi\s+cumpleaños\s+es\s+.+",
    r"\bnac[ií]\s+(?:el|en)\s+.+",
    r"\bsoy\s+al[eé]rgic[oa]\s+a\s+.+",
    r"\btengo\s+(?:un|una|dos|tres)\s+(?:perr[oa]|gat[oa]|herman[oa]|hij[oa]|novi[oa]|mascota)s?\b.*",
]
_PATRON_DATO_PERSONAL = re.compile("|".join(PATRONES_DATO_PERSONAL), re.IGNORECASE)


def dato_personal(texto: str):
    """La frase del mensaje que cuenta algo de Keric, o None."""
    # Se corta en oraciones, antes de cada "¿" y antes de ", mi/me/..."
    # para separar "estoy aprendiendo Flutter, ¿qué opinas?".
    separadores = (
        r"(?<=[.!;?])\s+|\s*,?\s*¿"
        r"|\s*,\s*(?:y\s+)?(?=(?:me|mi|trabajo|estudio|vivo|tengo|soy|estoy)\b)"
    )
    for frase in re.split(separadores, texto):
        frase = frase.strip(" .!;,")
        if not frase or "?" in frase or frase.startswith("¿"):
            continue
        if _PATRON_DATO_PERSONAL.search(frase):
            return frase
    return None
