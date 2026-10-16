"""Graba muestras de tu voz diciendo "Madeline" para calibrar la
palabra de activación. Las guarda en data/calibracion/ (no se sube a
GitHub). Mientras graba, la escucha de Madeline queda en pausa.

Uso:  .venv/bin/python scripts/calibrar_activacion.py
"""
import os
import subprocess
import time

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CARPETA = os.path.join(RAIZ, "data", "calibracion")
ARCHIVO_PAUSA = "/tmp/madeline_escucha_pausada"
SONIDO = "/usr/share/sounds/freedesktop/stereo/message.oga"

TEXTO_NEGATIVO = (
    "Más de la mitad de la gente vino. La madera de la mesa está rayada. "
    "Mañana delineamos el plan. Magdalena fue al mercado. Mandé la línea de código. "
    "Me das la mermelada. Más de la cuenta. Mi mamá dejó la leche."
)


def avisar(texto: str):
    print(texto, flush=True)
    subprocess.run(["notify-send", "Calibración Madeline", texto, "-t", "4000"])


def grabar(nombre: str, segundos: float):
    if os.path.exists(SONIDO):
        subprocess.run(["paplay", SONIDO])
    ruta = os.path.join(CARPETA, nombre + ".raw")
    subprocess.run(
        ["timeout", str(segundos), "parecord", "--raw", "--format=s16le",
         "--rate=16000", "--channels=1", ruta],
        stderr=subprocess.DEVNULL,
    )


def main():
    os.makedirs(CARPETA, exist_ok=True)
    pausa_previa = os.path.exists(ARCHIVO_PAUSA)
    open(ARCHIVO_PAUSA, "w").close()

    try:
        avisar("Vamos a calibrar. Después de cada pitido, habla con tu voz normal.")
        time.sleep(3)

        for i in range(1, 7):
            avisar(f"[{i}/6] Di solo: \"Madelain\"")
            grabar(f"nombre_{i}", 2.5)
            time.sleep(0.8)

        for i, orden in enumerate(["pon música", "qué hora es", "muchas gracias"], 1):
            avisar(f"[{i}/3] Di: \"Madelain, {orden}\"")
            grabar(f"orden_{i}", 4)
            time.sleep(0.8)

        avisar(f"Último: lee en voz alta (sin decir Madelain):\n{TEXTO_NEGATIVO}")
        time.sleep(4)
        grabar("negativo", 20)

        avisar("¡Listo! Gracias. Ya puedes volver con Claude.")
    finally:
        if not pausa_previa and os.path.exists(ARCHIVO_PAUSA):
            os.remove(ARCHIVO_PAUSA)


if __name__ == "__main__":
    main()
