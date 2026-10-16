import asyncio
import difflib
import os
import random
import re
import json
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import base64
import unicodedata
import uuid
from datetime import datetime
from typing import Optional
import edge_tts
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from groq import Groq, RateLimitError
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ============================================================
# CONFIGURACIÓN
# ============================================================

load_dotenv(override=True)

# Después de load_dotenv: memoria lee su configuración del .env.
import memoria  # noqa: E402

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise RuntimeError("Falta GROQ_API_KEY en el archivo .env")

client = Groq(api_key=GROQ_API_KEY)

# Modelo para lo que el router local no resuelve (preguntas libres,
# comandos ambiguos). Los comandos simples NO llegan a Groq.
MODEL = "openai/gpt-oss-120b"
# llama-3.3-70b-versatile fue dado de baja por Groq (anunciado el 17
# de junio de 2026). Este es el reemplazo que ellos mismos recomiendan
# para casos con function calling — sigue soportando tools igual.

# Último turno completo (pregunta + respuesta) más el mensaje actual.
# Con 2 se perdía la pregunta anterior y Groq veía solo su propia
# respuesta, así que las preguntas de seguimiento no funcionaban.
# Los comandos locales no usan esta memoria.
historial_chat = []
MAX_HISTORIAL = 3

# Memoria contextual ligera: no se envía a Groq salvo que una pregunta
# realmente necesite contexto.
memoria_contexto = {
    "cancion": None,
    "ultimo_resultado": None,
    # Acción destructiva esperando un "sí"/"no" del usuario antes de
    # ejecutarse (ver sección de energía en router_local). None = no
    # hay nada pendiente.
    "confirmacion_pendiente": None,
}

app = FastAPI(title="Madeline API", description="API para la asistente Madeline optimizada con Groq")

# Carpeta dedicada SOLO para audios que Madeline genera para el teléfono.
# Antes esto montaba /tmp completo — cualquiera que llegara al server
# podía pedir cualquier archivo temporal del sistema, no solo los
# audios de Madeline, con solo adivinar el nombre.
CARPETA_AUDIOS = "/tmp/madeline_audios"
os.makedirs(CARPETA_AUDIOS, exist_ok=True)
app.mount("/audios", StaticFiles(directory=CARPETA_AUDIOS), name="audios")

# ------------------------------------------------------------
# AUTENTICACIÓN
# ------------------------------------------------------------
# Sin esto, cualquiera en tu WiFi podía apagar el PC o sacar capturas
# de tu pantalla. Con MADELINE_TOKEN en el .env, toda petición que venga
# de la red tiene que traer el header X-Madeline-Token.
#
# Exentos:
# - lo que viene del mismo PC (madeline_mic.sh, la bienvenida de Hyprland),
# - /audios/ y /captura/: los nombres son aleatorios, duran pocos
#   minutos, y el reproductor de audio de la app no puede mandar headers.
MADELINE_TOKEN = os.getenv("MADELINE_TOKEN", "").strip()
RUTAS_SIN_TOKEN = ("/audios/", "/captura/")

if not MADELINE_TOKEN:
    print("[!] MADELINE_TOKEN no está en el .env: el servidor queda ABIERTO a toda la red local.")


@app.middleware("http")
async def verificar_token(request: Request, call_next):
    if not MADELINE_TOKEN:
        return await call_next(request)

    cliente = request.client.host if request.client else ""
    if cliente in ("127.0.0.1", "::1") or request.url.path.startswith(RUTAS_SIN_TOKEN):
        return await call_next(request)

    token = request.headers.get("x-madeline-token", "")
    if not secrets.compare_digest(token.encode(), MADELINE_TOKEN.encode()):
        print(f"[!] Petición rechazada sin token válido desde {cliente}: {request.url.path}")
        return JSONResponse(status_code=401, content={"detail": "Token de Madeline inválido."})

    return await call_next(request)

class ChatRequest(BaseModel):
    message: str

class ChatResponse(BaseModel):
    response: str
    image_path: Optional[str] = None
    audio_path: Optional[str] = None
    # Lo que entendió Whisper (solo en /comando_audio): la app lo
    # muestra en la burbuja del usuario.
    transcripcion: Optional[str] = None

class ComandoTexto(BaseModel):
    texto: str

# ============================================================
# HERRAMIENTAS / MÚSCULOS
# ============================================================

def ajustar_volumen(accion: str, porcentaje: int = 10, aplicacion: str = None) -> str:
    """Ajusta el volumen del sistema o de una aplicación específica."""
    try:
        if accion == "mutear":
            if aplicacion:
                subprocess.run(
                    ["pamixer", "--stream", "--toggle-mute",
                     "--sink-input", aplicacion],
                    check=True
                )
                return f"Se cambió el mute de la aplicación: {aplicacion}."
            else:
                subprocess.run(["pamixer", "-t"], check=True)
                return "Volumen general muteado o desmuteado."

        elif accion == "setear":
            if aplicacion:
                subprocess.run(
                    ["pamixer", "--stream", "--set-volume",
                     str(porcentaje), "--sink-input", aplicacion],
                    check=True
                )
                return f"Volumen fijado exactamente al {porcentaje}% en {aplicacion}."
            else:
                subprocess.run(
                    ["pamixer", "--set-volume", str(porcentaje)],
                    check=True
                )
                return f"Volumen general fijado exactamente al {porcentaje}%."

        elif accion == "subir":
            if aplicacion:
                subprocess.run(
                    ["pamixer", "--stream", "-i", str(porcentaje),
                     "--sink-input", aplicacion],
                    check=True
                )
                return f"Volumen subido un {porcentaje}% en {aplicacion}."
            else:
                subprocess.run(["pamixer", "-i", str(porcentaje)], check=True)
                return f"Volumen general subido un {porcentaje}%."

        elif accion == "bajar":
            if aplicacion:
                subprocess.run(
                    ["pamixer", "--stream", "-d", str(porcentaje),
                     "--sink-input", aplicacion],
                    check=True
                )
                return f"Volumen bajado un {porcentaje}% en {aplicacion}."
            else:
                subprocess.run(["pamixer", "-d", str(porcentaje)], check=True)
                return f"Volumen general bajado un {porcentaje}%."

        return "Acción no reconocida."

    except Exception as e:
        return f"Error ejecutando pamixer: {e}"


def cerrar_aplicacion(app_name: str) -> str:
    """Cierra una aplicación matando su proceso en Linux.

    Usa DICCIONARIO_APPS para resolver el nombre técnico (la misma
    fuente que abrir_aplicacion y mover_ventana, para no desincronizarse)
    y PROCESOS_CIERRE para los casos donde el proceso real a matar no
    es el mismo binario que se ejecutó al abrir (ej: Minecraft corre
    como "java" aunque se abra con "prismlauncher")."""
    nombre_tecnico = resolver_app(app_name)

    if not nombre_tecnico:
        return mensaje_app_desconocida("cerrar", app_name)

    procesos = PROCESOS_CIERRE.get(nombre_tecnico, [nombre_tecnico])

    cerrado_alguno = False

    for proceso in procesos:
        resultado = subprocess.run(
            ["pkill", "-i", "-f", proceso],
            stderr=subprocess.DEVNULL,
        )
        if resultado.returncode == 0:
            cerrado_alguno = True

    if cerrado_alguno:
        return f"Se cerró la aplicación {app_name}."

    return (
        f"Pucha, no pude cerrar {app_name} "
        f"(quizás ya estaba cerrada o el nombre del proceso es distinto)."
    )


def control_multimedia(accion: str) -> str:
    """Controla la reproducción de música."""
    # play/pause explícitos: antes ambos eran "play-pause" (alternar),
    # y "dale play" con la música sonando la pausaba.
    acciones_validas = {
        "pausar": ["pause"],
        "reproducir": ["play"],
        "siguiente": ["next"],
        "anterior": ["previous"],
        "reiniciar": ["position", "0"],
    }

    comando = acciones_validas.get(accion.lower())

    if not comando:
        return f"Error: La acción '{accion}' no es válida. No se ejecutó nada."

    try:
        if accion.lower() == "anterior":
            subprocess.run(["playerctl", "previous"], check=True)
            time.sleep(0.1)
            subprocess.run(["playerctl", "previous"], check=True)
            return "Se saltó a la canción anterior con doble toque."

        if accion.lower() == "reproducir" and not hay_reproductor():
            # "Ponle play" sin nada abierto: mejor arrancar la radio
            # que no hacer nada.
            return abrir_radio_musica()

        subprocess.run(["playerctl", *comando], check=True)
        return f"Se ejecutó la acción: {accion}."

    except Exception as e:
        return f"Pucha, no pude controlar la música: {e}"


def hay_reproductor() -> bool:
    """Si hay algún reproductor (Spotify, YouTube en el navegador...)
    que playerctl pueda controlar."""
    resultado = subprocess.run(
        ["playerctl", "status"], capture_output=True, text=True
    )
    return resultado.returncode == 0


def info_cancion() -> str:
    """Obtiene el nombre de la canción y artista actual."""
    try:
        resultado = subprocess.check_output(
            [
                "playerctl",
                "metadata",
                "--format",
                "{{ artist }} - {{ title }}"
            ]
        ).decode("utf-8").strip()

        if resultado and resultado != "-":
            return f"Actualmente está sonando: {resultado}"

        return "Al parecer no hay ninguna canción reproduciéndose."

    except Exception as e:
        return f"Pucha, no pude leer la canción: {e}"


# Radio fija de YouTube Music: en vez de buscar una canción puntual
# (que requería yt-dlp + rastrear ventanas de Hyprland para no
# acumular basura — mucha complejidad para el beneficio real), un
# pedido vago de música simplemente abre esta playlist/radio ya
# armada, como pestaña nueva en la ventana que ya tengas abierta.
RADIO_YOUTUBE_MUSIC = (
    # /watch?list=... (NO /playlist?list=...) carga directo a la
    # vista "reproduciendo ahora" y arranca con el primer tema. La
    # otra ("/playlist?...") es solo la lista para elegir a mano.
    "https://music.youtube.com/watch?list=RDATjuUCejTNnQHVNBGyyt11LdQytQ"
)


def abrir_radio_musica() -> str:
    """Abre la radio de YouTube Music en una pestaña nueva, reusando
    la ventana existente del navegador (no abre una ventana aparte)."""
    try:
        # Pausa lo que ya estuviera sonando, para no tener dos cosas
        # sonando encimadas apenas cargue la pestaña nueva.
        subprocess.run(
            ["playerctl", "-a", "pause"],
            stderr=subprocess.DEVNULL,
        )

        abrir_url_en_navegador(RADIO_YOUTUBE_MUSIC)

        return "Abriendo tu radio en YouTube Music"

    except Exception as e:
        return f"No pude abrir la radio: {e}"


def _reemplazar_proceso_madeline():
    print("[*] Reiniciando el proceso de Madeline...", flush=True)
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])


def reiniciar_servidor_madeline() -> str:
    """Reinicia el PROCESO de Madeline (no el sistema operativo), para
    aplicar cambios de código sin necesitar una terminal.

    Antes usaba systemctl --user restart madeline-server, pero ese
    servicio no existe: Madeline arranca con un exec-once de Hyprland.
    os.execv reemplaza este proceso por uno nuevo con el código
    actualizado (mismo PID, así que sigue colgado del exec-once).
    Se espera unos segundos para que alcance a responder y hablar."""
    threading.Timer(6, _reemplazar_proceso_madeline).start()
    return (
        "Reiniciando el servidor de Madeline. "
        "Va a tardar unos segundos en volver."
    )


def control_energia(accion: str) -> str:
    """Apaga o reinicia el sistema operativo."""
    try:
        if accion == "apagar":
            subprocess.Popen(["systemctl", "poweroff"])
            return "Iniciando secuencia de apagado completo."

        elif accion == "reiniciar":
            subprocess.Popen(["systemctl", "reboot"])
            return "Reiniciando el sistema."

        return "Acción de energía no reconocida."

    except Exception as e:
        return f"Error intentando cambiar el estado de energía: {e}"


def gestor_paquetes_disponible() -> str:
    """Detecta qué gestor de paquetes AUR-aware hay instalado.
    En CachyOS/Arch suele venir paru o yay; si no hay ninguno,
    cae a pacman a secas (sin soporte AUR)."""
    if shutil.which("paru"):
        return "paru"
    if shutil.which("yay"):
        return "yay"
    return "pacman"


def actualizar_sistema() -> str:
    """Dispara la actualización completa del sistema — el equivalente
    por voz al botón de actualizar de CachyOS. La corre en una
    terminal aparte (con --hold, para que no se cierre sola) en vez
    de en background silencioso, porque:
    1. sudo puede pedir contraseña, y necesita una terminal interactiva.
    2. el usuario puede querer ver qué se está actualizando o si algo
       falla, en vez de enterarse recién cuando termina."""
    try:
        gestor = gestor_paquetes_disponible()
        terminal = DICCIONARIO_APPS.get("terminal", "kitty")

        comando = (
            "sudo pacman -Syu" if gestor == "pacman"
            else f"{gestor} -Syu"
        )

        subprocess.Popen([terminal, "--hold", "-e", "bash", "-c", comando])

        return (
            f"Actualización del sistema iniciada con {gestor} en una "
            f"terminal aparte. Puede que te pida tu contraseña."
        )

    except Exception as e:
        return f"No pude iniciar la actualización: {e}"


def verificar_actualizaciones() -> str:
    """Cuenta cuántos paquetes tienen actualización pendiente, SIN
    aplicarlas — para responder '¿tengo actualizaciones pendientes?'
    sin disparar la actualización completa.

    checkupdates solo mira los repos oficiales, así que los paquetes
    del AUR se cuentan aparte con paru/yay -Qua."""
    def contar(comando: list) -> int:
        resultado = subprocess.run(
            comando, capture_output=True, text=True, timeout=30
        )
        return len([l for l in resultado.stdout.splitlines() if l.strip()])

    try:
        gestor = gestor_paquetes_disponible()

        if shutil.which("checkupdates"):
            oficiales = contar(["checkupdates"])
        else:
            oficiales = contar(["pacman", "-Qu"])

        aur = contar([gestor, "-Qua"]) if gestor != "pacman" else 0

        if not oficiales and not aur:
            return "El sistema ya está al día, no hay actualizaciones pendientes."

        texto = f"Hay {oficiales + aur} paquetes con actualización pendiente"
        if aur:
            texto += f", {aur} de ellos del AUR"
        return texto + "."

    except subprocess.TimeoutExpired:
        return "Se tardó demasiado revisando actualizaciones."
    except Exception as e:
        return f"No pude revisar las actualizaciones: {e}"


def segundos_desde_unidad(cantidad: int, unidad: str) -> int:
    """Convierte 'cantidad' + 'unidad' (minutos/horas/segundos) a segundos."""
    if unidad.startswith("hora"):
        return cantidad * 3600
    if unidad.startswith("minuto"):
        return cantidad * 60
    return cantidad


async def recordatorio_en_segundo_plano(segundos: int, mensaje: str):
    """Espera y después habla el recordatorio por los parlantes locales."""
    await asyncio.sleep(segundos)
    texto = f"Keric, te recuerdo: {mensaje}."
    print(f"[RECORDATORIO] {texto}")
    await hablar_por_parlantes(texto)


# asyncio solo guarda referencias DÉBILES a las tareas: sin este set,
# el recolector de basura podía borrar un recordatorio antes de que
# sonara.
RECORDATORIOS_ACTIVOS = set()


def programar_recordatorio(cantidad: int, unidad: str, mensaje: str) -> str:
    """Programa el recordatorio en background y confirma de inmediato."""
    segundos = segundos_desde_unidad(cantidad, unidad)
    tarea = asyncio.create_task(recordatorio_en_segundo_plano(segundos, mensaje))
    RECORDATORIOS_ACTIVOS.add(tarea)
    tarea.add_done_callback(RECORDATORIOS_ACTIVOS.discard)
    return f"Recordatorio programado para dentro de {cantidad} {unidad}: {mensaje}."


APPS_CERRAR_TODO = ["navegador", "minecraft", "bottles"]


def cerrar_todo() -> str:
    """Cierra navegador y juegos de una pasada, reusando
    cerrar_aplicacion (y por lo tanto PROCESOS_CIERRE) en vez de
    mantener una lista de procesos aparte."""
    cerradas = [
        app for app in APPS_CERRAR_TODO
        if cerrar_aplicacion(app).startswith("Se cerró")
    ]

    if not cerradas:
        return "No había nada que cerrar."

    return f"Protocolo de limpieza ejecutado. Cerré: {', '.join(cerradas)}."


URL_CLIMA = "https://wttr.in/Villa+Alegre,Chile?format=j1&lang=es"

# wttr.in da pronóstico de hoy + 2 días. Con lang=es la condición ya
# viene en español (campo "lang_es"), así que no hace falta un
# diccionario de traducción propio.
DIAS_PRONOSTICO = {"hoy": 0, "mañana": 1, "pasado mañana": 2}


def _condicion_es(bloque: dict) -> str:
    try:
        return bloque["lang_es"][0]["value"].strip().lower()
    except (KeyError, IndexError):
        return bloque["weatherDesc"][0]["value"].strip().lower()


def reporte_clima(dia: str = "ahora") -> str:
    """Clima de Villa Alegre: el actual ("ahora") o el pronóstico de
    "hoy", "mañana" o "pasado mañana" (mínima, máxima y probabilidad
    de lluvia). Todo local, sin gastar tokens de Groq."""
    try:
        respuesta = requests.get(URL_CLIMA, timeout=8)

        if respuesta.status_code != 200:
            return "No pude conectar con el satélite del clima."

        datos = respuesta.json()
        dia = (dia or "ahora").strip().lower().replace("_", " ")

        if dia not in DIAS_PRONOSTICO:
            actual = datos["current_condition"][0]
            return (
                f"Ahora mismo en Villa Alegre: {_condicion_es(actual)}, "
                f"con {actual['temp_C']} grados."
            )

        pronostico = datos["weather"][DIAS_PRONOSTICO[dia]]
        horas = pronostico["hourly"]
        # El bloque de las 12:00 representa bien "cómo va a estar el día".
        mediodia = horas[len(horas) // 2]
        lluvia = max(int(h.get("chanceofrain", 0)) for h in horas)

        texto = (
            f"Para {dia} en Villa Alegre: {_condicion_es(mediodia)}, "
            f"mínima de {pronostico['mintempC']} y máxima de "
            f"{pronostico['maxtempC']} grados."
        )

        if lluvia >= 20:
            texto += f" Probabilidad de lluvia de hasta {lluvia}%."
        else:
            texto += " No se espera lluvia."

        return texto

    except Exception as e:
        return f"Error consultando el clima: {e}"


# ------------------------------------------------------------
# SENSORES (compartido por estado_sistema y /sensores)
# ------------------------------------------------------------
# Se leen /proc y /sys directo en vez de "free | awk", "top -bn1" y
# "cat" con shell=True: es más rápido (top tardaba ~0.5 s en cada
# consulta del celular, cada 2 s) y más preciso (el primer muestreo
# de top -bn1 no refleja bien el uso actual).

_MUESTRA_CPU = None  # (momento, total, inactivo) de la lectura anterior


def _leer_proc_stat():
    with open("/proc/stat") as f:
        # Solo los 8 primeros campos: guest y guest_nice ya vienen
        # incluidos dentro de user y nice.
        valores = [int(x) for x in f.readline().split()[1:9]]
    # idle + iowait cuentan como tiempo inactivo
    return sum(valores), valores[3] + valores[4]


def uso_cpu() -> float:
    """% de uso de CPU desde la lectura anterior. Si no hay una
    lectura reciente (< 10 s), mide durante 0.2 s."""
    global _MUESTRA_CPU

    ahora = time.monotonic()
    total, inactivo = _leer_proc_stat()

    if _MUESTRA_CPU is None or ahora - _MUESTRA_CPU[0] > 10:
        time.sleep(0.2)
        total_previo, inactivo_previo = total, inactivo
        ahora = time.monotonic()
        total, inactivo = _leer_proc_stat()
    else:
        _, total_previo, inactivo_previo = _MUESTRA_CPU

    _MUESTRA_CPU = (ahora, total, inactivo)

    delta_total = total - total_previo
    if delta_total <= 0:
        return 0.0
    return round(100 * (1 - (inactivo - inactivo_previo) / delta_total), 1)


def leer_sensores() -> dict:
    """RAM (MB), CPU (% y °C) y GPU NVIDIA (% y °C, None si está
    apagada por el sistema híbrido o no hay nvidia-smi)."""
    memoria = {}
    with open("/proc/meminfo") as f:
        for linea in f:
            clave, valor = linea.split(":", 1)
            memoria[clave] = int(valor.split()[0])  # en kB

    ram_total = memoria["MemTotal"] // 1024
    ram_usada = ram_total - memoria["MemAvailable"] // 1024

    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            cpu_temp = int(f.read().strip()) // 1000
    except Exception:
        cpu_temp = None

    try:
        gpu_info = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip()
        gpu_uso, gpu_temp = [x.strip() for x in gpu_info.split(",")]
    except Exception:
        gpu_uso, gpu_temp = None, None

    return {
        "ram_usada": ram_usada,
        "ram_total": ram_total,
        "cpu_uso": uso_cpu(),
        "cpu_temp": cpu_temp,
        "gpu_uso": gpu_uso,
        "gpu_temp": gpu_temp,
    }


def _gb(megas: int) -> str:
    # Con coma decimal, para que la voz lo lea "cinco coma dos".
    return f"{megas / 1024:.1f}".replace(".", ",")


def estado_sistema() -> str:
    """Lee la memoria RAM, porcentaje de CPU y temperaturas para que Madeline hable."""
    try:
        s = leer_sensores()

        cpu_temp = f"{s['cpu_temp']}°C" if s["cpu_temp"] is not None else "desconocida"

        if s["gpu_uso"] is not None:
            info_gpu = (
                f"La tarjeta gráfica NVIDIA está al {s['gpu_uso']}% "
                f"de uso con {s['gpu_temp']}°C."
            )
        else:
            info_gpu = "La tarjeta de video dedicada está en reposo."

        return (
            f"Estado actual: RAM {_gb(s['ram_usada'])} gigas ocupadas de "
            f"{_gb(s['ram_total'])}. Procesador al {s['cpu_uso']:.0f}% "
            f"con una temperatura de {cpu_temp}. {info_gpu}"
        )

    except Exception as e:
        return f"No pude leer los sensores: {e}"


def tomar_captura(ancho_max: int = 1920) -> str:
    """Saca la captura usando grim en Hyprland y la comprime a JPEG.

    Cada captura se guarda con un ID único (uuid) en vez de un nombre
    fijo, para que dos solicitudes simultáneas (PC + teléfono al
    mismo tiempo) no se pisen la captura una a la otra. El ID viaja
    codificado en el resultado como "CAPTURA_LISTA:<id>"."""
    captura_id = uuid.uuid4().hex[:12]
    ruta_png = f"/tmp/captura_madeline_{captura_id}.png"

    try:
        subprocess.run(
            ["grim", ruta_png],
            check=True,
            timeout=10,
        )

        try:
            from PIL import Image

            ruta_jpg = f"/tmp/captura_madeline_{captura_id}.jpg"

            with Image.open(ruta_png) as img:
                img = img.convert("RGB")

                if img.width > ancho_max:
                    ratio = ancho_max / img.width
                    img = img.resize(
                        (ancho_max, int(img.height * ratio))
                    )

                img.save(
                    ruta_jpg,
                    "JPEG",
                    quality=85,
                    optimize=True,
                )

            os.remove(ruta_png)

        except Exception as e:
            print(
                f"[!] No se pudo comprimir la captura, "
                f"se enviará el PNG original: {e}"
            )

        return f"CAPTURA_LISTA:{captura_id}"

    except Exception as e:
        return f"Falló el módulo de captura de pantalla: {e}"


def ruta_captura_disponible(captura_id: str) -> str | None:
    """Devuelve la ruta de la captura con ese ID, priorizando el JPEG."""
    ruta_jpg = f"/tmp/captura_madeline_{captura_id}.jpg"
    ruta_png = f"/tmp/captura_madeline_{captura_id}.png"

    if os.path.exists(ruta_jpg):
        return ruta_jpg
    if os.path.exists(ruta_png):
        return ruta_png
    return None


# Modelo con visión de Groq (actual al momento de escribir esto —
# ver https://console.groq.com/docs/vision si cambia). Es una
# llamada APARTE del modelo de texto normal (MODEL): manda una
# imagen, así que necesita su propio modelo compatible.
MODEL_VISION = "qwen/qwen3.6-27b"

# ID de la captura de la última llamada a analizar_pantalla(), para
# que procesar_mensaje pueda devolverla como imagen junto con la
# respuesta hablada. None = no hay ninguna pendiente de devolver.
ULTIMA_CAPTURA_ANALISIS = None


def recortar_a_oracion_completa(texto: str) -> str:
    """Si la respuesta se cortó a mitad de una oración (se acabó
    max_completion_tokens antes de terminar la idea), la recorta
    hasta el último punto/cierre completo — mejor terminar una frase
    antes que se pueda, que leer en voz alta algo que corta a mitad
    de palabra."""
    if not texto:
        return texto

    ultimo_cierre = max(
        texto.rfind("."), texto.rfind("!"), texto.rfind("?")
    )

    # Si ni siquiera llegó a cerrar una primera oración decente,
    # mejor dejar el texto entero que devolver casi nada.
    if ultimo_cierre >= 20:
        return texto[:ultimo_cierre + 1].strip()

    return texto.strip()


def limpiar_texto_para_voz(texto: str) -> str:
    """Defensa universal: saca markdown de CUALQUIER respuesta antes
    de hablarla (tablas, negritas, encabezados, viñetas). El prompt
    ya le pide al modelo que no use nada de esto, pero como con el
    razonamiento de qwen, a veces igual se cuela — mejor no confiar
    solo en la instrucción."""
    if not texto:
        return texto

    lineas = texto.split("\n")
    lineas_limpias = []

    for linea in lineas:
        # Líneas separadoras de tabla markdown (|---|---|---|) se
        # descartan directo, no aportan nada en voz.
        if "|" in linea and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", linea):
            continue

        # Filas de tabla ("| A | B | C |") -> texto separado por comas
        if linea.count("|") >= 2:
            celdas = [c.strip() for c in linea.split("|") if c.strip()]
            if celdas:
                linea = ", ".join(celdas) + "."

        lineas_limpias.append(linea)

    texto = "\n".join(lineas_limpias)

    # Encabezados (## Título) -> el texto solo, sin los #
    texto = re.sub(r"^#{1,6}\s*", "", texto, flags=re.MULTILINE)
    # Negritas / cursivas
    texto = re.sub(r"\*\*(.+?)\*\*", r"\1", texto)
    texto = re.sub(r"\*(.+?)\*", r"\1", texto)
    texto = re.sub(r"__(.+?)__", r"\1", texto)
    # Viñetas de lista ("- item" / "* item")
    texto = re.sub(r"^\s*[-*]\s+", "", texto, flags=re.MULTILINE)

    # Colapsar saltos de línea y espacios repetidos — total, se
    # habla todo seguido, no se ve el formato.
    texto = re.sub(r"\n{2,}", ". ", texto)
    texto = re.sub(r"\n", " ", texto)
    texto = re.sub(r"\s{2,}", " ", texto)

    return texto.strip()


def limpiar_respuesta_vision(texto: str) -> str:
    """Defensa extra sobre lo que devuelve el modelo de visión:

    1. qwen3.6-27b es un modelo de razonamiento con modo 'thinking' —
       aunque se lo pida en español y breve, a veces manda todo su
       razonamiento envuelto en <think>...</think> antes de la
       respuesta final. Lo sacamos igual, pase lo que pase con
       reasoning_effort.
    2. El resto del markdown (tablas, negritas, etc.) lo maneja
       limpiar_texto_para_voz, la misma limpieza que se le aplica a
       cualquier respuesta antes de hablarla — sin duplicar lógica acá.
    """
    # Bloque de razonamiento completo (abre y cierra)
    texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.DOTALL | re.IGNORECASE)

    # Si queda un <think> SIN cerrar, la respuesta se cortó a mitad
    # del razonamiento (se acabó max_completion_tokens antes de
    # llegar a la respuesta real) — no hay nada aprovechable ahí,
    # tratamos todo como vacío en vez de leer el razonamiento crudo.
    if re.search(r"<think>", texto, flags=re.IGNORECASE):
        return ""

    return limpiar_texto_para_voz(texto)


def analizar_pantalla(pregunta: str) -> str:
    """Toma una captura de pantalla y la manda, junto con la
    pregunta, a un modelo con visión — para cosas tipo '¿qué error es
    este?' o '¿qué aplicación tengo abierta?'.

    Es una llamada A PARTE del flujo normal de router_local/Groq: no
    pasa por tools_necesarias ni por el modelo de texto de siempre,
    porque necesita mandar una imagen. Cuando se dispara por el
    patrón local (extraer_pedido_analisis_pantalla), es UNA sola
    llamada a Groq. Cuando la dispara Groq como herramienta libre,
    son dos (la que decide llamar la herramienta + esta), como
    cualquier otra tool — nada fuera de lo normal.

    A diferencia de antes, NO borra la captura al terminar: la deja
    disponible para que procesar_mensaje también te la devuelva como
    imagen (ver ULTIMA_CAPTURA_ANALISIS más abajo), igual que ya hace
    el comando de captura normal."""
    global ULTIMA_CAPTURA_ANALISIS
    ULTIMA_CAPTURA_ANALISIS = None

    # 1280 px en vez de 1920: el modelo de visión cobra por
    # resolución, así gasta ~la mitad de tokens y el texto en
    # pantalla se sigue leyendo bien.
    resultado_captura = tomar_captura(ancho_max=1280)

    if not resultado_captura.startswith("CAPTURA_LISTA"):
        return resultado_captura

    captura_id = resultado_captura.split(":", 1)[1]
    ruta = ruta_captura_disponible(captura_id)

    if not ruta:
        return "No pude tomar la captura para analizarla."

    try:
        with open(ruta, "rb") as f:
            imagen_b64 = base64.b64encode(f.read()).decode("utf-8")

        extension = "jpeg" if ruta.endswith(".jpg") else "png"

        instruccion = (
            "Respondé SIEMPRE en español, en una o dos frases breves, "
            "en texto plano sin markdown (sin asteriscos ni negritas) "
            "porque tu respuesta se va a leer en voz alta. Pregunta: "
        )

        respuesta = client.chat.completions.create(
            model=MODEL_VISION,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruccion + pregunta},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/{extension};base64,{imagen_b64}"
                            },
                        },
                    ],
                }
            ],
            temperature=0.3,
            max_completion_tokens=300,
            # Apaga el modo "thinking" de Qwen — sin esto, a veces
            # manda todo el razonamiento en vez de solo la respuesta.
            reasoning_effort="none",
        )

        texto_respuesta = limpiar_respuesta_vision(
            respuesta.choices[0].message.content
        )

        if not texto_respuesta:
            return (
                "El modelo se quedó pensando y no llegó a responder a "
                "tiempo. Probá de nuevo, a veces con la segunda pasada "
                "alcanza."
            )

        # Guardamos el ID para que procesar_mensaje devuelva también la
        # foto analizada. El archivo se borra recién cuando el
        # cliente la pida por /captura/{id} (mismo mecanismo que la
        # captura normal, ver /captura/{captura_id} más abajo).
        ULTIMA_CAPTURA_ANALISIS = captura_id

        return texto_respuesta

    except Exception as e:
        print(f"[ERROR VISION] {e}")
        # Acá sí falló de verdad y no hay nada que mostrar — borramos
        # la captura para no dejar basura en /tmp.
        if os.path.exists(ruta):
            os.remove(ruta)
        return f"No pude analizar la pantalla: {e}"


# Capturas pedidas desde el PC (voz o Super+J): quedan en esta bandeja
# hasta que la app del celular las descarga. Reemplaza a Telegram.
CARPETA_BANDEJA = "/tmp/madeline_bandeja"
os.makedirs(CARPETA_BANDEJA, exist_ok=True)
HORAS_VIDA_BANDEJA = 24
PATRON_ID_CAPTURA = re.compile(r"^[0-9a-f]{12}$")


def enviar_captura_al_telefono(captura_id: str):
    """Deja la captura en la bandeja para que la app la descargue."""
    ruta = ruta_captura_disponible(captura_id)
    if not ruta:
        return
    destino = os.path.join(CARPETA_BANDEJA, captura_id + os.path.splitext(ruta)[1])
    os.replace(ruta, destino)
    print(f"[*] Captura {captura_id} en la bandeja del celular.")


def capturas_en_bandeja() -> list:
    archivos = []
    for nombre in os.listdir(CARPETA_BANDEJA):
        captura_id, _ = os.path.splitext(nombre)
        if PATRON_ID_CAPTURA.match(captura_id):
            ruta = os.path.join(CARPETA_BANDEJA, nombre)
            archivos.append({"id": captura_id, "creada": os.path.getmtime(ruta), "ruta": ruta})
    return sorted(archivos, key=lambda a: a["creada"])


HEADERS_BUSQUEDA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def buscar_en_web(consulta: str) -> str:
    """Busca información en internet usando DuckDuckGo HTML."""
    try:
        print(f"[*] Madeline está buscando en la web: {consulta}")

        query = urllib.parse.quote(consulta)
        url = f"https://html.duckduckgo.com/html/?q={query}"

        respuesta = requests.get(
            url,
            headers=HEADERS_BUSQUEDA,
            timeout=10
        )

        soup = BeautifulSoup(respuesta.text, "html.parser")
        resultados = soup.find_all(
            "a",
            class_="result__snippet",
            limit=3
        )

        if not resultados:
            return "No encontré información reciente sobre eso en internet."

        resumen = f"Resultados web para '{consulta}':\n"

        for i, res in enumerate(resultados):
            texto_limpio = res.text.strip()
            resumen += f"{i + 1}. {texto_limpio}\n"

        print("[*] Éxito en la búsqueda web.")
        return resumen

    except Exception as e:
        error_msg = f"Error crítico en el módulo de búsqueda: {e}"
        print(f"[*] {error_msg}")
        return error_msg


def buscar_urls_duckduckgo(consulta: str, cantidad: int = 3) -> list:
    """Busca en DuckDuckGo HTML y devuelve las URLs REALES de los
    primeros resultados (a diferencia de buscar_en_web, que devuelve
    texto para que Madeline lo lea, esto devuelve links para abrir)."""
    try:
        query = urllib.parse.quote(consulta)
        url = f"https://html.duckduckgo.com/html/?q={query}"

        respuesta = requests.get(url, headers=HEADERS_BUSQUEDA, timeout=10)
        soup = BeautifulSoup(respuesta.text, "html.parser")
        enlaces = soup.find_all("a", class_="result__a", limit=cantidad)

        urls = []

        for enlace in enlaces:
            href = enlace.get("href", "")

            # DuckDuckGo a veces envuelve el link real en un redirect
            # propio (/l/?uddg=<url codificada>) — hay que desenvolverlo.
            if "uddg=" in href:
                partes = urllib.parse.urlparse(href)
                qs = urllib.parse.parse_qs(partes.query)
                href = qs.get("uddg", [href])[0]

            if href.startswith("http"):
                urls.append(href)

        return urls

    except Exception as e:
        print(f"[!] Error buscando URLs en DuckDuckGo: {e}")
        return []


def buscar_urls_arxiv(consulta: str, cantidad: int = 3) -> list:
    """Busca papers en arXiv y devuelve las URLs de los primeros
    resultados. arXiv es HTML simple sin JS, fácil y estable de leer
    a diferencia de Google Scholar (que bloquea scraping seguido)."""
    try:
        query = urllib.parse.quote(consulta)
        url = f"https://arxiv.org/search/?query={query}&searchtype=all"

        respuesta = requests.get(url, headers=HEADERS_BUSQUEDA, timeout=10)
        soup = BeautifulSoup(respuesta.text, "html.parser")
        items = soup.find_all("p", class_="list-title", limit=cantidad)

        urls = []

        for item in items:
            enlace = item.find("a")
            if enlace and enlace.get("href", "").startswith("http"):
                urls.append(enlace["href"])

        return urls

    except Exception as e:
        print(f"[!] Error buscando en arXiv: {e}")
        return []


def abrir_url_en_navegador(url: str) -> bool:
    """Abre UNA URL como pestaña nueva en la ventana ya abierta del
    navegador ("-new-tab", no una ventana aparte).

    hyprctl dispatch exec le pasa su argumento a una shell, y las URLs
    vienen de resultados de búsqueda (o sea, de páginas ajenas): una
    URL con comillas o $(...) podía ejecutar comandos. Por eso solo se
    aceptan http/https y la URL va escapada con shlex.quote."""
    if not url.startswith(("http://", "https://")):
        print(f"[!] URL descartada (no es http/https): {url}")
        return False

    nombre_tecnico = DICCIONARIO_APPS.get("navegador", "zen-browser")
    subprocess.Popen([
        "hyprctl", "dispatch", "exec",
        f"{nombre_tecnico} -new-tab {shlex.quote(url)}",
    ])
    return True


def abrir_pestanas_navegador(urls: list):
    """Abre cada URL como pestaña nueva, en la ventana que ya está
    abierta (no una ventana por URL)."""
    for url in urls:
        abrir_url_en_navegador(url)
        # Un respiro entre pestañas para no saturar al navegador
        # abriendo todas de una en el mismo instante.
        time.sleep(0.3)


def abrir_pestanas_busqueda(consulta: str, tipo: str = "general") -> str:
    """Busca 'consulta' y ABRE varias pestañas con los resultados más
    relevantes — pensado para investigación (papers, imágenes,
    comparar varias fuentes), donde una respuesta hablada no alcanza
    y hace falta poder leer/mirar varias cosas.

    Distinto de buscar_en_web(), que devuelve texto para que Madeline
    lo diga en voz, sin abrir nada."""
    tipo = (tipo or "general").lower().strip()

    if tipo == "imagenes":
        query = urllib.parse.quote(consulta)
        url_imagenes = f"https://www.google.com/search?q={query}&tbm=isch"
        abrir_pestanas_navegador([url_imagenes])
        return f"Abrí resultados de imágenes sobre {consulta}."

    if tipo == "papers":
        urls = buscar_urls_arxiv(consulta, cantidad=3)

        if not urls:
            return f"No encontré papers sobre {consulta} en arXiv."

        abrir_pestanas_navegador(urls)
        return f"Abrí {len(urls)} papers sobre {consulta} desde arXiv."

    urls = buscar_urls_duckduckgo(consulta, cantidad=3)

    if not urls:
        return f"No encontré resultados para {consulta}."

    abrir_pestanas_navegador(urls)
    return f"Abrí {len(urls)} pestañas con resultados sobre {consulta}."


def controlar_bluetooth(accion: str) -> str:
    """Enciende o apaga el adaptador Bluetooth (bluetoothctl/BlueZ)."""
    try:
        if accion == "encender":
            subprocess.run(
                ["bluetoothctl", "power", "on"],
                check=True, timeout=10, capture_output=True,
            )
            return "Bluetooth encendido."

        if accion == "apagar":
            subprocess.run(
                ["bluetoothctl", "power", "off"],
                check=True, timeout=10, capture_output=True,
            )
            return "Bluetooth apagado."

        return "Acción de Bluetooth no reconocida."

    except subprocess.CalledProcessError as e:
        return f"Error controlando el Bluetooth: {e}"
    except FileNotFoundError:
        return "No encontré bluetoothctl instalado en el sistema."
    except Exception as e:
        return f"Error controlando el Bluetooth: {e}"


DISPOSITIVOS_BLUETOOTH = {
    # Las claves SIEMPRE en minúscula: el texto que llega ya está
    # normalizado a minúscula antes de buscar acá, así que una clave
    # con mayúscula ("Alexa") nunca va a matchear.
    # "alexa": "AA:BB:CC:DD:EE:FF",
    # "auriculares": "11:22:33:44:55:66",
    "alexa": "2C:71:FF:6D:CD:88",
    "audífonos": "7C:7B:BF:B3:3E:7D",
    "audifonos": "7C:7B:BF:B3:3E:7D",
    "buds": "7C:7B:BF:B3:3E:7D",
}


def conectar_bluetooth(dispositivo: str) -> str:
    """Conecta a un dispositivo Bluetooth ya emparejado previamente."""
    mac = DISPOSITIVOS_BLUETOOTH.get(dispositivo.strip().lower())

    if not mac:
        return (
            f"No tengo guardada la dirección de '{dispositivo}'. "
            f"Emparejalo primero a mano con bluetoothctl y agregalo "
            f"a DISPOSITIVOS_BLUETOOTH con su MAC (en minúscula)."
        )

    try:
        resultado = subprocess.run(
            ["bluetoothctl", "connect", mac],
            capture_output=True, text=True, timeout=15,
        )

        if resultado.returncode == 0 or "successful" in resultado.stdout.lower():
            return f"Conectado a {dispositivo}."

        return f"No pude conectar a {dispositivo}: {resultado.stdout.strip()}"

    except subprocess.TimeoutExpired:
        return f"Se agotó el tiempo intentando conectar a {dispositivo}."
    except Exception as e:
        return f"Error conectando a {dispositivo}: {e}"


def desconectar_bluetooth(dispositivo: str) -> str:
    """Desconecta un dispositivo Bluetooth ya conectado."""
    mac = DISPOSITIVOS_BLUETOOTH.get(dispositivo.strip().lower())

    if not mac:
        return f"No tengo guardada la dirección de '{dispositivo}'."

    try:
        resultado = subprocess.run(
            ["bluetoothctl", "disconnect", mac],
            capture_output=True, text=True, timeout=10,
        )

        if resultado.returncode == 0:
            return f"Desconectado de {dispositivo}."

        return f"No pude desconectar {dispositivo}: {resultado.stdout.strip()}"

    except Exception as e:
        return f"Error desconectando de {dispositivo}: {e}"


DICCIONARIO_APPS = {
    "navegador": "zen-browser",
    "zen": "zen-browser",
    "zen browser": "zen-browser",
    "browser": "zen-browser",
    "discord": "vesktop",
    "desktop": "vesktop",
    "telegram": "telegram-desktop",
    "tele": "telegram-desktop",
    "telegram desktop": "telegram-desktop",
    "terminal": "kitty",
    "archivos": "nautilus",
    "vs code": "code",
    "vscode": "code",
    "visual studio code": "code",
    "visual studio": "code",
    "minecraft": "prismlauncher",
    "minecraft launcher": "prismlauncher",
    "steam": "steam",
    "heroic": "heroic",
    "heroic games launcher": "heroic",
    "heroic games": "heroic",
    "heroic launcher": "heroic",
    "prism": "prismlauncher",
    "prism launcher": "prismlauncher",
    "bottles": "bottles",

    # --- Navegadores, terminales y archivos ---
    "firefox": "firefox",
    "alacritty": "alacritty",
    "dolphin": "dolphin",
    "explorador": "dolphin",
    "explorador de archivos": "dolphin",

    # --- Oficina (LibreOffice) ---
    "libreoffice": "libreoffice",
    "word": "libreoffice-writer",
    "writer": "libreoffice-writer",
    "excel": "libreoffice-calc",
    "calc": "libreoffice-calc",
    "hoja de cálculo": "libreoffice-calc",
    "hoja de calculo": "libreoffice-calc",
    "powerpoint": "libreoffice-impress",
    "power point": "libreoffice-impress",
    "impress": "libreoffice-impress",

    # --- Utilidades ---
    "calculadora": "qalculate-gtk",
    "calendario": "gnome-calendar",
    "editor de texto": "gnome-text-editor",
    "bloc de notas": "gnome-text-editor",
    "control de volumen": "pavucontrol",
    "mezclador de volumen": "pavucontrol",
    "mezclador": "pavucontrol",
    "pavucontrol": "pavucontrol",
    "gestor de bluetooth": "blueman-manager",
    "administrador de bluetooth": "blueman-manager",
    "panel de nvidia": "nvidia-settings",
    "configuración de nvidia": "nvidia-settings",
    "configuracion de nvidia": "nvidia-settings",
    "configuración de pantallas": "nwg-displays",
    "configuracion de pantallas": "nwg-displays",
    "monitor del sistema": "btop",
    "administrador de tareas": "btop",
    "btop": "btop",
    "pantalla del celular": "scrcpy",
    "espejo del celular": "scrcpy",
    "scrcpy": "scrcpy",
    "máquinas virtuales": "virt-manager",
    "maquinas virtuales": "virt-manager",
    "virt manager": "virt-manager",
    "test de velocidad": "speedtest",
    "speedtest": "speedtest",
    "claude": "claude-desktop",

    # --- Multimedia y descargas ---
    "vlc": "vlc",
    "mpv": "mpv",
    "qbittorrent": "qbittorrent",
    "torrent": "qbittorrent",
    "jdownloader": "jdownloader",
    "motrix": "motrix",
    "upscayl": "upscayl",

    # --- Programación y diseño ---
    "android studio": "android-studio",
    "arduino": "arduino-ide",
    "arduino ide": "arduino-ide",
    "rstudio": "rstudio",
    "r studio": "rstudio",
    "sublime": "sublime",
    "sublime text": "sublime",
    "inkscape": "inkscape",
    "staruml": "staruml",
    "star uml": "staruml",
    "fritzing": "fritzing",
    "meld": "meld",
    "filezilla": "filezilla",
    "haguichi": "haguichi",

    # --- Launchers y emuladores ---
    "lutris": "lutris",
    "faugus": "faugus-launcher",
    "olympus": "olympus",
    "lime3ds": "lime3ds",
    "lime": "lime3ds",
    "emulador de 3ds": "lime3ds",
    "pokemmo": "pokemmo",
    "pokemon": "pokemmo",
    "pokémon": "pokemmo",
    "sober": "sober",
    "roblox": "sober",

    # --- Juegos de Steam ---
    "battlefield": "battlefield",
    "battlefield 4": "battlefield",
    "celeste": "celeste",
    "gravity circuit": "gravity circuit",
    "guild wars": "guild wars 2",
    "guild wars 2": "guild wars 2",
    "guild wars dos": "guild wars 2",
    "gw2": "guild wars 2",
    "hacknet": "hacknet",
    "hades": "hades",
    "how to fish": "how to fish",
    "katana zero": "katana zero",
    "katana": "katana zero",
    "project zomboid": "projectzomboid",
    "zomboid": "projectzomboid",
    "terraria": "terraria",
    "outlast": "outlast",
    "outlast trials": "outlast",
    "the outlast trials": "outlast",
    "ultimate chicken horse": "ultimate chicken horse",
    "chicken horse": "ultimate chicken horse",

    # --- Apps propias de Keric ---
    "notas": "notas_app",
    "mis notas": "notas_app",
    "app de notas": "notas_app",
    "crónica de tyria": "gw2_cronica",
    "cronica de tyria": "gw2_cronica",
    "crónica": "gw2_cronica",
    "cronica": "gw2_cronica",
    "simulador de liga": "descenso_primera_b",
    "simulador": "descenso_primera_b",
    "rocket stats": "rocket_stats",
    "diana zero": "diana_zero",
    "diana": "diana_zero",
}

# El comando real para ABRIR cuando no es el mismo nombre técnico:
# juegos de Steam (por su appid), flatpaks y apps propias fuera del
# PATH. Son constantes, nunca texto del usuario, así que pasarlas a
# hyprctl exec (que corre en una shell) es seguro.
_STEAM = "steam steam://rungameid/"
_LOCAL = os.path.expanduser("~")
COMANDOS_APERTURA = {
    "telegram-desktop": "Telegram",
    "bottles": "flatpak run com.usebottles.bottles",
    "libreoffice-writer": "libreoffice --writer",
    "libreoffice-calc": "libreoffice --calc",
    "libreoffice-impress": "libreoffice --impress",
    "btop": "kitty btop",
    "speedtest": "flatpak run xyz.ketok.Speedtest",
    "jdownloader": "JDownloader",
    "motrix": "/opt/Motrix/motrix",
    "rstudio": "/usr/lib/rstudio/rstudio",
    "sublime": "flatpak run com.sublimetext.three",
    "staruml": "/opt/StarUML/staruml",
    "fritzing": "flatpak run org.fritzing.Fritzing",
    "pokemmo": "flatpak run com.pokemmo.PokeMMO",
    "sober": "flatpak run org.vinegarhq.Sober",
    "battlefield": _STEAM + "1238860",
    "celeste": _STEAM + "504230",
    "gravity circuit": _STEAM + "858710",
    "guild wars 2": _STEAM + "1284210",
    "hacknet": _STEAM + "365450",
    "hades": _STEAM + "1145360",
    "how to fish": _STEAM + "4001890",
    "katana zero": _STEAM + "460950",
    "projectzomboid": _STEAM + "108600",
    "terraria": _STEAM + "105600",
    "outlast": _STEAM + "1304930",
    "ultimate chicken horse": _STEAM + "386940",
    "notas_app": f"{_LOCAL}/.local/bin/notas_app",
    "gw2_cronica": f"{_LOCAL}/gw2/cronica/build/linux/x64/release/bundle/gw2_cronica",
    "descenso_primera_b": f"{_LOCAL}/.local/opt/simulador-liga/descenso_primera_b",
    "rocket_stats": f"{_LOCAL}/Documentos/app_rocket_stats/run_app.sh",
    "diana_zero": f"{_LOCAL}/.local/bin/iniciar_diana_zero.sh",
}

# Alias que son palabras comunes (o casi): el corrector difuso de
# normalizar_terminos() no debe "corregir" otras palabras hacia ellos
# ("sobre" -> "sober", "haces" -> "hades", "anota" -> "notas").
ALIAS_SIN_CORRECCION = {
    "word", "excel", "calc", "writer", "impress", "calculadora",
    "calendario", "mezclador", "explorador", "torrent", "lime", "sober",
    "celeste", "hades", "katana", "outlast", "meld", "notas", "crónica",
    "cronica", "simulador", "diana", "claude", "olympus", "faugus",
    "sublime", "firefox", "dolphin", "arduino", "motrix", "pokemon",
    "pokémon", "roblox", "battlefield", "lutris", "zomboid",
    "pavucontrol", "rstudio", "r studio", "hacknet", "emulador de 3ds",
}

# Para CERRAR, el proceso real a matar a veces no es el mismo binario
# que se usa para ABRIR (mismo motivo que CLASES_VENTANA: el paquete
# se llama distinto al proceso). Minecraft en particular corre como
# "java" aunque se abra con "prismlauncher", así que hay que matar
# los dos para cerrarlo de verdad. Lo que no está acá se cierra
# matando el mismo nombre técnico de DICCIONARIO_APPS.
PROCESOS_CIERRE = {
    "zen-browser": ["zen", "zen-browser"],
    "prismlauncher": ["prismlauncher", "java"],
    "telegram-desktop": ["telegram"],
    "libreoffice-writer": ["soffice"],
    "libreoffice-calc": ["soffice"],
    "libreoffice-impress": ["soffice"],
    "libreoffice": ["soffice"],
    "gw2_cronica": ["gw2_cronica"],
    "diana_zero": ["visor_camara", "diana_zero"],
    # Juegos de Steam: el proceso corre desde steamapps/common/<carpeta>
    "battlefield": ["battlefield 4", "bf4"],
    "outlast": ["outlast trials"],
}


CLASES_VENTANA = {
    "zen-browser": "zen",
}


def clase_ventana(nombre_tecnico: str) -> str:
    """Devuelve la clase real de Hyprland para un nombre técnico dado."""
    return CLASES_VENTANA.get(nombre_tecnico, nombre_tecnico)


# Lista blanca: solo se abre/cierra/mueve lo que está en
# DICCIONARIO_APPS. Antes, un nombre desconocido se usaba tal cual,
# y como app_name puede venir de Groq (o sea, del texto que sea), eso
# terminaba en hyprctl exec (que corre en una shell) o en pkill -f
# (donde "." mata todos tus procesos).
APPS_PERMITIDAS = set(DICCIONARIO_APPS.values())


def resolver_app(app_name: str) -> str | None:
    """Devuelve el nombre técnico de una app conocida, o None si no
    está en la lista blanca. Acepta tanto el alias ("discord") como
    el nombre técnico directo ("vesktop")."""
    nombre = (app_name or "").strip().lower()

    if nombre in DICCIONARIO_APPS:
        return DICCIONARIO_APPS[nombre]
    if nombre in APPS_PERMITIDAS:
        return nombre
    return None


def mensaje_app_desconocida(verbo: str, app_name: str) -> str:
    # Empieza con "No pude" para que respuesta_local lo trate como
    # error y no le anteponga un "Claro, Keric."
    return (
        f"No pude {verbo} '{app_name}': no está en mi lista de "
        f"aplicaciones. Agrégala a DICCIONARIO_APPS si la quieres usar."
    )


VERBOS_APP = {
    "abrir": [
        "abre", "abres", "abrir", "ábreme",
        "ejecuta", "ejecutar", "lanza", "lanzar",
        "inicia", "iniciar",
        # "pon"/"poner" van SOLO en abrir, no en mover: son ambiguos
        # entre "abrir algo" y "mover algo", y abrir_aplicacion ya
        # soporta mandar la app recién abierta a un escritorio, así
        # que cubre el caso de "poner X en el escritorio Y" también.
        "pon", "poner",
    ],
    "cerrar": [
        "cierra", "cierras", "cerrar", "ciérralo", "ciérrame",
        "mata", "termina", "terminar", "sal de", "salir de",
    ],
    "mover": [
        "mueve", "mueves", "mover", "muévelo", "muéveme",
        "lleva", "llevar", "manda", "mandar", "envía", "enviar",
    ],
}


def patron_verbos(clave: str) -> str:
    """Arma la alternancia regex a partir de VERBOS_APP[clave]."""
    return "|".join(re.escape(v) for v in VERBOS_APP[clave])


# Constantes de regex reusadas por abrir/cerrar/mover. Vivir a nivel de
# módulo evita reconstruirlas en cada mensaje y permite compartirlas
# con extraer_apps_de_fragmento() más abajo.
APPS_REGEX = "|".join(
    sorted(
        [re.escape(x) for x in DICCIONARIO_APPS.keys()],
        key=len,
        reverse=True,
    )
)
ARTICULO = r"(?:el|la|los|las)?\s*"
DESTINO = r"(?:escritorio|workspace|ventana)\s*(?:n[uú]mero|num\.?)?\s*"
SEPARADOR_LISTA = r"\s*(?:,|y|e)\s*"


def extraer_apps_de_fragmento(fragmento: str) -> list:
    """Devuelve todas las apps mencionadas en un fragmento de texto,
    sin duplicados y respetando el orden en que aparecen."""
    encontradas = re.findall(rf"\b({APPS_REGEX})\b", fragmento)
    return list(dict.fromkeys(encontradas))


def abrir_aplicacion(app_name: str, escritorio: int = None) -> str:
    """Abre una aplicación y opcionalmente la mueve a un escritorio."""
    nombre_tecnico = resolver_app(app_name)

    if not nombre_tecnico:
        return mensaje_app_desconocida("abrir", app_name)

    comando = COMANDOS_APERTURA.get(nombre_tecnico, nombre_tecnico)

    try:
        if escritorio:
            escritorio = int(escritorio)
            clase = clase_ventana(nombre_tecnico)

            subprocess.Popen(
                ["hyprctl", "dispatch", "exec", comando]
            )

            # Antes era un "exec && sleep 2 && move" con shell=True.
            # Mismo efecto, pero sin shell: el movimiento se agenda en
            # un hilo aparte para no bloquear el servidor esos 2 s.
            threading.Timer(
                2,
                subprocess.run,
                args=([
                    "hyprctl", "dispatch", "movetoworkspacesilent",
                    f"{escritorio},class:.*{clase}.*",
                ],),
            ).start()

            return (
                f"{app_name} se está abriendo "
                f"y será enviada al escritorio {escritorio}."
            )

        subprocess.Popen(
            ["hyprctl", "dispatch", "exec", comando]
        )

        return f"He dado la orden de abrir {app_name}."

    except Exception as e:
        return f"Error al intentar abrir {app_name}: {e}"


def mover_ventana(app_name: str, escritorio: int) -> str:
    """Mueve una ventana a un escritorio específico."""
    nombre_tecnico = resolver_app(app_name)

    if not nombre_tecnico:
        return mensaje_app_desconocida("mover", app_name)

    try:
        escritorio = int(escritorio)
        clase = clase_ventana(nombre_tecnico)

        argumento_hyprland = (
            f"{escritorio},class:.*{clase}.*"
        )

        subprocess.run(
            [
                "hyprctl",
                "dispatch",
                "movetoworkspacesilent",
                argumento_hyprland,
            ]
        )

        return f"Ventana de {app_name} movida al escritorio {escritorio}."

    except Exception as e:
        return f"Error al mover la ventana de {app_name}: {e}"


# ============================================================
# HERRAMIENTAS PARA GROQ
# ============================================================

tools = [
    {
        "type": "function",
        "function": {
            "name": "ajustar_volumen",
            "description": (
                "Controla el volumen de CachyOS. Usa 'setear' para un "
                "volumen exacto, 'subir' o 'bajar' para cambios relativos, "
                "y 'mutear' para silenciar o desmutear."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "accion": {
                        "type": "string",
                        "enum": ["subir", "bajar", "mutear", "setear"],
                    },
                    "porcentaje": {
                        "type": "integer",
                        "description": "Nivel o cantidad de volumen. Por defecto 10.",
                    },
                    "aplicacion": {
                        "type": "string",
                        "description": "Nombre de la aplicación si corresponde.",
                    },
                },
                "required": ["accion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cerrar_aplicacion",
            "description": "Cierra una aplicación abierta en el PC.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Nombre de la aplicación.",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_multimedia",
            "description": (
                "Controla la reproducción multimedia. 'reiniciar' vuelve "
                "a empezar la canción actual (repetirla)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "accion": {
                        "type": "string",
                        "enum": [
                            "pausar",
                            "reproducir",
                            "siguiente",
                            "anterior",
                            "reiniciar",
                        ],
                    }
                },
                "required": ["accion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "info_cancion",
            "description": "Dice qué canción y artista están sonando.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_energia",
            "description": "Apaga o reinicia la computadora.",
            "parameters": {
                "type": "object",
                "properties": {
                    "accion": {
                        "type": "string",
                        "enum": ["apagar", "reiniciar"],
                    }
                },
                "required": ["accion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cerrar_todo",
            "description": "Cierra las aplicaciones comunes de golpe.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reporte_clima",
            "description": "Clima actual o pronóstico (hasta pasado mañana) de Villa Alegre.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dia": {
                        "type": "string",
                        "enum": ["ahora", "hoy", "mañana", "pasado mañana"],
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estado_sistema",
            "description": "Obtiene uso de RAM, CPU, temperatura y estado de la GPU.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tomar_captura",
            "description": "Toma una captura de pantalla.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_en_web",
            "description": (
                "Busca información reciente en internet cuando sea necesario."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {
                        "type": "string",
                        "description": "Consulta exacta que debe buscarse.",
                    }
                },
                "required": ["consulta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abrir_aplicacion",
            "description": "Abre una aplicación y opcionalmente la coloca en un escritorio.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Nombre de la aplicación.",
                    },
                    "escritorio": {
                        "type": "integer",
                        "description": "Número opcional del escritorio.",
                    },
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mover_ventana",
            "description": "Mueve una aplicación a un escritorio específico.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Nombre de la aplicación.",
                    },
                    "escritorio": {
                        "type": "integer",
                        "description": "Número del escritorio.",
                    },
                },
                "required": ["app_name", "escritorio"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "controlar_bluetooth",
            "description": "Enciende o apaga el adaptador Bluetooth del PC.",
            "parameters": {
                "type": "object",
                "properties": {
                    "accion": {
                        "type": "string",
                        "enum": ["encender", "apagar"],
                    }
                },
                "required": ["accion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "conectar_bluetooth",
            "description": (
                "Conecta el PC a un dispositivo Bluetooth ya emparejado "
                "previamente (auriculares, parlante, un Amazon Echo/Alexa "
                "usado como parlante Bluetooth, etc.)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dispositivo": {
                        "type": "string",
                        "description": "Nombre del dispositivo a conectar.",
                    }
                },
                "required": ["dispositivo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desconectar_bluetooth",
            "description": "Desconecta un dispositivo Bluetooth ya conectado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dispositivo": {
                        "type": "string",
                        "description": "Nombre del dispositivo a desconectar.",
                    }
                },
                "required": ["dispositivo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "actualizar_sistema",
            "description": (
                "Inicia la actualización completa de paquetes del "
                "sistema (equivalente al botón de actualizar de "
                "CachyOS), usando paru/yay/pacman en una terminal "
                "aparte."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verificar_actualizaciones",
            "description": (
                "Cuenta cuántos paquetes tienen actualización pendiente "
                "SIN aplicarlas todavía. Usar cuando preguntan si hay "
                "actualizaciones, no cuando piden actualizar ya."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abrir_radio_musica",
            "description": (
                "Abre la radio/playlist fija de YouTube Music en una "
                "pestaña del navegador. Usar para pedidos VAGOS de "
                "música ('pon música', 'quiero escuchar algo'), no "
                "para pausar/reanudar/saltar lo que ya está sonando "
                "(eso es control_multimedia)."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abrir_pestanas_busqueda",
            "description": (
                "Busca algo en internet y ABRE varias pestañas del "
                "navegador con los resultados, en vez de solo responder "
                "hablado. Usar cuando el usuario claramente quiere "
                "investigar, comparar varias fuentes, o ver resultados "
                "por su cuenta — por ejemplo pedidos de estudio o "
                "universidad ('buscame papers sobre X', 'ábreme fuentes "
                "sobre Y', 'quiero ver imágenes de Z'). NO usar para "
                "preguntas que se responden bien con una frase corta "
                "(para eso está buscar_en_web, que no abre nada)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {
                        "type": "string",
                        "description": "Qué buscar.",
                    },
                    "tipo": {
                        "type": "string",
                        "enum": ["general", "papers", "imagenes"],
                        "description": (
                            "'papers' busca en arXiv, 'imagenes' abre "
                            "Google Imágenes, 'general' es para lo demás."
                        ),
                    },
                },
                "required": ["consulta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analizar_pantalla",
            "description": (
                "Toma una captura de la pantalla del PC y la analiza con "
                "un modelo de visión para responder preguntas sobre lo "
                "que se ve — errores en pantalla, qué aplicación está "
                "abierta, leer texto de una imagen, describir un diseño, "
                "etc. Usar cuando el usuario pide explícitamente mirar o "
                "analizar la pantalla."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pregunta": {
                        "type": "string",
                        "description": "Qué preguntar sobre lo que se ve en pantalla.",
                    }
                },
                "required": ["pregunta"],
            },
        },
    },
]


# Nombre de la tool (el mismo del schema de arriba) -> función.
# Reemplaza la cadena de if/elif que repetía cada nombre a mano.
# control_energia NO está acá a propósito: pasa siempre por la
# confirmación (ver procesar_mensaje).
TOOLS_FUNCIONES = {
    "ajustar_volumen": ajustar_volumen,
    "cerrar_aplicacion": cerrar_aplicacion,
    "control_multimedia": control_multimedia,
    "info_cancion": info_cancion,
    "cerrar_todo": cerrar_todo,
    "reporte_clima": reporte_clima,
    "estado_sistema": estado_sistema,
    "tomar_captura": tomar_captura,
    "buscar_en_web": buscar_en_web,
    "abrir_aplicacion": abrir_aplicacion,
    "mover_ventana": mover_ventana,
    "controlar_bluetooth": controlar_bluetooth,
    "conectar_bluetooth": conectar_bluetooth,
    "desconectar_bluetooth": desconectar_bluetooth,
    "actualizar_sistema": actualizar_sistema,
    "verificar_actualizaciones": verificar_actualizaciones,
    "abrir_radio_musica": abrir_radio_musica,
    "abrir_pestanas_busqueda": abrir_pestanas_busqueda,
    "analizar_pantalla": analizar_pantalla,
}

assert set(TOOLS_FUNCIONES) | {"control_energia"} == {
    t["function"]["name"] for t in tools
}, "TOOLS_FUNCIONES y el schema de tools no coinciden"


# ============================================================
# ROUTER LOCAL
# ============================================================

VARIANTES_CONOCIDAS = {
    "estim": "steam",
    "esteam": "steam",
    "stim": "steam",
    "diskord": "discord",
    "discor": "discord",
    "blutu": "bluetooth",
    "blutuz": "bluetooth",
    "bluetu": "bluetooth",
    "blue tooth": "bluetooth",
    "blu tuz": "bluetooth",
}


# Palabras que el fuzzy matching de normalizar_terminos() NUNCA debe
# tocar, aunque se parezcan mucho a algo del vocabulario. Sin esto,
# "termina discord" se corregía a "terminal discord" (porque
# "termina" se parece mucho a la app "terminal"), y "visual studio
# code" perdía el "code" al final (porque se parece a "vscode").
# Son dos bugs reales que encontramos así, no algo hipotético.
def _palabras_de(frase: str) -> set:
    return set(frase.split())


# Palabras comunes que el corrector difuso confundía con apps
# ("esto" y "stop" -> "desktop"), más el vocabulario de música.
PALABRAS_PROTEGIDAS = {
    "esto", "esta", "este", "eso", "esa", "ese", "estos", "estas", "para",
    "pasa", "otra", "otro", "stop", "play", "skip", "next", "sigue", "tema",
    "pausa", "canta", "suena", "musica", "música", "cancion", "canción",
    "video", "anterior", "siguiente", "repite", "salta", "cambia", "ponle",
    "dale", "todo", "todos", "nada", "algo", "ahora", "antes", "después",
    "despues", "donde", "dónde", "cuando", "cuándo", "quien", "quién",
}

# Verbos de abrir/cerrar/mover: evita que un verbo real ("termina",
# "sal", "de") se "corrija" a una app parecida.
for _lista_verbos in VERBOS_APP.values():
    for _verbo in _lista_verbos:
        PALABRAS_PROTEGIDAS.update(_palabras_de(_verbo))

# Cualquier palabra que YA es parte de un nombre de app o dispositivo
# conocido: evita que se "corrija" a otra cosa y rompa el nombre
# completo ("visual studio code" -> "visual studio vscode").
for _app in DICCIONARIO_APPS.keys():
    PALABRAS_PROTEGIDAS.update(_palabras_de(_app))
for _dispositivo in DISPOSITIVOS_BLUETOOTH.keys():
    PALABRAS_PROTEGIDAS.update(_palabras_de(_dispositivo))


def normalizar_terminos(texto: str) -> str:
    """Corrige errores comunes de transcripción ANTES de que el texto llegue a los regex."""
    for variante, correcto in VARIANTES_CONOCIDAS.items():
        texto = re.sub(
            rf"\b{re.escape(variante)}\b",
            correcto,
            texto,
            flags=re.IGNORECASE,
        )

    vocabulario = [
        alias for alias in DICCIONARIO_APPS if alias not in ALIAS_SIN_CORRECCION
    ] + list(DISPOSITIVOS_BLUETOOTH.keys())

    if not vocabulario:
        return texto

    palabras = texto.split()

    for i, palabra in enumerate(palabras):
        limpia = re.sub(r"[^\w áéíóúñ]", "", palabra.lower())

        if (
            len(limpia) < 4
            or limpia in vocabulario
            or limpia in PALABRAS_PROTEGIDAS
        ):
            continue

        coincidencia = difflib.get_close_matches(
            limpia, vocabulario, n=1, cutoff=0.72
        )

        if coincidencia:
            palabras[i] = coincidencia[0]

    return " ".join(palabras)


RUTINAS = {
    "modo juego": [
        ("cerrar_aplicacion", {"app_name": "discord"}),
        ("cerrar_aplicacion", {"app_name": "navegador"}),
        ("abrir_aplicacion", {"app_name": "steam"}),
    ],
    "modo trabajo": [
        ("abrir_aplicacion", {"app_name": "navegador"}),
        ("abrir_aplicacion", {"app_name": "vscode"}),
        ("abrir_aplicacion", {"app_name": "discord", "escritorio": 2}),
    ],
}


def extraer_pedido_analisis_pantalla(texto_original: str):
    """Detecta 'Madeline, analiza mi pantalla [y] <pregunta>'. Si no
    hay pregunta explícita, usa una genérica ('¿qué ves?')."""
    match = re.search(
        r"\banaliza(?:me)?\s+(?:la\s+|mi\s+)?pantalla\b"
        r"(?:\s*(?:y|,)\s*(.+))?",
        texto_original,
        re.IGNORECASE,
    )

    if not match:
        return None

    pregunta = (match.group(1) or "").strip(" .,¡!¿?")

    if not pregunta:
        pregunta = "Describí brevemente qué ves en esta captura de pantalla."

    return pregunta


def extraer_pedido_pestanas(texto_original: str):
    """Detecta un pedido EXPLÍCITO de abrir pestañas de investigación
    (papers/imágenes) — los casos obvios, gratis y sin Groq. Frases
    más libres ('buscame fuentes sobre X y ábrelas') las agarra la
    herramienta de Groq, no esto."""

    match = re.search(
        r"\b(?:busca(?:me)?|abre|ábreme)\s+papers?\s+"
        r"(?:sobre|de|acerca de)\s+(.+)",
        texto_original,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip(" .,¡!¿?"), "papers"

    match = re.search(
        r"\b(?:busca(?:me)?|abre|ábreme)\s+im[aá]genes\s+"
        r"(?:de|sobre)\s+(.+)",
        texto_original,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip(" .,¡!¿?"), "imagenes"

    return None, None


def es_pedido_de_musica(texto_original: str) -> bool:
    """Detecta un pedido VAGO de música ('pon música', 'pon algo de
    música', 'quiero escuchar música') — dispara la radio fija de
    YouTube Music. No busca canciones puntuales por nombre; para eso
    ya no hay comando (ver charla con el usuario: se sacó adrede por
    ser demasiado complejo para el beneficio real)."""
    return bool(re.search(
        r"\b(?:pon(?:me)?|reproduc(?:e|ir)|toca|dame|quiero escuchar)\s+"
        r"(?:algo\s+de\s+)?m[uú]sica\b",
        texto_original,
        re.IGNORECASE,
    ))


def extraer_recordatorio(texto_original: str):
    """Detecta 'recordame/avisame en N minutos/horas (que) <mensaje>'."""
    match = re.search(
        r"\b(?:record[aá]me|recu[eé]rdame|avis[aá]me)\s+en\s+(\d+)\s*"
        r"(minutos?|horas?|segundos?)\b(?:\s+que\s+)?(.*)",
        texto_original,
        re.IGNORECASE,
    )

    if not match:
        return None

    cantidad = int(match.group(1))
    unidad = match.group(2).lower()
    mensaje = match.group(3).strip(" .,¡!¿?")

    if not mensaje:
        mensaje = "tu recordatorio"

    return cantidad, unidad, mensaje


# ------------------------------------------------------------
# PATRONES DE ÓRDENES CORTAS (música, volumen, captura, clima)
# ------------------------------------------------------------
# Antes bastaba con que la palabra APARECIERA en cualquier parte:
# "¿cuál es el siguiente feriado?" saltaba de canción, "sube el
# brillo" subía el volumen y "¿qué es una pausa activa?" pausaba la
# música — y como el router local corta antes de Groq, esas preguntas
# nunca recibían respuesta. Ahora la orden tiene que estar al INICIO
# (del mensaje o después de "y"/coma) y TERMINAR ahí (fin, puntuación
# u otra orden con "y").

INICIO_ORDEN = (
    r"(?:^|[,;.]\s*|\by\s+|\be\s+)"
    r"[¿¡]?\s*(?:oye\s+)?(?:madel\w*\s*,?\s*)?(?:por\s+favor\s*,?\s*)?"
    r"(?:(?:puedes|podr[ií]as)\s+)?"
)
FIN_ORDEN = r"(?=\s*(?:[.,;!?]|\by\b|\be\b|$))"

def quitar_tildes(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


# Órdenes de música. Se buscan sobre el texto SIN tildes (Whisper a
# veces las pone y a veces no) y, como el resto de órdenes cortas,
# tienen que ocupar la cláusula entera: así "¿qué es una pausa
# activa?" o "¿cuál es el siguiente feriado?" siguen yendo a Groq.
# Las frases que tiene que entender están en tests/frases_multimedia.py.
_OBJ = (
    r"(?:(?:a\s+)?(?:la|el|esta|este|esa|ese)\s+|al\s+|a\s+)?"
    r"(?:musica|cancion|video|reproduccion|tema|rola)"
)
_OBJ_OPC = rf"(?:\s+{_OBJ})?"
# Relleno que puede ir al final sin cambiar la orden.
_RELLENO = (
    r"(?:\s+(?:de\s+nuevo|otra\s+vez|un\s+(?:momento|rato|segundo|ratito)"
    r"|por\s+favor|porfa|ahora|ya|please))*"
)
_PONER = r"(?:ponle|ponele|pon|ponme|dale|darle|da|hazle)"

PATRONES_MULTIMEDIA = {
    "reproducir": [
        rf"{_PONER}(?:\s+(?:al|el))?\s+play{_OBJ_OPC}",
        rf"play{_OBJ_OPC}",
        rf"(?:reanuda|reanudar|resume|continua|continuar|despausa|despausar|reproduce|reproducir){_OBJ_OPC}",
        rf"(?:sigue|seguir)\s+con{_OBJ_OPC.replace('(?:', '(?:', 1)}",
        rf"(?:quita|quitale|saca|sacale)\s+(?:la\s+|el\s+)?pausa(?:\s+{_OBJ})?",
        rf"(?:vuelve\s+a\s+poner|pon\s+de\s+nuevo|pon\s+otra\s+vez)\s+{_OBJ}",
        rf"pon\s+(?:la|el)\s+(?:musica|cancion|video)\s+(?:otra\s+vez|de\s+nuevo)",
    ],
    "pausar": [
        rf"(?:pausa|pausar|pausala|pausalo|parala|paralo|deten|detener|detenla|stop){_OBJ_OPC}",
        rf"para\s+{_OBJ}|para",
        rf"(?:apaga|quita|calla)\s+{_OBJ}",
        rf"{_PONER}\s+(?:una\s+)?(?:pausa|stop)(?:\s+{_OBJ})?",
    ],
    "siguiente": [
        rf"(?:siguiente|la\s+siguiente|next|skip|la\s+que\s+sigue){_OBJ_OPC}",
        rf"(?:(?:la|el)\s+)?(?:cancion|tema|video)\s+siguiente",
        rf"otra(?!\s+vez){_OBJ_OPC}|otra\s+(?:cancion|tema|rola)",
        rf"(?:pasa|pasate|salta|saltate|skipea|cambia|cambiale)"
        rf"(?:\s+(?:a\s+la\s+siguiente|de\s+(?:cancion|tema)|esta(?:\s+cancion)?|este(?:\s+tema)?|{_OBJ}))?",
        rf"pon(?:me)?\s+(?:la\s+siguiente|otra(?:\s+cancion)?|la\s+que\s+sigue)",
    ],
    "anterior": [
        r"(?:(?:pon(?:me)?|vuelve\s+a|regresa\s+a|devuelvete\s+a|ve\s+a)\s+)?(?:la\s+|el\s+)?"
        r"(?:(?:cancion|tema|video)\s+)?(?:anterior|de\s+antes)",
        r"previous|vuelve\s+atras|retrocede|regresa",
    ],
    "reiniciar": [
        rf"(?:reinicia|reiniciar|repite|repetir|empieza|vuelve\s+a\s+empezar)\s+{_OBJ}"
        r"(?:\s+(?:de\s+nuevo|desde\s+(?:el\s+)?(?:principio|inicio|comienzo)))?",
        rf"(?:pon\s+)?(?:{_OBJ}\s+)?desde\s+(?:el\s+)?(?:principio|inicio|comienzo)",
        rf"otra\s+vez\s+{_OBJ}",
        # "pon ESA canción de nuevo" es repetirla; "pon la música de
        # nuevo" (sin demostrativo) sigue siendo reanudar.
        r"pon(?:me)?\s+(?:esa|esta)\s+(?:cancion|tema|rola)\s+(?:de\s+nuevo|otra\s+vez)",
    ],
}

PATRONES_INFO_CANCION = [
    r"que\s+(?:cancion|tema|musica|rola)\s+(?:es(?:\s+(?:esta|este|esa|ese|la\s+que\s+suena))?"
    r"|esta\s+sonando|suena|estoy\s+escuchando|es\s+la\s+que\s+suena)",
    r"que\s+(?:esta\s+sonando|suena|estoy\s+escuchando)",
    r"como\s+se\s+llama\s+(?:esta|este|esa|ese|la|el)\s+(?:cancion|tema|rola)",
    r"quien\s+canta\s+(?:esto|eso|esta(?:\s+cancion)?|este(?:\s+tema)?)",
    r"de\s+quien\s+es\s+(?:esta|este|esto|esa)(?:\s+(?:cancion|tema))?",
    r"cual\s+es\s+(?:esta|esa)\s+cancion",
    r"(?:cual\s+es\s+)?el\s+nombre\s+de\s+(?:esta|la|esa)\s+cancion",
    r"que\s+tema\s+es\s+(?:este|ese)",
    r"quien\s+es\s+el\s+artista",
]
_INICIO_INFO = r"(?:(?:dime|sabes|me\s+dices)\s+)?"


def extraer_ordenes_multimedia(t: str):
    """Órdenes de música de la frase, en el orden en que aparecen."""
    t = quitar_tildes(t.lower())
    encontradas = []
    fin = _RELLENO + FIN_ORDEN

    for accion, patrones in PATRONES_MULTIMEDIA.items():
        for patron in patrones:
            match = re.search(INICIO_ORDEN + rf"(?:{patron}){fin}", t)
            if match:
                encontradas.append((match.start(), ("multimedia", {"accion": accion})))
                break

    for patron in PATRONES_INFO_CANCION:
        match = re.search(INICIO_ORDEN + _INICIO_INFO + rf"(?:{patron}){fin}", t)
        if match:
            encontradas.append((match.start(), ("info_cancion", {})))
            break

    return [orden for _, orden in sorted(encontradas, key=lambda o: o[0])]

PATRON_CAPTURA = (
    r"\b(?:screenshot|pantallazo)\b"
    r"|\b(?:toma|tómame|tomame|saca|sácame|sacame|haz|hazme|tomar|sacar|hacer)\s+"
    r"(?:una\s+|un\s+)?captura\b"
    + "|" + INICIO_ORDEN + r"captura(?:\s+(?:de\s+)?(?:la\s+)?pantalla)?" + FIN_ORDEN
)

VERBOS_SUBIR = r"sube|subir|s[uú]bele|aumenta|incrementa|dale m[aá]s|m[aá]s"
VERBOS_BAJAR = r"baja|bajar|b[aá]jale|disminuye|reduce|dale menos|menos"
VERBOS_FIJAR = r"pon|ponme|deja|ajusta|fija|setea|cambia"

PATRON_VOLUMEN = (
    INICIO_ORDEN
    + rf"(?P<verbo>{VERBOS_SUBIR}|{VERBOS_BAJAR}|{VERBOS_FIJAR})?"
    + r"\s*(?P<objeto>(?:el\s+)?(?:volumen|audio|sonido))?"
    + r"(?:\s+un\s+(?:poco|poquito)(?:\s+m[aá]s)?)?"
    + r"(?:\s*(?P<prep>en|un|a|al)?\s*(?P<numero>\d{1,3})\s*(?:%|por\s*ciento)?)?"
    + FIN_ORDEN
)


def extraer_orden_volumen(t: str):
    """Devuelve los argumentos de ajustar_volumen, o None.

    "sube el volumen 20" / "súbele" -> subir; "baja el volumen al 30"
    -> setear 30 (antes bajaba 30 puntos); "sube el brillo" -> None."""
    for match in re.finditer(PATRON_VOLUMEN, t):
        verbo, objeto, prep, numero = match.group("verbo", "objeto", "prep", "numero")

        if not verbo and not objeto:
            continue

        es_fijar = verbo and re.fullmatch(VERBOS_FIJAR, verbo)

        if numero and (prep in ("a", "al") or es_fijar or not verbo):
            if not objeto:
                continue
            return {"accion": "setear", "porcentaje": max(0, min(100, int(numero)))}

        if es_fijar or not verbo:
            continue

        accion = "subir" if re.fullmatch(VERBOS_SUBIR, verbo) else "bajar"
        return {"accion": accion, "porcentaje": int(numero or 10)}

    return None


def extraer_pedido_clima(t: str):
    """Devuelve "ahora", "hoy", "mañana" o "pasado mañana" si el
    mensaje pide el clima, o None. El pronóstico (hasta pasado
    mañana) también se resuelve local: antes "¿llueve mañana?" se
    mandaba a Groq, que solo tenía el clima actual para responder."""
    if not re.search(
        r"\b(?:clima|pron[oó]stico|qu[eé] tiempo hace|"
        r"c[oó]mo est[aá] el tiempo|el tiempo (?:para |de )?(?:hoy|mañana)|"
        r"va a llover|llover[aá]|llueve (?:hoy|mañana))\b",
        t,
    ):
        return None

    # wttr.in solo da 3 días: "la semana" u otros días van a Groq.
    if re.search(r"\b(?:semana|noticias?|lunes|martes|miércoles|miercoles|"
                 r"jueves|viernes|sábado|sabado|domingo)\b", t):
        return None

    if "pasado mañana" in t:
        return "pasado mañana"
    if re.search(r"\bmañana\b", t) and "esta mañana" not in t:
        return "mañana"
    if re.search(r"\b(?:hoy|pron[oó]stico|llover|lloverá|llueve)", t):
        return "hoy"
    return "ahora"


# Charla corta que no necesita a Groq: "gracias" o "hola" gastaban
# una llamada completa (prompt + tools) para contestar una frase.
PATRON_CHARLA = re.compile(
    r"^(?:oye\s+)?(?:madel\w*\s*,?\s*)?"
    r"(?P<tipo>(?:muchas\s+)?gracias|hola|buenas|buenos d[ií]as|buenas tardes|"
    r"buenas noches|ok|okay|vale|listo|perfecto|genial|excelente)"
    r"(?:\s*,?\s*madel\w*)?[\s.!¡¿?]*$"
)

RESPUESTAS_CHARLA = {
    "gracias": ["De nada, Keric.", "Para eso estoy.", "¡Cuando quieras!"],
    "saludo": ["Hola, Keric. ¿En qué te ayudo?", "¡Hola! Aquí estoy.", "Dime, Keric."],
    "ok": ["Perfecto.", "Aquí estaré si me necesitas.", "Muy bien."],
}


# Memoria a largo plazo: se resuelve en local (0 tokens de Groq).
# "recuerda que" no choca con los recordatorios: esos dicen
# "recuérdame en N minutos".
_PREFIJO_MEMORIA = (
    r"^[¿¡]?\s*(?:oye\s+)?(?:madel\w*\s*,?\s*)?"
    r"(?:por\s+favor\s*,?\s*)?"
)
PATRON_MEMORIA_GUARDAR = re.compile(
    _PREFIJO_MEMORIA
    + r"(?:recuerda|acu[eé]rdate(?:\s+de)?|no\s+olvides|anota|guarda\s+en\s+(?:tu\s+)?memoria)"
    r"\s*(?:que|esto|lo\s+siguiente)?\s*:?\s+(?P<dato>.{3,})",
    re.IGNORECASE,
)
PATRON_MEMORIA_OLVIDAR = re.compile(
    _PREFIJO_MEMORIA
    + r"(?:olvida|olv[ií]date\s+de|borra\s+(?:de\s+tu\s+memoria|el\s+recuerdo\s+de))"
    r"\s+(?:lo\s+de\s+|eso\s+de\s+|que\s+)?(?P<tema>.{3,})",
    re.IGNORECASE,
)
PATRON_MEMORIA_CONSULTAR = re.compile(
    _PREFIJO_MEMORIA
    + r"qu[eé]\s+(?:(?:recuerdas|sabes)\s+(?:de|sobre)\s+m[ií](?:\s+mism[oa])?"
    r"|recuerdas\s+(?:de|sobre|acerca\s+de)\s+(?P<tema>.+))\s*[?.!]*$",
    re.IGNORECASE,
)


def pedido_de_memoria(texto: str):
    """("memoria_guardar"|"memoria_olvidar"|"memoria_consultar", args) o None."""
    texto = texto.strip()
    match = PATRON_MEMORIA_GUARDAR.match(texto)
    if match:
        return "memoria_guardar", {"dato": match.group("dato").strip(" .!¡")}
    match = PATRON_MEMORIA_OLVIDAR.match(texto)
    if match:
        return "memoria_olvidar", {"tema": match.group("tema").strip(" .!¡?¿")}
    match = PATRON_MEMORIA_CONSULTAR.match(texto)
    if match:
        return "memoria_consultar", {"tema": (match.group("tema") or "").strip(" .!¡?¿")}
    return None


def memoria_guardar(dato: str) -> str:
    memoria.guardar(dato)
    return random.choice(["Anotado, Keric.", "Lo tendré presente.", "Guardado en mi memoria.", "No se me olvida."])


def memoria_olvidar(tema: str) -> str:
    borrado = memoria.olvidar(tema)
    if not borrado:
        return f"No tenía nada guardado sobre {tema}."
    return f"Listo, olvidé que {borrado}." if len(borrado) < 120 else "Listo, ya lo olvidé."


def memoria_consultar(tema: str) -> str:
    recuerdos = memoria.recuerdos_sobre(tema)
    if not recuerdos:
        return f"No tengo nada guardado sobre {tema}." if tema else "Todavía no me has contado nada de ti."
    # Solo datos cortos se leen enteros; de una conversación basta la pregunta.
    frases = [
        r.split(" — ")[0].replace("Keric preguntó:", "me preguntaste").strip()
        for r in recuerdos[:3]
    ]
    return "Recuerdo que " + "; ".join(frases) + "."


def tipo_charla(t: str):
    match = PATRON_CHARLA.match(t.strip())
    if not match:
        return None
    palabra = match.group("tipo")
    if "gracias" in palabra:
        return "gracias"
    if palabra.startswith(("hola", "buen")):
        return "saludo"
    return "ok"


def router_local(texto: str):
    """Detecta múltiples comandos locales dentro del mismo mensaje."""

    t_crudo = texto.lower().strip()

    # ------------------------------------------------------------
    # CONFIRMACIÓN PENDIENTE (apagar/reiniciar el sistema)
    # ------------------------------------------------------------
    # Si el mensaje anterior dejó una acción destructiva esperando
    # confirmación, este mensaje se interpreta como la respuesta a
    # esa pregunta ANTES que como un comando nuevo.
    if memoria_contexto.get("confirmacion_pendiente"):
        pendiente = memoria_contexto["confirmacion_pendiente"]

        if re.search(
            r"\b(s[ií]|confirmo|dale|procede|afirmativo|adelante|hazlo|hágalo|hagalo)\b",
            t_crudo
        ):
            memoria_contexto["confirmacion_pendiente"] = None
            return [("energia", pendiente)]

        # Cualquier otra cosa cancela lo pendiente — nunca se queda
        # "colgado" esperando sí/no para siempre.
        memoria_contexto["confirmacion_pendiente"] = None

        if re.search(r"\b(no|cancela|cancelar|negativo|olv[ií]dalo)\b", t_crudo):
            return [("confirmacion_cancelada", {})]

        # Si no fue ni sí ni no, seguimos procesando el mensaje como
        # un comando nuevo y normal (no lo perdemos).

    charla = tipo_charla(t_crudo)
    if charla:
        return [("charla", {"tipo": charla})]

    pedido_memoria = pedido_de_memoria(texto)
    if pedido_memoria:
        return [pedido_memoria]

    pregunta_pantalla = extraer_pedido_analisis_pantalla(texto)
    if pregunta_pantalla:
        return [("analizar_pantalla", {"pregunta": pregunta_pantalla})]

    consulta_pestanas, tipo_pestanas = extraer_pedido_pestanas(texto)
    if consulta_pestanas:
        return [("abrir_pestanas_busqueda", {
            "consulta": consulta_pestanas,
            "tipo": tipo_pestanas,
        })]

    if es_pedido_de_musica(texto):
        return [("radio_musica", {})]

    recordatorio = extraer_recordatorio(texto)
    if recordatorio:
        cantidad, unidad, mensaje = recordatorio
        return [("recordatorio", {
            "cantidad": cantidad,
            "unidad": unidad,
            "mensaje": mensaje,
        })]

    t = normalizar_terminos(t_crudo)
    comandos = []

    for nombre_rutina, pasos in RUTINAS.items():
        if nombre_rutina in t:
            comandos.extend(pasos)
            break

    clima = extraer_pedido_clima(t)
    if clima:
        comandos.append(("clima", {"dia": clima}))

    if re.search(PATRON_CAPTURA, t):
        comandos.append(("captura", {}))

    # Sobre el texto sin el corrector de apps: no hay apps que corregir
    # y el corrector ya rompió "stop" y "esto".
    comandos.extend(extraer_ordenes_multimedia(t_crudo))

    orden_volumen = extraer_orden_volumen(t)
    if orden_volumen:
        comandos.append(("volumen", orden_volumen))

    if re.search(
        INICIO_ORDEN
        + r"(?:mute|mutea|mutear|silencia|silenciar|desmutea|desmutear)"
        + r"(?:\s+(?:el\s+|la\s+)?(?:volumen|audio|sonido|m[uú]sica|todo))?"
        + FIN_ORDEN,
        t
    ):
        comandos.append(("volumen", {"accion": "mutear"}))

    if (
        "estado del sistema" in t
        or "estado del pc" in t
        or "estado del computador" in t
        or "uso de ram" in t
        or "uso de cpu" in t
        or "cuánta ram" in t
        or "cuanta ram" in t
    ):
        comandos.append(("estado_sistema", {}))

    if re.search(
        r"\b(cierra todo|cerrar todo|cierra las aplicaciones|"
        r"modo pánico|modo panico)\b",
        t
    ):
        comandos.append(("cerrar_todo", {}))

    if re.search(
        r"\b(prende|enciende|activa|habilita)\s+(?:el\s+)?bluetooth\b",
        t
    ):
        comandos.append(("bluetooth", {"accion": "encender"}))

    if re.search(
        r"\b(apaga|apágame|apagame|desactiva|deshabilita)\s+(?:el\s+)?bluetooth\b",
        t
    ):
        comandos.append(("bluetooth", {"accion": "apagar"}))

    if DISPOSITIVOS_BLUETOOTH:
        articulo_bt = r"(?:el|la|los|las)?\s*"
        dispositivos_bt = "|".join(
            re.escape(x.lower()) for x in DISPOSITIVOS_BLUETOOTH.keys()
        )

        match = re.search(
            rf"\b(?:conecta(?:te)?|conécta(?:te)?|conectar|vincula(?:te)?|vincúla(?:te)?)\s+"
            rf"{articulo_bt}(?:a\s+)?{articulo_bt}({dispositivos_bt})\b",
            t
        )

        if match:
            comandos.append(("conectar_bluetooth", {
                "dispositivo": match.group(1)
            }))

        match = re.search(
            rf"\b(?:desconecta(?:te)?|desconécta(?:te)?|desconectar)\s+"
            rf"{articulo_bt}(?:de\s+)?{articulo_bt}({dispositivos_bt})\b",
            t
        )

        if match:
            comandos.append(("desconectar_bluetooth", {
                "dispositivo": match.group(1)
            }))

    # Reiniciar el PROCESO de Madeline (para aplicar cambios de código)
    # es distinto de reiniciar la PC — no necesita confirmación,
    # porque no interrumpe nada más que a la propia Madeline un momento.
    if re.search(r"\breinicia(?:te)?\s+(?:el\s+)?(?:servidor|madel\w*)\b", t):
        return [("reiniciar_servidor", {})]

    palabras_pc = r"(?:la\s+|el\s+)?(?:pc|computadora|computador|equipo|sistema)"

    # Apagar/reiniciar son destructivos (cortan lo que estés haciendo
    # sin aviso), así que en vez de ejecutar directo, se pide
    # confirmación local — sin gastar tokens de Groq — y se ejecuta
    # recién en el próximo mensaje si el usuario confirma. Por eso
    # esto SIEMPRE corta con return: no tiene sentido combinarlo con
    # otro comando en la misma frase.
    if re.search(
        rf"\b(apágate|apagate|apágame|apagame)\b"
        rf"|\bapaga(?:r)?\s+{palabras_pc}\b",
        t
    ):
        memoria_contexto["confirmacion_pendiente"] = {"accion": "apagar"}
        return [("confirmar_accion", {"accion": "apagar"})]

    if re.search(
        rf"\b(reiníciate|reiniciate)\b"
        rf"|\breinicia(?:r)?\s+{palabras_pc}\b",
        t
    ):
        memoria_contexto["confirmacion_pendiente"] = {"accion": "reiniciar"}
        return [("confirmar_accion", {"accion": "reiniciar"})]

    palabras_actualizar = (
        r"(?:el\s+sistema|los\s+paquetes|todo|el\s+pc|la\s+pc|"
        r"el\s+computador|la\s+computadora)"
    )

    if re.search(
        rf"\b(actualízate|actualizate)\b"
        rf"|\bactualiza(?:r)?\s+{palabras_actualizar}\b",
        t
    ):
        comandos.append(("actualizar_sistema", {}))

    if re.search(
        r"\b(?:tengo|hay)\s+(?:actualizaciones|paquetes)\s+pendientes\b"
        r"|\bcu[aá]ntas actualizaciones (?:tengo|hay)\b"
        r"|\bqu[eé] actualizaciones (?:tengo|hay)\b",
        t
    ):
        comandos.append(("verificar_actualizaciones", {}))

    # Las tres listas de abajo reconocen "app1, app2 y app3" después
    # del verbo, no solo una app suelta. Por eso primero armamos el
    # fragmento completo con re.finditer, y adentro de ESE fragmento
    # extraemos cada app individual con extraer_apps_de_fragmento().
    # Así "cierra discord y steam" genera dos comandos, no uno.

    patron_cerrar = (
        rf"\b(?:{patron_verbos('cerrar')})\s+"
        rf"{ARTICULO}(?:{APPS_REGEX})"
        rf"(?:{SEPARADOR_LISTA}{ARTICULO}(?:{APPS_REGEX}))*\b"
    )

    for match in re.finditer(patron_cerrar, t):
        for app_name in extraer_apps_de_fragmento(match.group(0)):
            comandos.append(("cerrar_aplicacion", {"app_name": app_name}))

    patron_abrir = (
        rf"\b(?:{patron_verbos('abrir')})\s+"
        rf"{ARTICULO}(?:{APPS_REGEX})"
        rf"(?:{SEPARADOR_LISTA}{ARTICULO}(?:{APPS_REGEX}))*"
        rf"(?:\s+(?:en|al)\s+{ARTICULO}{DESTINO}(\d+))?\b"
    )

    for match in re.finditer(patron_abrir, t):
        escritorio = int(match.group(1)) if match.group(1) else None

        for app_name in extraer_apps_de_fragmento(match.group(0)):
            argumentos = {"app_name": app_name}

            if escritorio:
                argumentos["escritorio"] = escritorio

            comandos.append(("abrir_aplicacion", argumentos))

    patron_mover = (
        rf"\b(?:{patron_verbos('mover')})\s+"
        rf"{ARTICULO}(?:{APPS_REGEX})"
        rf"(?:{SEPARADOR_LISTA}{ARTICULO}(?:{APPS_REGEX}))*\s+"
        rf"(?:al|a)\s+{ARTICULO}{DESTINO}(\d+)\b"
    )

    for match in re.finditer(patron_mover, t):
        escritorio = int(match.group(1))

        for app_name in extraer_apps_de_fragmento(match.group(0)):
            comandos.append(("mover_ventana", {
                "app_name": app_name,
                "escritorio": escritorio,
            }))

    return comandos


# ============================================================
# VARIEDAD DE RESPUESTAS (sin gastar tokens de Groq)
# ============================================================

MULETILLAS = [
    "Claro, Keric.",
    "Vamos allá, Keric.",
    "Enseguida, Keric.",
    "Entendido, Keric.",
    "Con gusto, Keric.",
    "Dalo por hecho, Keric.",
    "Sin problema, Keric.",
    "Marchando.",
    "Entendido.",
    "Ahora mismo.",
    "Con gusto.",
    "Dalo por hecho.",
    "Sin problema.",
    "En eso estoy.",
    "Hecho está.",
    "¡Vamos!",
    # Variantes SIN el nombre — para que no aparezca en el 100% de las
    # respuestas, como pide una personalidad natural y no robótica.
    "Desde luego.",
    "Por supuesto.",
    "Enseguida.",
    "Hecho.",
    "Ahí va.",
    "Claro que sí.",
    "Cómo no.",
    "Ya mismo.",
]

NUCLEOS = {
    ("captura", None): [
        "Captura realizada",
        "Ya tomé la captura",
        "Pantallazo listo",
        "Captura hecha y en camino",
        "Ahí quedó la imagen",
        "Pantalla capturada",
    ],
    ("multimedia", "pausar"): [
        "Reproducción pausada",
        "Música pausada",
        "Dejé la reproducción en pausa",
        "Silencio momentáneo, ya está",
    ],
    ("multimedia", "reproducir"): [
        "Reanudando la reproducción",
        "Sigue sonando",
        "Le doy play de nuevo",
        "De vuelta a la música",
    ],
    ("multimedia", "siguiente"): [
        "Pasando a la siguiente canción",
        "Vamos a la próxima",
        "Cambié de canción",
        "Saltando a la que sigue",
    ],
    ("multimedia", "anterior"): [
        "Volviendo a la canción anterior",
        "Retrocedí una canción",
        "De vuelta al tema anterior",
    ],
    ("multimedia", "reiniciar"): [
        "Reiniciando la reproducción",
        "Volví a empezar la canción",
        "Desde el principio otra vez",
    ],
    ("volumen", "setear"): [
        "Volumen fijado al {porcentaje}%",
        "Dejé el volumen en {porcentaje}%",
        "Ajustado al {porcentaje}% de volumen",
        "Volumen en {porcentaje}% en punto",
    ],
    ("volumen", "subir"): [
        "Subí el volumen un {porcentaje}%",
        "Aumenté el volumen en {porcentaje}%",
        "Un {porcentaje}% más de volumen, listo",
        "Le metí {porcentaje}% más de volumen",
    ],
    ("volumen", "bajar"): [
        "Bajé el volumen un {porcentaje}%",
        "Reduje el volumen en {porcentaje}%",
        "Un {porcentaje}% menos de volumen",
        "Le quité {porcentaje}% de volumen",
    ],
    ("volumen", "mutear"): [
        "Cambié el estado del silencio",
        "Alterné el mute",
        "Listo con el silencio",
    ],
    ("bluetooth", "encender"): [
        "Bluetooth encendido",
        "Adaptador Bluetooth activado",
        "Ya está prendido el Bluetooth",
    ],
    ("bluetooth", "apagar"): [
        "Bluetooth apagado",
        "Adaptador Bluetooth desactivado",
        "Ya está apagado el Bluetooth",
    ],
}


PREFIJOS_ERROR = ("Error", "Pucha", "Falló", "No pude", "Hubo un drama")

# Respuestas que ya son una frase completa (una pregunta, un aviso, un
# saludo): envolverlas con "Claro, Keric. ¿Quieres...?" suena raro.
RESPUESTAS_COMPLETAS = (
    "confirmar_accion", "confirmacion_cancelada", "charla", "respuesta_busqueda",
    "memoria_guardar", "memoria_olvidar", "memoria_consultar", "info_cancion",
)


def nucleo_local(nombre: str, args: dict, resultado: str) -> str:
    """Devuelve solo el 'núcleo' de la respuesta."""
    if resultado.startswith(PREFIJOS_ERROR):
        return resultado

    # "Ponle play" sin reproductor abre la radio: se dice eso, no
    # "le doy play de nuevo".
    if nombre == "multimedia" and resultado.startswith("Abriendo tu radio"):
        return resultado

    clave = (
        nombre,
        args.get("accion") if nombre in ("multimedia", "volumen", "bluetooth") else None,
    )
    variantes = NUCLEOS.get(clave)

    if not variantes:
        return resultado

    return random.choice(variantes).format(**args)


# Los tools de Groq se llaman distinto que las órdenes del router; las
# frases de NUCLEOS están con el nombre del router. Sin esto, una orden
# que resolvía Groq se leía cruda: "Se ejecutó la acción: reproducir".
NOMBRE_LOCAL_DE_TOOL = {
    "control_multimedia": "multimedia",
    "ajustar_volumen": "volumen",
    "controlar_bluetooth": "bluetooth",
    "tomar_captura": "captura",
}


def respuesta_local(nombre: str, args: dict, resultado: str) -> str:
    """Genera una respuesta carismática sin gastar tokens de Groq."""
    nombre = NOMBRE_LOCAL_DE_TOOL.get(nombre, nombre)
    if resultado.startswith(PREFIJOS_ERROR) or nombre in RESPUESTAS_COMPLETAS:
        return resultado

    if nombre == "estado_sistema":
        return f"Estos son los parámetros actuales. {resultado}"

    nucleo = nucleo_local(nombre, args, resultado).rstrip(". ")
    return f"{random.choice(MULETILLAS)} {nucleo}."


def ejecutar_comandos_locales(comandos):
    """Ejecuta todos los comandos detectados en orden."""
    resultados = []

    for nombre, args in comandos:
        resultado = ejecutar_comando_local(nombre, args)
        resultados.append((nombre, args, resultado))

        if nombre == "info_cancion":
            texto = resultado
            if texto.startswith("Actualmente está sonando:"):
                memoria_contexto["cancion"] = texto.replace(
                    "Actualmente está sonando:", ""
                ).strip()

        memoria_contexto["ultimo_resultado"] = resultado

    return resultados


def resumir_resultados_locales(resultados):
    """Construye una sola respuesta para varias órdenes."""
    frases = []

    for nombre, args, resultado in resultados:
        if nombre in RESPUESTAS_COMPLETAS:
            return resultado

        # Sin el punto final: se agrega uno solo al armar la frase
        # (antes quedaba "...grados.." en el texto).
        frases.append(nucleo_local(nombre, args, resultado).rstrip(". "))

    if not frases:
        return "Entendido."

    muletilla = random.choice(MULETILLAS)

    if len(frases) == 1:
        return f"{muletilla} {frases[0]}."

    return f"{muletilla} " + "; ".join(frases) + "."


# Temas para elegir qué schemas mandar a Groq cuando el router local no
# entendió la orden. Con una sola palabra del tema, Groq recibe el
# grupo completo (2-3 tools) y decide si ejecutar algo o si solo se le
# está hablando del tema. Son fragmentos de regex que se buscan sin
# tildes y al inicio de palabra, así "pausa" también toma "pausan" o
# "pausar" (con \b al final cuando el prefijo choca, como "ram"). Evitar
# palabras muy comunes ("para", "tema", "tiempo" sola) porque mandarían
# tools de más en cada pregunta. Costo aproximado por grupo: música
# ~220 tokens, apps ~170, volumen ~145.
TEMAS_TOOLS = {
    "musica": (
        ("musica", "cancion", "sonando", "suena", "escuch", "play", "pausa",
         "reproduc", "reanud", "despaus", "siguiente", "skip", "stop",
         "deten", "spotify", "youtube music", "yt music", "radio",
         "playlist", "artista", "cantante", "album", r"rolas?\b"),
        ("control_multimedia", "info_cancion", "abrir_radio_musica"),
    ),
    "volumen": (
        ("volumen", "silenci", "mute", "mutea", "mas fuerte", "mas bajito",
         "mas alto", "mas bajo", "no se escucha", "no escucho"),
        ("ajustar_volumen",),
    ),
    "apps": (
        ("abre", "abrir", "ejecuta", "lanza", "cierra", "cerra", r"mata\b",
         "matar", "termina", "quita"),
        ("abrir_aplicacion", "cerrar_aplicacion"),
    ),
    "ventanas": (
        ("mueve", "mover", "escritorio", "workspace", "ventana"),
        ("mover_ventana",),
    ),
    "clima": (
        ("clima", "temperatura", "lluvia", "llueve", "llov", "pronostico",
         "hace frio", "hace calor", "que tiempo", "el tiempo", "nublado",
         "paraguas"),
        ("reporte_clima",),
    ),
    "sistema": (
        ("estado del sistema", "estado del pc", r"ram\b", "cpu", "procesador",
         "como esta el pc", "como va el pc", "temperatura del pc"),
        ("estado_sistema",),
    ),
    "energia": (
        ("apaga", "reinicia", "suspende", "cierra todo", "cerrar todo",
         "modo panico"),
        ("control_energia", "cerrar_todo"),
    ),
    "actualizaciones": (
        ("actualiza", "paquetes", "update"),
        ("actualizar_sistema", "verificar_actualizaciones"),
    ),
    "bluetooth": (
        ("bluetooth", "audifono", "auricular", "parlante", "conect",
         "desconect", "vincula"),
        ("controlar_bluetooth", "conectar_bluetooth", "desconectar_bluetooth"),
    ),
    "pantalla": (
        ("pantalla", "captura", "screenshot", "que ves", "mira esto"),
        ("tomar_captura", "analizar_pantalla"),
    ),
    "busqueda": (
        ("busca", "investiga", "internet", "en la web", "noticia",
         "informacion reciente"),
        ("buscar_en_web",),
    ),
    "pestanas": (
        ("paper", "articulos", "fuentes", "imagenes", "pestana"),
        ("abrir_pestanas_busqueda", "buscar_en_web"),
    ),
}

PATRONES_TEMAS = {
    tema: re.compile(r"\b(?:" + "|".join(palabras) + ")")
    for tema, (palabras, _) in TEMAS_TOOLS.items()
}

# Frases que llegaron a Groq sin ningún tema: sirven para descubrir
# formas de pedir las cosas que todavía no se reconocen.
LOG_SIN_TEMA = os.getenv(
    "MADELINE_LOG_SIN_TEMA",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "frases_sin_tema.log"),
)


def temas_del_mensaje(texto: str) -> list:
    t = quitar_tildes(texto.lower())
    return [tema for tema, patron in PATRONES_TEMAS.items() if patron.search(t)]


def registrar_frase_sin_tema(texto: str):
    try:
        os.makedirs(os.path.dirname(LOG_SIN_TEMA), exist_ok=True)
        with open(LOG_SIN_TEMA, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M}\t{texto}\n")
    except OSError as e:
        print(f"[!] No pude registrar la frase sin tema: {e}")


def tools_necesarias(texto: str):
    """Envía a Groq solo los schemas de los temas que aparecen en el texto."""
    nombres = set()
    temas = temas_del_mensaje(texto)
    for tema in temas:
        nombres.update(TEMAS_TOOLS[tema][1])

    if not temas:
        registrar_frase_sin_tema(texto)
        # Sin esto, si ningún tema matchea, la solicitud se manda a
        # Groq sin NINGUNA herramienta. openai/gpt-oss-120b, al no
        # tener herramientas pero sentir que la pregunta necesita info
        # actual (como "¿a qué hora lloverá?"), a veces ALUCINA un tool
        # interno de OpenAI (web.run) que no existe en esta API — Groq
        # lo rechaza con 400 (bug conocido de los modelos gpt-oss
        # cuando se quedan sin herramientas). Dándole siempre al menos
        # buscar_en_web como opción real, tiene algo concreto para
        # usar en vez de inventar algo que no existe.
        nombres.add("buscar_en_web")

    return [
        tool for tool in tools
        if tool["function"]["name"] in nombres
    ]


def contexto_para_pregunta(texto: str) -> str:
    """Devuelve solo el contexto mínimo necesario para una pregunta."""
    t = texto.lower()

    if memoria_contexto["cancion"] and any(
        palabra in t
        for palabra in [
            "canción", "cancion", "tema", "artista",
            "álbum", "album", "año", "a quien", "quién",
            "quien", "cantante", "disco"
        ]
    ):
        return (
            "Contexto reciente: la canción consultada fue "
            f"'{memoria_contexto['cancion']}'."
        )

    if memoria_contexto["ultimo_resultado"] and any(
        palabra in t
        for palabra in ["eso", "esa", "ese", "lo anterior", "antes", "respuesta"]
    ):
        return (
            "Contexto reciente: la última respuesta fue: "
            f"{memoria_contexto['ultimo_resultado']}"
        )

    return ""


def ejecutar_comando_local(nombre: str, args: dict):
    """Ejecuta un comando resuelto sin llamar a Groq."""
    if nombre == "confirmar_accion":
        verbo = "apagar" if args.get("accion") == "apagar" else "reiniciar"
        return f"¿Confirmas que quieres {verbo} el sistema, Keric? Di 'sí' para seguir."

    if nombre == "actualizar_sistema":
        return actualizar_sistema()

    if nombre == "verificar_actualizaciones":
        return verificar_actualizaciones()

    if nombre == "confirmacion_cancelada":
        return "Cancelado. No se hizo ningún cambio."

    if nombre == "charla":
        return random.choice(RESPUESTAS_CHARLA[args["tipo"]])

    if nombre == "memoria_guardar":
        return memoria_guardar(**args)

    if nombre == "memoria_olvidar":
        return memoria_olvidar(**args)

    if nombre == "memoria_consultar":
        return memoria_consultar(**args)

    if nombre == "clima":
        return reporte_clima(**args)

    if nombre == "captura":
        return tomar_captura()

    if nombre == "multimedia":
        return control_multimedia(**args)

    if nombre == "radio_musica":
        return abrir_radio_musica()

    if nombre == "abrir_pestanas_busqueda":
        return abrir_pestanas_busqueda(**args)

    if nombre == "analizar_pantalla":
        return analizar_pantalla(**args)

    if nombre == "info_cancion":
        return info_cancion()

    if nombre == "volumen":
        return ajustar_volumen(**args)

    if nombre == "estado_sistema":
        return estado_sistema()

    if nombre == "cerrar_todo":
        return cerrar_todo()

    if nombre == "reiniciar_servidor":
        return reiniciar_servidor_madeline()

    if nombre == "energia":
        return control_energia(**args)

    if nombre == "bluetooth":
        return controlar_bluetooth(**args)

    if nombre == "conectar_bluetooth":
        return conectar_bluetooth(**args)

    if nombre == "desconectar_bluetooth":
        return desconectar_bluetooth(**args)

    if nombre == "recordatorio":
        return programar_recordatorio(**args)

    if nombre == "cerrar_aplicacion":
        return cerrar_aplicacion(**args)

    if nombre == "abrir_aplicacion":
        return abrir_aplicacion(**args)

    if nombre == "mover_ventana":
        return mover_ventana(**args)

    return "Comando local no reconocido."


# ============================================================
# FASTAPI ENDPOINTS
# ============================================================

@app.get("/")
def read_root():
    return {"status": "Madeline API en línea"}


FRASES_BIENVENIDA = [
    "Bienvenido a casa, Keric.",
    "¡Hola de nuevo, Keric!",
    "Sistemas en línea. Bienvenido de vuelta.",
    "Todo listo, Keric. Bienvenido.",
]


async def dar_bienvenida() -> str:
    """Arma y dice el saludo de bienvenida + un reporte del clima.
    Pensada para llamarse UNA sola vez al arrancar la sesión gráfica,
    no algo que el usuario pida por voz."""
    saludo = random.choice(FRASES_BIENVENIDA)
    clima = reporte_clima()
    texto = f"{saludo} {clima}"
    await hablar_por_parlantes(texto)
    return texto


@app.post("/bienvenida")
async def bienvenida_endpoint():
    """Dice la frase de bienvenida + clima por los parlantes del PC.

    Esto NO lo dispara un comando de voz/texto del usuario — lo pensé
    para que lo llame un exec-once de Hyprland (o lo que uses como
    gestor de sesión) apenas arranca la sesión gráfica, justo después
    de poner tu contraseña. Ver las instrucciones de instalación."""
    texto = await dar_bienvenida()
    return {"mensaje": texto}


@app.get("/captura/{captura_id}")
async def obtener_captura(captura_id: str, background_tasks: BackgroundTasks):
    """Sirve la captura con ese ID específico (no 'la última' a secas,
    para no mezclar capturas de solicitudes simultáneas distintas)."""
    ruta = ruta_captura_disponible(captura_id)

    if not ruta:
        raise HTTPException(
            status_code=404,
            detail="Esa captura ya no está disponible (puede que haya expirado)."
        )

    media_type = "image/jpeg" if ruta.endswith(".jpg") else "image/png"
    background_tasks.add_task(os.remove, ruta)

    return FileResponse(ruta, media_type=media_type, background=background_tasks)

# ============================================================
# GROQ (plan gratuito: cada token cuenta)
# ============================================================
# Consumo aproximado por pregunta que llega a Groq: ~170 tokens de
# este prompt + ~90 de tools (tools_necesarias manda solo las que
# hacen falta) + historial + respuesta. Lo que resuelve el router
# local (órdenes, clima, charla corta) no gasta nada.

SYSTEM_PROMPT = """Eres Madeline, la asistente de escritorio de Keric, con el \
carácter de la protagonista de Celeste: cálida, decidida y con ingenio. \
Responde en español, tuteando; llámalo por su nombre a veces, no en cada frase.
Sé breve: máximo 3 frases salvo que pida más detalle. Mejor corto y \
completo que largo y cortado.
Tu respuesta se lee en voz alta: nunca uses markdown, listas ni \
tablas; habla en prosa corrida.
Usa herramientas solo si te piden una acción o un dato que no sabes \
(buscar_en_web solo para información reciente). Nunca inventes resultados."""

SYSTEM_PROMPT_BUSQUEDA = """Eres Madeline. Responde la pregunta en \
español, en 1 a 3 frases de prosa sin markdown (se lee en voz alta), \
usando solo los resultados de búsqueda. Si no alcanzan, dilo."""

DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def system_prompt_con_fecha(prompt: str) -> str:
    # En español a mano: strftime("%A %B") sale en inglés sin locale.
    ahora = datetime.now()
    fecha = (
        f"{DIAS_SEMANA[ahora.weekday()]} {ahora.day} de "
        f"{MESES[ahora.month - 1]} de {ahora.year}, {ahora:%H:%M}"
    )
    return f"{prompt}\nFecha y hora: {fecha}"


class SinCuotaGroq(Exception):
    """Se agotó el límite del plan gratuito (HTTP 429)."""


async def llamar_groq(parametros: dict):
    """Llama a Groq en un hilo aparte, para no congelar el servidor
    (el panel de sensores, cortar la voz, etc.) mientras responde.

    Si gpt-oss alucina una herramienta inexistente (bug conocido),
    reintenta UNA vez sin herramientas. Si se agotó la cuota,
    lanza SinCuotaGroq para responder con una frase en vez de un 500."""
    try:
        return await asyncio.to_thread(client.chat.completions.create, **parametros)
    except RateLimitError as e:
        print(f"[!] Groq sin cuota (429): {e}")
        raise SinCuotaGroq() from e
    except Exception as e:
        error_texto = str(e)

        if "tool_use_failed" not in error_texto and "Tool choice is none" not in error_texto:
            print(f"[ERROR GROQ] {e}")
            raise HTTPException(status_code=500, detail=f"Error al contactar con la IA: {e}")

        print(f"[!] Groq alucinó un tool inexistente, reintentando sin tools: {e}")
        sin_tools = {k: v for k, v in parametros.items() if k not in ("tools", "tool_choice")}

        try:
            return await asyncio.to_thread(client.chat.completions.create, **sin_tools)
        except RateLimitError as e2:
            raise SinCuotaGroq() from e2
        except Exception as e2:
            print(f"[ERROR GROQ] {e2}")
            raise HTTPException(status_code=500, detail=f"Error al contactar con la IA: {e2}")


async def resumir_busqueda(pregunta: str, resultados_web: str) -> str:
    """Segunda llamada, lo más barata posible: sin historial, sin
    tools, prompt corto y snippets recortados (~500 tokens en total)."""
    parametros = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt_con_fecha(SYSTEM_PROMPT_BUSQUEDA)},
            {"role": "user", "content": f"Pregunta: {pregunta}\n\n{resultados_web[:1500]}"},
        ],
        "temperature": 0.2,
        "max_completion_tokens": 250,
        "reasoning_effort": "low",
    }

    try:
        respuesta = await llamar_groq(parametros)
        texto = limpiar_texto_para_voz(respuesta.choices[0].message.content or "")
        if respuesta.choices[0].finish_reason == "length":
            texto = recortar_a_oracion_completa(texto)
        if texto:
            return texto
    except (SinCuotaGroq, HTTPException):
        pass

    # Sin cuota o sin respuesta: al menos el primer resultado, limpio.
    lineas = [l for l in resultados_web.splitlines()[1:] if l.strip()]
    primero = re.sub(r"^\d+\.\s*", "", lineas[0]) if lineas else ""
    return f"Esto es lo que encontré: {primero}" if primero else "No encontré nada útil, lo siento."


MENSAJE_SIN_CUOTA = (
    "Keric, se agotó la cuota gratuita de Groq por ahora. Las órdenes "
    "directas siguen funcionando; las preguntas tendrán que esperar un poco."
)


async def responder_sin_cuota(origen: str) -> ChatResponse:
    # El mensaje del usuario ya quedó en el historial sin respuesta:
    # se saca, para no mandarlo dos veces en la próxima pregunta.
    if historial_chat and historial_chat[-1]["role"] == "user":
        historial_chat.pop()

    print(f"[Madeline] {MENSAJE_SIN_CUOTA}")
    audio = await procesar_salida_de_voz(MENSAJE_SIN_CUOTA, origen)
    return ChatResponse(response=MENSAJE_SIN_CUOTA, audio_path=audio)


# Todas las conversaciones, una por línea (JSON), para revisar después
# qué entendió Madeline y en qué falló. Solo queda en este PC.
LOG_CONVERSACIONES = os.getenv(
    "MADELINE_LOG_CONVERSACIONES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "conversaciones.jsonl"),
)


def registrar_conversacion(mensaje: str, origen: str, registro: dict, respuesta: ChatResponse):
    linea = {
        "fecha": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        "origen": origen,
        "mensaje": mensaje,
        **registro,
        "respuesta": respuesta.response,
    }
    try:
        os.makedirs(os.path.dirname(LOG_CONVERSACIONES), exist_ok=True)
        with open(LOG_CONVERSACIONES, "a", encoding="utf-8") as f:
            f.write(json.dumps(linea, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        print(f"[!] No pude registrar la conversación: {e}")


async def procesar_mensaje(
    request: ChatRequest,
    origen: str = "pc"
):
    # ruta: "router" (orden local), "groq" o "sin_cuota".
    registro = {"ruta": None, "acciones": []}
    respuesta = await _procesar_mensaje(request, origen, registro)
    registrar_conversacion(request.message.strip(), origen.lower().strip(), registro, respuesta)
    return respuesta


async def _procesar_mensaje(
    request: ChatRequest,
    origen: str,
    registro: dict,
):
    origen = origen.lower().strip()

    if origen not in ("pc", "telefono"):
        raise HTTPException(
            status_code=400,
            detail="El origen debe ser 'pc' o 'telefono'."
        )
    global historial_chat, ULTIMA_CAPTURA_ANALISIS
    
    mensaje_usuario = request.message.strip()
    if not mensaje_usuario:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío.")

    print(f"[Usuario] {mensaje_usuario}")
    
    image_path_response = None

    # 2. ROUTER LOCAL
    comandos = router_local(mensaje_usuario)

    # Datos que Keric cuenta al pasar ("estoy aprendiendo Flutter"):
    # se guardan sin interrumpir lo que sea que pidió.
    if not any(nombre == "memoria_guardar" for nombre, _ in comandos or []):
        dato = memoria.dato_personal(mensaje_usuario)
        if dato:
            print(f"[*] Memoria: dato personal -> {dato}")
            memoria.guardar_en_segundo_plano(dato)

    if comandos:
        print(f"[*] Router local: {comandos}")
        registro["ruta"] = "router"
        registro["acciones"] = [[nombre, args] for nombre, args in comandos]

        resultados = ejecutar_comandos_locales(comandos)

        for nombre, args, resultado in resultados:
            print(
                f"[*] Resultado local: "
                f"{nombre} -> {resultado}"
            )

        captura_id_local = None

        for nombre, args, resultado in resultados:
            if nombre == "captura" and resultado.startswith("CAPTURA_LISTA"):
                captura_id_local = resultado.split(":", 1)[1]
                break

        texto_final = resumir_resultados_locales(
            resultados
        )

        # --------------------------------------------------------
        # CAPTURA
        # --------------------------------------------------------

        if captura_id_local and ruta_captura_disponible(captura_id_local):
            image_path_response = f"/captura/{captura_id_local}"
            # Desde el celular la app ya la recibe en la respuesta; la
            # pedida desde el PC queda en la bandeja para el celular.
            if origen == "pc":
                enviar_captura_al_telefono(captura_id_local)

        # --------------------------------------------------------
        # ANÁLISIS DE PANTALLA (también devuelve la foto analizada)
        # --------------------------------------------------------

        hubo_analisis_pantalla = any(
            nombre == "analizar_pantalla" for nombre, args, resultado in resultados
        )

        if hubo_analisis_pantalla and ULTIMA_CAPTURA_ANALISIS:
            if ruta_captura_disponible(ULTIMA_CAPTURA_ANALISIS):
                image_path_response = f"/captura/{ULTIMA_CAPTURA_ANALISIS}"
            ULTIMA_CAPTURA_ANALISIS = None

        # --------------------------------------------------------
        # VOZ
        # --------------------------------------------------------

        audio_path_response = await procesar_salida_de_voz(
            texto_final,
            origen
        )

        return ChatResponse(
            response=texto_final,
            image_path=image_path_response,
            audio_path=audio_path_response
        )

    # 3. SOLO SI NO ES UN COMANDO LOCAL: usamos Groq con contexto mínimo.
    historial_chat.append({"role": "user", "content": mensaje_usuario})

    if len(historial_chat) > MAX_HISTORIAL:
        historial_chat = historial_chat[-MAX_HISTORIAL:]

    mensajes = [{"role": "system", "content": system_prompt_con_fecha(SYSTEM_PROMPT)}]

    contexto = contexto_para_pregunta(mensaje_usuario)
    if contexto:
        mensajes.append({"role": "system", "content": contexto})

    # Recuerdos a largo plazo: solo los 2-3 relevantes, o nada.
    recuerdos = await asyncio.to_thread(memoria.contexto_recuerdos, mensaje_usuario)
    if recuerdos:
        print(f"[*] Memoria: {recuerdos}")
        mensajes.append({"role": "system", "content": recuerdos})

    mensajes.extend(historial_chat)
    tools_mensaje = tools_necesarias(mensaje_usuario)
    registro["ruta"] = "groq"
    registro["tools_enviadas"] = [t["function"]["name"] for t in tools_mensaje]

    parametros = {
        "model": MODEL,
        "messages": mensajes,
        "temperature": 0.2,
        # gpt-oss espera max_completion_tokens (no max_tokens), y ese
        # límite incluye los tokens de razonamiento. 350 deja margen
        # para explicaciones sin ser un cheque en blanco: la regla de
        # brevedad del prompt hace que casi nunca se use entero.
        "max_completion_tokens": 350,
        "reasoning_effort": "low",
    }

    if tools_mensaje:
        parametros["tools"] = tools_mensaje
        parametros["tool_choice"] = "auto"

    try:
        response = await llamar_groq(parametros)
    except SinCuotaGroq:
        registro["ruta"] = "sin_cuota"
        return await responder_sin_cuota(origen)

    response_message = response.choices[0].message

    # Si el modelo se quedó sin tokens antes de terminar la idea,
    # esto queda en "length" en vez de "stop" — lo usamos más abajo
    # para recortar prolijo en vez de leer algo cortado a mitad.
    finish_reason = response.choices[0].finish_reason

    # 5. TOOL CALL
    if response_message.tool_calls:
        resultados_tool = []

        for tool_call in response_message.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            print(f"[*] Tool call: {fn_name} {fn_args}")
            registro["acciones"].append([fn_name, fn_args])

            if fn_name == "control_energia":
                # Igual que en router_local: apagar/reiniciar NUNCA se
                # ejecuta directo, queda pendiente hasta que el usuario
                # diga "sí". Antes, una frase que no matcheaba el regex
                # local ("apaga todo por favor") llegaba acá y apagaba
                # sin preguntar.
                accion = fn_args.get("accion")
                if accion in ("apagar", "reiniciar"):
                    memoria_contexto["confirmacion_pendiente"] = {"accion": accion}
                    fn_name = "confirmar_accion"
                    resultado = ejecutar_comando_local(
                        "confirmar_accion", {"accion": accion}
                    )
                else:
                    resultado = "No pude entender la acción de energía."
            elif fn_name in TOOLS_FUNCIONES:
                try:
                    resultado = TOOLS_FUNCIONES[fn_name](**fn_args)
                except TypeError as e:
                    # Groq a veces inventa un argumento que la función
                    # no acepta: mejor un error hablado que un 500.
                    resultado = f"Error: argumentos inválidos para {fn_name}: {e}"
            else:
                resultado = "Error: Herramienta no encontrada."

            if fn_name == "info_cancion" and resultado.startswith("Actualmente está sonando:"):
                memoria_contexto["cancion"] = resultado.replace(
                    "Actualmente está sonando:", ""
                ).strip()

            if fn_name == "tomar_captura" and resultado.startswith("CAPTURA_LISTA"):
                captura_id_groq = resultado.split(":", 1)[1]
                if ruta_captura_disponible(captura_id_groq):
                    image_path_response = f"/captura/{captura_id_groq}"
                    if origen == "pc":
                        enviar_captura_al_telefono(captura_id_groq)

            if fn_name == "buscar_en_web" and not resultado.startswith(PREFIJOS_ERROR):
                # Antes los snippets se leían en bruto ("Resultados web
                # para X: 1. ... 2. ..."). Ahora una segunda llamada,
                # mínima, los convierte en una respuesta de verdad.
                resultado = await resumir_busqueda(mensaje_usuario, resultado)
                fn_name = "respuesta_busqueda"

            resultados_tool.append((fn_name, fn_args, resultado))
            memoria_contexto["ultimo_resultado"] = str(resultado)

        texto_final = " ".join(
            respuesta_local(nombre, args, resultado)
            for nombre, args, resultado in resultados_tool
            if not resultado.startswith("CAPTURA_LISTA")
        )
        texto_final = limpiar_texto_para_voz(texto_final)

        if not texto_final:
            texto_final = "Listo, hecho."

        # Mismo mecanismo que en la rama del router local: si Groq
        # decidió llamar a analizar_pantalla por su cuenta (frase
        # libre, no el patrón fijo), también devolvemos la foto
        # analizada junto con la respuesta hablada.
        hubo_analisis_pantalla = any(
            nombre == "analizar_pantalla" for nombre, args, resultado in resultados_tool
        )

        if hubo_analisis_pantalla and ULTIMA_CAPTURA_ANALISIS:
            if ruta_captura_disponible(ULTIMA_CAPTURA_ANALISIS):
                image_path_response = f"/captura/{ULTIMA_CAPTURA_ANALISIS}"
            ULTIMA_CAPTURA_ANALISIS = None

    else:
        # Respuesta normal sin herramienta — acá es exactamente donde
        # aparecía la tabla markdown, así que pasa por la limpieza.
        texto_final = limpiar_texto_para_voz(
            response_message.content or "No tengo nada que añadir."
        )

        if finish_reason == "length":
            texto_final = recortar_a_oracion_completa(texto_final)

    # 6. MEMORIA
    historial_chat.append({"role": "assistant", "content": texto_final})

    # Se guardan las charlas y búsquedas, no las órdenes ("abre
    # Firefox" por Groq no es algo que valga la pena recordar).
    if not response_message.tool_calls or any(
        nombre == "respuesta_busqueda" for nombre, _, _ in resultados_tool
    ):
        memoria.guardar_conversacion(mensaje_usuario, texto_final)
    if len(historial_chat) > MAX_HISTORIAL:
        historial_chat = historial_chat[-MAX_HISTORIAL:]

    print(f"[Madeline] {texto_final}")

    audio_path_response = await procesar_salida_de_voz(
        texto_final,
        origen
    )

    return ChatResponse(
        response=texto_final,
        image_path=image_path_response,
        audio_path=audio_path_response
    )

# Proceso de mpv reproduciendo la voz de Madeline en el PC ahora mismo,
# o None si no está hablando. Se usa para poder interrumpirlo cuando
# arranca una grabación nueva (ver interrumpir_voz_pc()).
PROCESO_VOZ_PC = None


# Voz femenina mexicana (la más natural en las pruebas); se puede cambiar con MADELINE_VOZ en el .env
# (edge-tts --list-voices | grep es-).
VOZ_EDGE = os.getenv("MADELINE_VOZ", "es-MX-DaliaNeural").strip()

# Efecto de "asistente digital" sobre la voz: eco corto de
# sala y brillo en los agudos (sin bajarle el tono, para que siga
# sonando como ella). No clona ninguna
# voz real, solo procesa la de edge-tts. Se elige en el .env con
# MADELINE_EFECTO_VOZ=sutil | holografico | ninguno. Cuesta ~0.25 s.
FILTROS_VOZ = {
    "sutil": (
        "highpass=f=90,equalizer=f=250:t=q:w=1:g=2,"
        "equalizer=f=3500:t=q:w=1.2:g=2.5,"
        "aecho=0.8:0.6:28|47:0.16|0.09,volume=1.4,alimiter=limit=0.9"
    ),
    "holografico": (
        "highpass=f=110,equalizer=f=3000:t=q:w=1:g=3,"
        "chorus=0.7:0.9:20|32:0.25|0.18:0.3|0.25:2|2.3,"
        "aecho=0.8:0.7:35|60:0.22|0.12,volume=1.3,alimiter=limit=0.9"
    ),
}
EFECTO_VOZ = os.getenv("MADELINE_EFECTO_VOZ", "ninguno").strip().lower()


# Voz de respaldo para cuando edge-tts falla (sin internet, o Microsoft
# cambia algo como en octubre de 2026 y Madeline quedó muda). Usa Piper si
# está instalado (piper-tts-bin del AUR + un modelo .onnx en
# MADELINE_PIPER_MODELO) y si no espeak-ng. Es binario "piper-tts" y no
# "piper": en Arch "piper" es el configurador de ratones gamer.
PIPER_MODELO = os.getenv("MADELINE_PIPER_MODELO", "").strip()


async def generar_voz_local(texto: str, ruta_salida: str) -> bool:
    """Genera la voz con un motor local en ruta_salida. Devuelve False
    si no hay ningún motor instalado o si falló."""
    ruta_wav = f"{ruta_salida}.local.wav"

    if PIPER_MODELO and shutil.which("piper-tts"):
        comando = ["piper-tts", "--model", PIPER_MODELO, "--output_file", ruta_wav]
        entrada = texto.encode()
    elif shutil.which("espeak-ng"):
        comando = ["espeak-ng", "-v", "es-419+f3", "-s", "165", "-w", ruta_wav, texto]
        entrada = None
    else:
        print("[!] No hay voz local de respaldo: instala espeak-ng o piper-tts.")
        return False

    try:
        proceso = await asyncio.create_subprocess_exec(
            *comando,
            stdin=subprocess.PIPE if entrada else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await proceso.communicate(entrada)

        if proceso.returncode != 0 or not os.path.exists(ruta_wav):
            return False

        # Se pasa a MP3 porque el archivo se llama .mp3 y el teléfono lo
        # pide así. Sin ffmpeg queda el WAV: mpv lo reproduce igual.
        if not shutil.which("ffmpeg"):
            os.replace(ruta_wav, ruta_salida)
            return True

        proceso = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", ruta_wav, "-c:a", "libmp3lame", "-q:a", "3", ruta_salida,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await proceso.communicate()
        return proceso.returncode == 0

    finally:
        if os.path.exists(ruta_wav):
            os.remove(ruta_wav)


async def generar_voz(texto: str, ruta_salida: str):
    """Genera la voz de Madeline en ruta_salida con edge-tts y, si falla,
    con la voz local de respaldo. Solo lanza la excepción de edge-tts
    si tampoco hay voz local."""
    try:
        await generar_voz_edge(texto, ruta_salida)
    except Exception as e:
        print(f"[!] edge-tts falló, uso la voz local de respaldo: {e}")
        if not await generar_voz_local(texto, ruta_salida):
            raise


async def generar_voz_edge(texto: str, ruta_salida: str):
    """Genera la voz de Madeline en ruta_salida (MP3), con el efecto de
    EFECTO_VOZ si hay ffmpeg. Si el efecto falla, queda la voz limpia:
    nunca te quedas sin respuesta por culpa del filtro."""
    voz = edge_tts.Communicate(texto, voice=VOZ_EDGE)
    filtro = FILTROS_VOZ.get(EFECTO_VOZ)

    if not filtro or not shutil.which("ffmpeg"):
        await voz.save(ruta_salida)
        return

    ruta_cruda = f"{ruta_salida}.crudo.mp3"

    try:
        await voz.save(ruta_cruda)

        proceso = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", ruta_cruda, "-af", filtro,
            "-c:a", "libmp3lame", "-q:a", "3", ruta_salida,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        _, error = await proceso.communicate()

        if proceso.returncode != 0:
            print(f"[!] Falló el efecto de voz, uso la voz limpia: {error.decode()[:200]}")
            os.replace(ruta_cruda, ruta_salida)

    finally:
        if os.path.exists(ruta_cruda):
            os.remove(ruta_cruda)


def interrumpir_voz_pc():
    """Corta lo que Madeline esté diciendo en el PC ahora mismo, si hay
    algo sonando. Se llama apenas arranca una grabación nueva, para
    que la voz de Madeline no se solape con lo que estés por decir."""
    global PROCESO_VOZ_PC

    if PROCESO_VOZ_PC is not None and PROCESO_VOZ_PC.poll() is None:
        try:
            PROCESO_VOZ_PC.terminate()
        except Exception as e:
            print(f"[!] No se pudo interrumpir la voz: {e}")

    PROCESO_VOZ_PC = None


async def hablar_por_parlantes(texto: str):
    """
    Genera la voz de Madeline y la reproduce SOLO en los parlantes del PC.
    """
    global PROCESO_VOZ_PC

    if not texto:
        return

    # Nombre único: si un recordatorio sonaba mientras Madeline hablaba,
    # los dos escribían (y borraban) el mismo archivo.
    ruta_respuesta = f"/tmp/respuesta_madeline_pc_{uuid.uuid4().hex[:12]}.mp3"
    proceso = None

    try:
        await generar_voz(texto, ruta_respuesta)

        # Popen (no subprocess.run) + espera con asyncio.sleep: así NO
        # se bloquea el event loop mientras habla, y el servidor puede
        # seguir atendiendo otras solicitudes en paralelo — sin esto,
        # una grabación nueva ni siquiera podría LLEGAR a interrumpir
        # nada, porque el servidor entero estaría trabado esperando a
        # que termine de hablar.
        proceso = subprocess.Popen(
            ["mpv", "--no-video", ruta_respuesta],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        PROCESO_VOZ_PC = proceso

        while proceso.poll() is None:
            await asyncio.sleep(0.1)

    except Exception as e:
        print(f"[!] Falló la voz local del PC: {e}")

    finally:
        if proceso is not None and PROCESO_VOZ_PC is proceso:
            PROCESO_VOZ_PC = None
        if os.path.exists(ruta_respuesta):
            try:
                os.remove(ruta_respuesta)
            except Exception:
                pass


MINUTOS_VIDA_ARCHIVOS = 10


def limpiar_archivos_viejos():
    """Borra audios para el teléfono y capturas con más de 10 minutos.
    Antes los audios se acumulaban para siempre en /tmp/madeline_audios,
    y las capturas pedidas desde el PC (que nadie descarga por
    /captura/{id}) también quedaban en /tmp."""
    limite = time.time() - MINUTOS_VIDA_ARCHIVOS * 60
    candidatos = [os.path.join(CARPETA_AUDIOS, n) for n in os.listdir(CARPETA_AUDIOS)]
    candidatos += [
        os.path.join("/tmp", n) for n in os.listdir("/tmp")
        if n.startswith("captura_madeline_")
    ]

    for ruta in candidatos:
        try:
            if os.path.getmtime(ruta) < limite:
                os.remove(ruta)
        except OSError:
            pass

    limite_bandeja = time.time() - HORAS_VIDA_BANDEJA * 3600
    for captura in capturas_en_bandeja():
        if captura["creada"] < limite_bandeja:
            try:
                os.remove(captura["ruta"])
            except OSError:
                pass


async def generar_audio_para_telefono(texto: str) -> str | None:
    """
    Genera la voz de Madeline para el teléfono.

    NO reproduce el audio en el PC.
    Simplemente genera el archivo y devuelve su ruta HTTP.
    """

    if not texto:
        return None

    limpiar_archivos_viejos()

    nombre_archivo = f"respuesta_madeline_{uuid.uuid4().hex[:12]}.mp3"

    ruta_archivo = os.path.join(
        CARPETA_AUDIOS,
        nombre_archivo
    )

    try:
        await generar_voz(texto, ruta_archivo)

        print(
            f"[*] Audio para teléfono generado: "
            f"{ruta_archivo}"
        )

        # CARPETA_AUDIOS está montada como /audios
        return f"/audios/{nombre_archivo}"

    except Exception as e:
        print(
            f"[!] Falló la generación de voz "
            f"para teléfono: {e}"
        )

        if os.path.exists(ruta_archivo):
            try:
                os.remove(ruta_archivo)
            except Exception:
                pass

        return None

async def procesar_salida_de_voz(
    texto: str,
    origen: str
) -> str | None:
    """
    Decide dónde debe salir la voz de Madeline.

    PC:
        reproduce localmente y devuelve None.

    TELEFONO:
        genera un archivo para que Flutter lo reproduzca.
    """

    origen = origen.lower().strip()

    if origen == "telefono":
        return await generar_audio_para_telefono(texto)

    # Por defecto todo lo que no sea teléfono
    # se considera PC.
    await hablar_por_parlantes(texto)

    return None

PISTA_VOCABULARIO = (
    "Uso a Madeline para controlar mi computadora: "
    "abro Steam, Discord, Zen Browser, "
    "Visual Studio Code, Heroic y Prism Launcher; "
    "conecto el Bluetooth a mi Alexa o a mis audífonos Buds; "
    "controlo el volumen, tomo capturas "
    "y muevo ventanas entre escritorios."
)

ALUCINACIONES_WHISPER = (
    r"^\W*(?:subt[ií]tulos (?:realizados|por)|amara\.org|"
    r"suscr[ií]bete|gracias por ver|¡?gracias por ver el video)"
)


@app.post("/callar")
async def callar_endpoint():
    """Corta lo que Madeline esté diciendo en el PC. Lo llama
    madeline_mic.sh apenas presionas Win+J: antes la voz recién se
    cortaba cuando TERMINABAS de grabar (al llegar el audio), así que
    el micrófono grababa a Madeline hablando encima de tu orden."""
    interrumpir_voz_pc()
    return {"ok": True}


@app.post(
    "/comando_audio",
    response_model=ChatResponse
)
async def comando_audio_endpoint(
    audio: UploadFile = File(...),
    origen: str = "pc"
):
    """
    Recibe audio, lo transcribe con Whisper
    y lo envía al cerebro principal de Madeline.

    origen=pc:
        Madeline habla por los parlantes del PC.

    origen=telefono:
        Madeline genera el audio y lo devuelve
        para que Flutter lo reproduzca.
    """

    origen = origen.lower().strip()

    if origen not in ("pc", "telefono"):
        raise HTTPException(
            status_code=400,
            detail="El origen debe ser 'pc' o 'telefono'."
        )

    # Por si el cliente no llamó a /callar al empezar a grabar.
    if origen == "pc":
        interrumpir_voz_pc()

    try:
        # Los bytes van directo a Whisper: antes se escribían a /tmp y
        # se volvían a leer, usando además el nombre de archivo que
        # mandaba el cliente.
        contenido = await audio.read()

        transcripcion = await asyncio.to_thread(
            client.audio.transcriptions.create,
            file=(audio.filename or "audio.wav", contenido),
            model="whisper-large-v3",
            language="es",
            prompt=PISTA_VOCABULARIO,
        )

        mensaje_usuario = transcripcion.text.strip()

    except RateLimitError:
        return await responder_sin_cuota(origen)

    except Exception as e:
        print(f"[ERROR WHISPER] {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error procesando el audio: {e}"
        )

    # Whisper "alucina" estas frases con audio vacío o puro ruido. Sin
    # este filtro, una grabación en silencio terminaba en una llamada
    # a Groq por "Subtítulos realizados por la comunidad de Amara.org".
    if re.search(ALUCINACIONES_WHISPER, mensaje_usuario, re.IGNORECASE):
        mensaje_usuario = ""

    # --------------------------------------------------------
    # 3. VALIDAR TRANSCRIPCIÓN
    # --------------------------------------------------------

    if not mensaje_usuario:
        raise HTTPException(
            status_code=400,
            detail="No se detectó voz."
        )

    print(
        f"[🎙️ Voz detectada - {origen}]: "
        f"{mensaje_usuario}"
    )

    # --------------------------------------------------------
    # 4. ENVIAR AL CEREBRO PRINCIPAL
    # --------------------------------------------------------

    request_data = ChatRequest(
        message=mensaje_usuario
    )

    resultado = await procesar_mensaje(
        request_data,
        origen=origen
    )

    # IMPORTANTE:
    #
    # Ya NO hacemos:
    #
    # await hablar_por_parlantes(...)
    #
    # porque procesar_mensaje() ya decidió dónde
    # debe salir la voz.
    #

    resultado.transcripcion = mensaje_usuario
    return resultado


@app.post(
    "/comando_texto",
    response_model=ChatResponse
)
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

    print(
        f"[⌨️ Texto - {origen}]: "
        f"{comando.texto}"
    )

    request_data = ChatRequest(
        message=comando.texto
    )

    resultado = await procesar_mensaje(
        request_data,
        origen=origen
    )

    return resultado

@app.get("/bandeja")
def bandeja_endpoint():
    """Capturas pedidas desde el PC que el celular todavía no bajó."""
    return {"capturas": [{"id": c["id"], "creada": c["creada"]} for c in capturas_en_bandeja()]}


@app.get("/bandeja/{captura_id}")
def bandeja_captura_endpoint(captura_id: str, background_tasks: BackgroundTasks):
    """Entrega una captura de la bandeja y la borra (se baja una vez)."""
    captura = next((c for c in capturas_en_bandeja() if c["id"] == captura_id), None)
    if not captura:
        raise HTTPException(status_code=404, detail="Esa captura ya no está en la bandeja.")
    background_tasks.add_task(os.remove, captura["ruta"])
    media_type = "image/jpeg" if captura["ruta"].endswith(".jpg") else "image/png"
    return FileResponse(captura["ruta"], media_type=media_type, background=background_tasks)


@app.get("/sensores")
def sensores_endpoint():
    """Estado de hardware en tiempo real para el panel del celular.

    Es "def" (no "async def") a propósito: FastAPI lo corre en un hilo
    aparte, así la consulta cada 2 s del celular no frena al resto del
    servidor. Mantiene el formato de siempre (strings, "N/A" si no hay
    dato) para no tener que cambiar la app."""
    try:
        s = leer_sensores()
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

if __name__ == "__main__":
    import uvicorn
    print("Iniciando Madeline FastAPI en http://0.0.0.0:8000")
    # Recuerdos guardados con Ollama apagado: se completan de fondo.
    threading.Thread(target=memoria.rellenar_embeddings, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000)