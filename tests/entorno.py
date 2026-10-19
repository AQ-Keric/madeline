"""Entorno común de los tests: nada toca la memoria real ni Ollama.

Se importa antes que madeline_server/memoria en cada archivo de tests.
"""
import hashlib
import os
import re
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.setdefault("GROQ_API_KEY", "test")
_TMP = tempfile.mkdtemp(prefix="madeline_test_")
os.environ["MADELINE_MEMORIA_DB"] = os.path.join(_TMP, "memoria.db")
os.environ["MADELINE_LOG_SIN_TEMA"] = os.path.join(_TMP, "frases_sin_tema.log")
os.environ["MADELINE_LOG_CONVERSACIONES"] = os.path.join(_TMP, "conversaciones.jsonl")
os.environ["MADELINE_RECADOS"] = os.path.join(_TMP, "recados.json")
os.environ["MADELINE_GOOGLE_DIR"] = os.path.join(_TMP, "google")

import memoria  # noqa: E402


def embedding_falso(texto, consulta=False):
    """Bolsa de palabras: frases con palabras en común se parecen."""
    vector = np.zeros(4096, dtype=np.float32)
    for palabra in re.findall(r"\w{4,}", memoria._sin_tildes(texto)):
        vector[int(hashlib.md5(palabra.encode()).hexdigest(), 16) % 4096] += 1
    norma = np.linalg.norm(vector)
    return vector / norma if norma else None


memoria._embedding = embedding_falso


def vaciar_memoria():
    with memoria._lock:
        memoria._db().execute("DELETE FROM recuerdos")
        memoria._db().commit()
        memoria._cache = None


# En los tests se guarda en el acto, para no depender de hilos.
memoria.guardar_en_segundo_plano = memoria.guardar
