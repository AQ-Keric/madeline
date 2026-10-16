"""Palabra de activación: di "Madeline, …" y te escucha, como Alexa.

Escucha el micrófono todo el tiempo con Vosk (reconocimiento de voz
offline, ~8% de un núcleo): nada sale del PC hasta que oye "Madeline".
Entonces graba la orden hasta que te callas y la manda al mismo
/comando_audio que usa Super+J, así que la transcripción sigue siendo
la de Whisper.

Uso:  .venv/bin/python scripts/madeline_escucha.py
Pausar/reanudar: scripts/madeline_escucha_toggle.sh (Super+Shift+J).
"""
import io
import json
import os
import subprocess
import sys
import wave
from collections import deque

import requests
from vosk import KaldiRecognizer, Model, SetLogLevel

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MODELO = os.getenv(
    "MADELINE_VOSK_MODELO", os.path.join(RAIZ, "data", "modelos", "vosk-model-small-es-0.42")
)
URL_SERVIDOR = os.getenv("MADELINE_URL", "http://127.0.0.1:8000")
# MADELINE_ESCUCHA_DEBUG=1 imprime todo lo que Vosk entiende, para
# calibrar con tu voz. Apagado por defecto: no se registra lo que dices.
DEBUG = os.getenv("MADELINE_ESCUCHA_DEBUG", "") == "1"

# Mientras exista, no se escucha (lo crea/borra el toggle).
ARCHIVO_PAUSA = "/tmp/madeline_escucha_pausada"
# Lo crea madeline_mic.sh mientras grabas con Super+J: sin esto, decir
# "Madeline" en esa orden la mandaría dos veces.
ARCHIVO_MIC = "/tmp/madeline_mic.pid"
SONIDO_ACTIVACION = "/usr/share/sounds/freedesktop/stereo/message.oga"

TASA = 16000
BYTES_POR_SEGUNDO = TASA * 2
BLOQUE = 4000  # 0.125 s
# Se guarda el audio de la frase en curso (desde el último silencio,
# hasta este tope): así "Madeline, sube el volumen" dicho de corrido
# llega entero a Whisper aunque se detecte recién al final de la frase.
SEGUNDOS_PREVIOS = 10
# Si dices solo "Madeline" y haces una pausa, cuánto espera la orden.
SEGUNDOS_ESPERA_ORDEN = 6
SEGUNDOS_MAX_ORDEN = 15

# "madeleine" es como Vosk escribe "Madelain" (ma-de-lain).
PALABRAS_ACTIVACION = {"madeline", "madeleine"}
# Al cerrar cada frase se revisan las N transcripciones más probables,
# no solo la primera: con la voz de Keric "Madelain" a veces sale
# primero como "más de la" o "manuel hay", pero "madeleine" casi
# siempre está entre las alternativas. Calibrado con sus grabaciones:
# con 20 detecta 9/9 y 0 falsos en 111 frases parecidas. No cuesta CPU
# extra (las alternativas salen del mismo cálculo).
ALTERNATIVAS = 20


def avisar(texto: str, urgencia: str = "low", ms: int = 3000):
    subprocess.Popen(["notify-send", "Madeline", texto, "-t", str(ms), "-u", urgencia])


def tiene_palabra_activacion(texto: str) -> bool:
    return bool(PALABRAS_ACTIVACION & set(texto.split()))


def hay_orden(texto: str, frase_de_activacion: bool) -> bool:
    """Si el texto trae una orden además de "Madeline".

    En la frase donde se dijo "Madeline" solo cuenta lo que viene
    después: al cerrar la frase Vosk a veces corrige "madeline" por
    "madrid", y eso no es una orden."""
    palabras = texto.split()
    if frase_de_activacion:
        posiciones = [i for i, p in enumerate(palabras) if p in PALABRAS_ACTIVACION]
        palabras = palabras[posiciones[-1] + 1:] if posiciones else []
    return any(p not in PALABRAS_ACTIVACION and p != "oye" for p in palabras)


def a_wav(pcm: bytes) -> bytes:
    salida = io.BytesIO()
    with wave.open(salida, "wb") as archivo:
        archivo.setnchannels(1)
        archivo.setsampwidth(2)
        archivo.setframerate(TASA)
        archivo.writeframes(pcm)
    return salida.getvalue()


def enviar_orden(pcm: bytes):
    avisar("Procesando orden...", ms=2000)
    try:
        respuesta = requests.post(
            f"{URL_SERVIDOR}/comando_audio",
            files={"audio": ("orden.wav", a_wav(pcm), "audio/wav")},
            timeout=120,
        )
        datos = respuesta.json()
    except Exception as e:
        avisar(f"No pude contactar al servidor: {e}", "critical", 4000)
        return

    if datos.get("detail"):
        print(f"[!] {datos['detail']}", flush=True)
        avisar(str(datos["detail"]), "critical", 4000)
    elif datos.get("response"):
        print(f"[Madeline] {datos['response']}", flush=True)
        avisar(datos["response"], ms=6000)


def al_activarse():
    print("[*] ¡Madeline!", flush=True)
    # Se calla si estaba hablando, para que no se grabe su propia voz.
    try:
        requests.post(f"{URL_SERVIDOR}/callar", timeout=2)
    except requests.RequestException:
        pass
    if os.path.exists(SONIDO_ACTIVACION):
        subprocess.Popen(["paplay", SONIDO_ACTIVACION], stderr=subprocess.DEVNULL)
    avisar("🎙️ Te escucho...", ms=2000)


def escuchar():
    SetLogLevel(-1)
    if not os.path.isdir(MODELO):
        sys.exit(f"[!] Falta el modelo de Vosk en {MODELO} (ver README).")
    modelo = Model(MODELO)
    reconocedor = KaldiRecognizer(modelo, TASA)
    reconocedor.SetMaxAlternatives(ALTERNATIVAS)

    microfono = subprocess.Popen(
        ["parecord", "--raw", "--format=s16le", f"--rate={TASA}", "--channels=1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    print("[*] Escuchando la palabra de activación \"Madeline\"...", flush=True)

    previo = deque(maxlen=int(SEGUNDOS_PREVIOS * BYTES_POR_SEGUNDO / BLOQUE))
    grabando = False
    orden = bytearray()
    # Audio recibido desde "Madeline" (en bytes, no en reloj: así no
    # importa si el PC va atrasado procesando).
    bytes_desde_activacion = 0
    hubo_orden = False
    frase_de_activacion = False

    while True:
        bloque = microfono.stdout.read(BLOQUE)
        if not bloque:
            sys.exit("[!] Se cortó el micrófono.")

        if os.path.exists(ARCHIVO_PAUSA) or os.path.exists(ARCHIVO_MIC):
            if grabando or previo:
                grabando = False
                previo.clear()
                reconocedor.Reset()
            continue

        if grabando:
            orden.extend(bloque)
        else:
            previo.append(bloque)

        fin_de_frase = reconocedor.AcceptWaveform(bloque)
        if fin_de_frase:
            resultado = json.loads(reconocedor.Result())
            alternativas = [a["text"] for a in resultado.get("alternatives", [])]
            alternativas = alternativas or [resultado.get("text", "")]
        else:
            alternativas = [json.loads(reconocedor.PartialResult()).get("partial", "")]
        texto = alternativas[0]
        con_activacion = next((t for t in alternativas if tiene_palabra_activacion(t)), None)

        if DEBUG and fin_de_frase and texto:
            print(f"[oído] {texto}  (activa: {con_activacion})", flush=True)

        if not grabando:
            if not con_activacion:
                if fin_de_frase:
                    # Terminó una frase sin "Madeline": la próxima
                    # empieza de cero.
                    previo.clear()
                continue
            grabando = True
            bytes_desde_activacion = 0
            orden = bytearray(b"".join(previo))
            previo.clear()
            hubo_orden = False
            frase_de_activacion = True
            al_activarse()

        # Grabando la orden: termina cuando Vosk detecta el fin de la
        # frase. Si solo dijiste "Madeline", espera a la frase siguiente.
        if frase_de_activacion and con_activacion:
            texto = con_activacion
        if hay_orden(texto, frase_de_activacion):
            hubo_orden = True
        if fin_de_frase:
            frase_de_activacion = False
        bytes_desde_activacion += len(bloque)
        duracion = bytes_desde_activacion / BYTES_POR_SEGUNDO

        if hubo_orden and (fin_de_frase or duracion > SEGUNDOS_MAX_ORDEN):
            enviar_orden(bytes(orden))
        elif not hubo_orden and duracion > SEGUNDOS_ESPERA_ORDEN:
            avisar("No escuché ninguna orden.", ms=2000)
        else:
            continue

        grabando = False
        orden = bytearray()
        reconocedor.Reset()


if __name__ == "__main__":
    try:
        escuchar()
    except KeyboardInterrupt:
        pass
