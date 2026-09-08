import asyncio
import difflib
import os
import random
import re
import json
import shutil
import subprocess
import time
import urllib.parse
import base64
import uuid
from datetime import datetime
from typing import List, Optional
import edge_tts
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from groq import Groq
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ============================================================
# CONFIGURACIÓN
# ============================================================

load_dotenv(override=True)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not GROQ_API_KEY:
    raise RuntimeError("Falta GROQ_API_KEY en el archivo .env")

client = Groq(api_key=GROQ_API_KEY)

# Modelo pequeño para interpretar comandos ambiguos.
# Los comandos simples se resuelven localmente y NO llegan a Groq.
MODEL = "openai/gpt-oss-120b"
# llama-3.3-70b-versatile fue dado de baja por Groq (anunciado el 17
# de junio de 2026). Este es el reemplazo que ellos mismos recomiendan
# para casos con function calling — sigue soportando tools igual.

# Solo mantenemos un turno anterior para conversaciones que realmente
# necesiten contexto. Los comandos locales no usan esta memoria.
historial_chat = []
MAX_HISTORIAL = 2

# Memoria contextual ligera: no se envía a Groq salvo que una pregunta
# realmente necesite contexto.
memoria_contexto = {
    "cancion": None,
    "ultimo_resultado": None,
    "ultimo_tema": None,
    # Acción destructiva esperando un "sí"/"no" del usuario antes de
    # ejecutarse (ver sección de energía en router_local). None = no
    # hay nada pendiente.
    "confirmacion_pendiente": None,
}

app = FastAPI(title="Jarvis API", description="API para el asistente Jarvis optimizado con Groq")

# Carpeta dedicada SOLO para audios que Jarvis genera para el teléfono.
# Antes esto montaba /tmp completo — cualquiera que llegara al server
# podía pedir cualquier archivo temporal del sistema, no solo los
# audios de Jarvis, con solo adivinar el nombre.
CARPETA_AUDIOS = "/tmp/jarvis_audios"
os.makedirs(CARPETA_AUDIOS, exist_ok=True)
app.mount("/audios", StaticFiles(directory=CARPETA_AUDIOS), name="audios")

class ChatRequest(BaseModel):
    message: str

class ChatResponse(BaseModel):
    response: str
    image_path: Optional[str] = None
    audio_path: Optional[str] = None

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
    nombre_tecnico = DICCIONARIO_APPS.get(app_name.lower(), app_name.lower())
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
    acciones_validas = {
        "pausar": "play-pause",
        "reproducir": "play-pause",
        "siguiente": "next",
        "anterior": "previous",
        "reiniciar": "previous",
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

        subprocess.run(["playerctl", comando], check=True)
        return f"Se ejecutó la acción: {accion}."

    except Exception as e:
        return f"Pucha, no pude controlar la música: {e}"


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

        nombre_tecnico = DICCIONARIO_APPS.get("navegador", "zen-browser")

        # "-new-tab": pestaña nueva EN LA VENTANA QUE YA ESTÁ ABIERTA,
        # no una ventana aparte.
        subprocess.Popen([
            "hyprctl", "dispatch", "exec",
            f'{nombre_tecnico} -new-tab "{RADIO_YOUTUBE_MUSIC}"',
        ])

        return "Abriendo tu radio en YouTube Music"

    except Exception as e:
        return f"No pude abrir la radio: {e}"


def reiniciar_servidor_jarvis() -> str:
    """Reinicia el PROCESO de Jarvis (no el sistema operativo) a
    través de systemd, para poder aplicar cambios de código después
    de editar el archivo, sin necesitar una terminal.

    Necesita que el server corra como servicio systemd --user llamado
    'jarvis-server' (ver instrucciones de instalación) — si no está
    armado así, esto no hace nada."""
    try:
        subprocess.Popen(
            ["systemctl", "--user", "restart", "jarvis-server"]
        )
        return (
            "Reiniciando el servidor de Jarvis. "
            "Va a tardar unos segundos en volver."
        )
    except Exception as e:
        return f"No pude reiniciar el servidor: {e}"


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
            f"terminal aparte. Puede que le pida su contraseña."
        )

    except Exception as e:
        return f"No pude iniciar la actualización: {e}"


def verificar_actualizaciones() -> str:
    """Cuenta cuántos paquetes tienen actualización pendiente, SIN
    aplicarlas — para responder '¿tengo actualizaciones pendientes?'
    sin disparar la actualización completa."""
    try:
        gestor = gestor_paquetes_disponible()

        if shutil.which("checkupdates"):
            resultado = subprocess.run(
                ["checkupdates"], capture_output=True, text=True, timeout=20
            )
            lineas = [l for l in resultado.stdout.strip().splitlines() if l]
        else:
            resultado = subprocess.run(
                [gestor, "-Qu"], capture_output=True, text=True, timeout=20
            )
            lineas = [l for l in resultado.stdout.strip().splitlines() if l]

        if not lineas:
            return "El sistema ya está al día, no hay actualizaciones pendientes."

        return f"Hay {len(lineas)} paquetes con actualización pendiente."

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
    texto = f"Señor, le recuerdo: {mensaje}."
    print(f"[RECORDATORIO] {texto}")
    await hablar_por_parlantes(texto)


def programar_recordatorio(cantidad: int, unidad: str, mensaje: str) -> str:
    """Programa el recordatorio en background y confirma de inmediato."""
    segundos = segundos_desde_unidad(cantidad, unidad)
    asyncio.create_task(recordatorio_en_segundo_plano(segundos, mensaje))
    return f"Recordatorio programado para dentro de {cantidad} {unidad}: {mensaje}."


def cerrar_todo() -> str:
    """Mata todas las aplicaciones comunes de una pasada."""
    apps_a_cerrar = ["zen", "zen-browser", "prismlauncher", "java", "bottles"]

    try:
        for app_name in apps_a_cerrar:
            subprocess.run(["killall", app_name], stderr=subprocess.DEVNULL)

        return "Protocolo de limpieza ejecutado. Se cerraron navegadores y juegos."

    except Exception as e:
        return f"Hubo un drama cerrando todo: {e}"


# wttr.in devuelve la condición del clima en inglés. Diccionario de
# traducción para las condiciones más comunes que devuelve la API.
TRADUCCION_CLIMA = {
    "clear": "despejado",
    "sunny": "soleado",
    "partly cloudy": "parcialmente nublado",
    "cloudy": "nublado",
    "overcast": "muy nublado",
    "mist": "neblina",
    "fog": "niebla",
    "freezing fog": "niebla helada",
    "patchy rain possible": "posibilidad de lluvia aislada",
    "patchy rain nearby": "lluvia aislada cerca",
    "patchy light rain": "lluvia ligera aislada",
    "light rain": "lluvia ligera",
    "light rain shower": "chubascos ligeros",
    "moderate rain": "lluvia moderada",
    "moderate rain at times": "lluvia moderada intermitente",
    "heavy rain": "lluvia fuerte",
    "heavy rain at times": "lluvia fuerte intermitente",
    "torrential rain shower": "lluvia torrencial",
    "patchy light drizzle": "llovizna ligera aislada",
    "light drizzle": "llovizna ligera",
    "thundery outbreaks possible": "posibilidad de tormenta eléctrica",
    "patchy light snow": "nieve ligera aislada",
    "light snow": "nieve ligera",
    "moderate snow": "nieve moderada",
    "heavy snow": "nieve fuerte",
    "blizzard": "ventisca",
    "patchy sleet possible": "posibilidad de aguanieve aislada",
    "ice pellets": "granizo",
    "moderate or heavy rain shower": "chubascos moderados o fuertes",
    "moderate or heavy snow showers": "nevadas moderadas o fuertes",
    "patchy freezing drizzle possible": "posibilidad de llovizna helada aislada",
}


def traducir_condicion_clima(condicion: str) -> str:
    """Traduce una condición climática de wttr.in (inglés) al español.
    Si no está en el diccionario, devuelve la original tal cual
    (mejor mostrar algo en inglés que romper la respuesta)."""
    return TRADUCCION_CLIMA.get(condicion.strip().lower(), condicion)


def reporte_clima() -> str:
    """Obtiene el clima de Villa Alegre mediante wttr.in, en español."""
    try:
        # %C = condición (texto, en inglés) / %t = temperatura
        url = (
            "https://wttr.in/Villa+Alegre,Chile"
            "?format=%C|%t"
        )

        respuesta = requests.get(url, timeout=5)

        if respuesta.status_code != 200 or "|" not in respuesta.text:
            return "No pude conectar con el satélite del clima."

        condicion_cruda, temperatura = respuesta.text.strip().split("|", 1)
        condicion_es = traducir_condicion_clima(condicion_cruda)
        temperatura = temperatura.replace("+", "").strip()

        return (
            f"Actualmente está {condicion_es} en Villa Alegre, "
            f"con una temperatura de {temperatura}."
        )

    except Exception as e:
        return f"Error consultando el clima: {e}"


def estado_sistema() -> str:
    """Lee la memoria RAM, porcentaje de CPU y temperaturas para que Jarvis hable."""
    try:
        # RAM
        ram = subprocess.check_output(
            "free -h | awk '/^Mem:/ {print $3 \" ocupadas de \" $2}'",
            shell=True
        ).decode("utf-8").strip()

        # CPU Uso
        cpu = subprocess.check_output(
            "top -bn1 | grep 'Cpu(s)' | awk '{print $2 + $4}'",
            shell=True
        ).decode("utf-8").strip()

        # CPU Temp
        try:
            cpu_temp_raw = subprocess.check_output(
                "cat /sys/class/thermal/thermal_zone0/temp", shell=True
            ).decode().strip()
            cpu_temp = f"{int(cpu_temp_raw) // 1000}°C"
        except Exception:
            cpu_temp = "desconocida"

        # GPU NVIDIA
        try:
            gpu_info = subprocess.check_output(
                "nvidia-smi --query-gpu=utilization.gpu,temperature.gpu "
                "--format=csv,noheader,nounits",
                shell=True
            ).decode("utf-8").strip()
            gpu_uso, gpu_temp = gpu_info.split(', ')
            info_gpu = (
                f"La tarjeta gráfica NVIDIA está al {gpu_uso}% "
                f"de uso con {gpu_temp}°C."
            )
        except Exception:
            info_gpu = "La tarjeta de video dedicada está en reposo."

        return (
            f"Estado actual: RAM {ram}. Procesador al {cpu}% "
            f"con una temperatura de {cpu_temp}. {info_gpu}"
        )

    except Exception as e:
        return f"No pude leer los sensores: {e}"


def tomar_captura() -> str:
    """Saca la captura usando grim en Hyprland y la comprime a JPEG.

    Cada captura se guarda con un ID único (uuid) en vez de un nombre
    fijo, para que dos solicitudes simultáneas (PC + teléfono al
    mismo tiempo) no se pisen la captura una a la otra. El ID viaja
    codificado en el resultado como "CAPTURA_LISTA:<id>"."""
    captura_id = uuid.uuid4().hex[:12]
    ruta_png = f"/tmp/captura_jarvis_{captura_id}.png"

    try:
        subprocess.run(
            ["grim", ruta_png],
            check=True,
            timeout=10,
        )

        try:
            from PIL import Image

            ruta_jpg = f"/tmp/captura_jarvis_{captura_id}.jpg"

            with Image.open(ruta_png) as img:
                img = img.convert("RGB")

                if img.width > 1920:
                    ratio = 1920 / img.width
                    img = img.resize(
                        (1920, int(img.height * ratio))
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
    ruta_jpg = f"/tmp/captura_jarvis_{captura_id}.jpg"
    ruta_png = f"/tmp/captura_jarvis_{captura_id}.png"

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
# que chat_endpoint pueda devolverla como imagen junto con la
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
    disponible para que chat_endpoint también te la devuelva como
    imagen (ver ULTIMA_CAPTURA_ANALISIS más abajo), igual que ya hace
    el comando de captura normal."""
    global ULTIMA_CAPTURA_ANALISIS
    ULTIMA_CAPTURA_ANALISIS = None

    resultado_captura = tomar_captura()

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

        # Guardamos el ID para que chat_endpoint devuelva también la
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


def enviar_captura_telegram(captura_id: str):
    """Envía la captura con ese ID directamente a tu Telegram."""
    ruta = ruta_captura_disponible(captura_id)
    if not ruta:
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Advertencia: Faltan los tokens de Telegram en el archivo .env. No se envió la captura.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(ruta, "rb") as foto:
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID}, files={"photo": foto})
        print("[*] Captura enviada exitosamente a Telegram.")
    except Exception as e:
        print(f"[!] Error enviando captura a Telegram: {e}")


def buscar_en_web(consulta: str) -> str:
    """Busca información en internet usando DuckDuckGo HTML."""
    try:
        print(f"[*] Jarvis está buscando en la web: {consulta}")

        query = urllib.parse.quote(consulta)
        url = f"https://html.duckduckgo.com/html/?q={query}"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        respuesta = requests.get(
            url,
            headers=headers,
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


HEADERS_BUSQUEDA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def buscar_urls_duckduckgo(consulta: str, cantidad: int = 3) -> list:
    """Busca en DuckDuckGo HTML y devuelve las URLs REALES de los
    primeros resultados (a diferencia de buscar_en_web, que devuelve
    texto para que Jarvis lo lea, esto devuelve links para abrir)."""
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


def abrir_pestanas_navegador(urls: list):
    """Abre cada URL como pestaña nueva, en la ventana que ya está
    abierta (no una ventana por URL)."""
    nombre_tecnico = DICCIONARIO_APPS.get("navegador", "zen-browser")

    for url in urls:
        subprocess.Popen([
            "hyprctl", "dispatch", "exec",
            f'{nombre_tecnico} -new-tab "{url}"',
        ])
        # Un respiro entre pestañas para no saturar al navegador
        # abriendo todas de una en el mismo instante.
        time.sleep(0.3)


def abrir_pestanas_busqueda(consulta: str, tipo: str = "general") -> str:
    """Busca 'consulta' y ABRE varias pestañas con los resultados más
    relevantes — pensado para investigación (papers, imágenes,
    comparar varias fuentes), donde una respuesta hablada no alcanza
    y hace falta poder leer/mirar varias cosas.

    Distinto de buscar_en_web(), que devuelve texto para que Jarvis
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
}


CLASES_VENTANA = {
    "zen-browser": "zen",
}


def clase_ventana(nombre_tecnico: str) -> str:
    """Devuelve la clase real de Hyprland para un nombre técnico dado."""
    return CLASES_VENTANA.get(nombre_tecnico, nombre_tecnico)


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
    try:
        nombre_tecnico = DICCIONARIO_APPS.get(app_name.lower(), app_name.lower())

        if escritorio:
            clase = clase_ventana(nombre_tecnico)

            comando_magico = (
                f"hyprctl dispatch exec {nombre_tecnico} && "
                f"sleep 2 && "
                f"hyprctl dispatch movetoworkspacesilent "
                f"{escritorio},class:.*{clase}.*"
            )

            subprocess.Popen(comando_magico, shell=True)

            return (
                f"{app_name} se está abriendo "
                f"y será enviada al escritorio {escritorio}."
            )

        subprocess.Popen(
            ["hyprctl", "dispatch", "exec", nombre_tecnico]
        )

        return f"He dado la orden de abrir {app_name}."

    except Exception as e:
        return f"Error al intentar abrir {app_name}: {e}"


def mover_ventana(app_name: str, escritorio: int) -> str:
    """Mueve una ventana a un escritorio específico."""
    try:
        nombre_tecnico = DICCIONARIO_APPS.get(app_name.lower(), app_name.lower())
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
            "description": "Controla la reproducción multimedia.",
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
            "description": "Obtiene el clima actual de Villa Alegre.",
            "parameters": {
                "type": "object",
                "properties": {},
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


PALABRAS_PROTEGIDAS = set()

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

    vocabulario = list(DICCIONARIO_APPS.keys()) + list(DISPOSITIVOS_BLUETOOTH.keys())

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
    """Detecta 'Jarvis, analiza mi pantalla [y] <pregunta>'. Si no
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

    if re.search(r"\b(clima|tiempo)\b", t) and not any(
        palabra in t
        for palabra in [
            "noticia", "pronóstico", "pronostico",
            "mañana", "semana"
        ]
    ):
        comandos.append(("clima", {}))

    if re.search(
        r"\b(captura|screenshot|captura de pantalla|"
        r"toma una captura|haz una captura|saca una captura)\b",
        t
    ):
        comandos.append(("captura", {}))

    if re.search(
        r"\b(pausa|pausar|pausa la música|pausa la musica|"
        r"detén la música|deten la musica|para la música|para la musica)\b",
        t
    ):
        comandos.append(("multimedia", {"accion": "pausar"}))

    if re.search(
        r"\b(reproduce|reproducir|reanuda|reanudar|"
        r"continúa|continua|dale play)\b",
        t
    ):
        comandos.append(("multimedia", {"accion": "reproducir"}))

    if re.search(
        r"\b(siguiente|siguiente canción|siguiente cancion|next|"
        r"pasa la canción|pasa la cancion|otra canción|otra cancion)\b",
        t
    ):
        comandos.append(("multimedia", {"accion": "siguiente"}))

    if re.search(
        r"\b(anterior|canción anterior|cancion anterior|previous|"
        r"vuelve atrás|vuelve atras|retrocede)\b",
        t
    ):
        comandos.append(("multimedia", {"accion": "anterior"}))

    if (
        "qué canción" in t
        or "que cancion" in t
        or "qué está sonando" in t
        or "que esta sonando" in t
        or "qué música está sonando" in t
        or "que musica esta sonando" in t
    ):
        comandos.append(("info_cancion", {}))

    match_subir = re.search(
        r"\b(sub(?:e|ir)|s[uú]bele|aumenta|incrementa|"
        r"dale m[aá]s volumen|m[aá]s volumen|aumenta el audio)\b"
        r"(?:.{0,20}?(\d{1,3})\s*%?)?",
        t
    )

    if match_subir:
        porcentaje = int(match_subir.group(2) or 10)
        comandos.append(("volumen", {
            "accion": "subir",
            "porcentaje": porcentaje,
        }))

    match_bajar = re.search(
        r"\b(baja|bajar|b[aá]jale|disminuye|reduce|"
        r"dale menos volumen|menos volumen)\b"
        r"(?:.{0,20}?(\d{1,3})\s*%?)?",
        t
    )

    if match_bajar:
        porcentaje = int(match_bajar.group(2) or 10)
        comandos.append(("volumen", {
            "accion": "bajar",
            "porcentaje": porcentaje,
        }))

    if not match_subir and not match_bajar:
        match = re.search(
            r"(?:volumen|volume).{0,20}?\b(?:a|al|en)?\s*(\d{1,3})\s*%?",
            t
        )

        if match:
            porcentaje = max(0, min(100, int(match.group(1))))
            comandos.append(("volumen", {
                "accion": "setear",
                "porcentaje": porcentaje,
            }))

    if re.search(
        r"\b(mute|mutear|silencia|silenciar|desmutea|desmutear)\b",
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

    # Reiniciar el PROCESO de Jarvis (para aplicar cambios de código)
    # es distinto de reiniciar la PC — no necesita confirmación,
    # porque no interrumpe nada más que al propio Jarvis un momento.
    if re.search(r"\breinicia(?:te)?\s+(?:el\s+)?(?:servidor|jarvis)\b", t):
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
    "Por supuesto, señor.",
    "Como ordene, señor.",
    "Desde luego, señor.",
    "Como desee, señor.",
    "Enseguida, señor.",
    "Entendido, señor.",
    "Ahora mismo, señor.",
    "Con gusto, señor.",
    "Faltaba más, señor.",
    "Marchando, señor.",
    "A la orden, señor.",
    "Dalo por hecho, señor.",
    "Concedido, señor.",
    "Sin problema, señor.",
    "En ello estoy, señor.",
    "Hecho está, señor.",
    # Variantes SIN "señor" — para que no aparezca en el 100% de las
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


def nucleo_local(nombre: str, args: dict, resultado: str) -> str:
    """Devuelve solo el 'núcleo' de la respuesta."""
    if resultado.startswith(
        ("Error", "Pucha", "Falló", "No pude", "Hubo un drama")
    ):
        return resultado

    clave = (
        nombre,
        args.get("accion") if nombre in ("multimedia", "volumen", "bluetooth") else None,
    )
    variantes = NUCLEOS.get(clave)

    if not variantes:
        return resultado

    return random.choice(variantes).format(**args)


def respuesta_local(nombre: str, args: dict, resultado: str) -> str:
    """Genera una respuesta carismática sin gastar tokens de Groq."""
    if resultado.startswith(("Error", "Pucha", "Falló", "No pude", "Hubo un drama")):
        return resultado

    if nombre in ("confirmar_accion", "confirmacion_cancelada"):
        return resultado

    if nombre == "clima":
        return f"{random.choice(MULETILLAS)} {resultado}"

    if nombre == "info_cancion":
        return f"{random.choice(MULETILLAS)} {resultado}"

    if nombre == "estado_sistema":
        return f"Señor, estos son los parámetros actuales. {resultado}"

    if nombre in ("captura", "multimedia", "volumen", "bluetooth"):
        nucleo = nucleo_local(nombre, args, resultado)
        return f"{random.choice(MULETILLAS)} {nucleo}."

    if nombre in (
        "abrir_aplicacion", "cerrar_aplicacion", "mover_ventana",
        "cerrar_todo", "energia", "conectar_bluetooth", "desconectar_bluetooth",
    ):
        return f"{random.choice(MULETILLAS)} {resultado}"

    return f"{random.choice(MULETILLAS)} {resultado}"


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
        memoria_contexto["ultimo_tema"] = nombre

    return resultados


def resumir_resultados_locales(resultados):
    """Construye una sola respuesta para varias órdenes."""
    frases = []

    for nombre, args, resultado in resultados:
        if nombre == "clima":
            frases.append(resultado)
            continue

        # Estas dos son frases completas por sí mismas (una pregunta o
        # un aviso de cancelación) — envolverlas con una muletilla
        # tipo "Por supuesto, señor. ¿Desea...?" suena raro.
        if nombre in ("confirmar_accion", "confirmacion_cancelada"):
            return resultado

        frases.append(nucleo_local(nombre, args, resultado))

    if not frases:
        return "Entendido, señor."

    muletilla = random.choice(MULETILLAS)

    if len(frases) == 1:
        return f"{muletilla} {frases[0]}."

    return f"{muletilla} " + "; ".join(frases) + "."


def tools_necesarias(texto: str):
    """Envía a Groq solo los schemas que podrían ser necesarios."""
    t = texto.lower()
    nombres = set()

    if any(x in t for x in (
        "busca en internet", "busca en la web", "búscalo en internet",
        "buscalo en internet", "búscalo en la web", "buscalo en la web",
        "investiga", "consulta internet", "en internet", "en la web",
        "noticias de", "noticias sobre", "últimas noticias",
        "ultimas noticias", "información reciente", "informacion reciente",
    )):
        nombres.add("buscar_en_web")

    if any(x in t for x in (
        "volumen", "sube el volumen", "baja el volumen",
        "silencia", "silenciar", "mute",
    )):
        nombres.add("ajustar_volumen")

    if any(x in t for x in (
        "pausa", "pausar", "reproduce", "reproducir", "reanuda",
        "reanudar", "siguiente canción", "siguiente cancion",
        "canción anterior", "cancion anterior",
    )):
        nombres.add("control_multimedia")

    if any(x in t for x in (
        "qué canción", "que cancion", "qué está sonando",
        "que esta sonando", "qué música", "que musica",
    )):
        nombres.add("info_cancion")

    if any(x in t for x in (
        "clima", "tiempo actual", "temperatura",
        "lluvia", "llueve", "lloverá", "llovera", "pronóstico", "pronostico",
    )):
        nombres.add("reporte_clima")

    if any(x in t for x in (
        "estado del sistema", "estado del pc", "uso de ram",
        "uso de cpu", "cuánta ram", "cuanta ram",
    )):
        nombres.add("estado_sistema")

    if any(x in t for x in (
        "captura", "screenshot", "captura de pantalla",
    )):
        nombres.add("tomar_captura")

    if any(x in t for x in (
        "abre ", "abrir ", "ejecuta ", "ejecutar ",
        "lanza ", "lanzar ",
    )):
        nombres.add("abrir_aplicacion")

    if any(x in t for x in (
        "cierra ", "cerrar ", "mata ", "termina ",
    )):
        nombres.add("cerrar_aplicacion")

    if any(x in t for x in (
        "mueve ", "mover ", "escritorio", "workspace",
    )):
        nombres.add("mover_ventana")

    if any(x in t for x in (
        "apaga", "apagar", "reinicia", "reiniciar",
    )):
        nombres.add("control_energia")

    if any(x in t for x in (
        "cierra todo", "cerrar todo", "modo pánico", "modo panico",
    )):
        nombres.add("cerrar_todo")

    if any(x in t for x in (
        "bluetooth", "conecta", "conéctate", "conectate",
        "desconecta", "desconéctate", "desconectate", "vincula",
    )):
        nombres.add("controlar_bluetooth")
        nombres.add("conectar_bluetooth")
        nombres.add("desconectar_bluetooth")

    if any(x in t for x in (
        "actualiza", "actualizar", "actualización", "actualizacion",
        "paquetes",
    )):
        nombres.add("actualizar_sistema")
        nombres.add("verificar_actualizaciones")

    if any(x in t for x in (
        "música", "musica", "youtube music", "yt music",
    )):
        nombres.add("abrir_radio_musica")

    if any(x in t for x in (
        "papers", "paper", "artículos", "articulos", "fuentes",
        "imágenes", "imagenes", "pestañas", "pestanas",
        "investigar", "investigación", "investigacion",
    )):
        nombres.add("abrir_pestanas_busqueda")
        nombres.add("buscar_en_web")

    if any(x in t for x in ("pantalla", "captura")):
        nombres.add("analizar_pantalla")

    if not nombres:
        # Sin esto, si ninguna palabra clave matchea, la solicitud se
        # manda a Groq sin NINGUNA herramienta. openai/gpt-oss-120b,
        # al no tener herramientas pero sentir que la pregunta
        # necesita info actual (como "¿a qué hora lloverá?"), a veces
        # ALUCINA un tool interno de OpenAI (web.run) que no existe
        # en esta API — Groq lo rechaza con 400 (bug conocido de los
        # modelos gpt-oss cuando se quedan sin herramientas). Dándole
        # siempre al menos buscar_en_web como opción real, tiene algo
        # concreto para usar en vez de inventar algo que no existe.
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
        return f"¿Confirma que quiere {verbo} el sistema, señor? Diga 'sí' para proceder."

    if nombre == "actualizar_sistema":
        return actualizar_sistema()

    if nombre == "verificar_actualizaciones":
        return verificar_actualizaciones()

    if nombre == "confirmacion_cancelada":
        return "Cancelado, señor. No se hizo ningún cambio."

    if nombre == "clima":
        return reporte_clima()

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
        return reiniciar_servidor_jarvis()

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
    return {"status": "Jarvis API en línea"}


FRASES_BIENVENIDA = [
    "Bienvenido a casa, señor.",
    "Bienvenido de vuelta, señor.",
    "Sistemas en línea. Bienvenido a casa, señor.",
    "Todo listo, señor. Bienvenido a casa.",
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

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    request: ChatRequest,
    origen: str = "pc"
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

    if comandos:
        print(f"[*] Router local: {comandos}")

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
            enviar_captura_telegram(captura_id_local)

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
    fecha_hora_actual = datetime.now().strftime("%A %d de %B de %Y, a las %I:%M %p")
    system_prompt = f"""Eres J.A.R.V.I.S., asistente de escritorio del usuario.

Responde en español, con personalidad elegante y carismática, ligeramente británica.
Sé calmado, ingenioso y profesional. Puedes usar "señor" o "señor Keric"
de forma natural, sin repetirlo en cada frase.
Habla de forma natural, no robótica. Sé breve: como máximo 3-4 frases,
salvo que el usuario te pida explícitamente más detalle. Preferí una
explicación corta y clara antes que una completa y larga — tenés un
límite de tokens, y una respuesta cortada a mitad es peor que una
corta pero completa.
Usa herramientas solo cuando el usuario solicite una acción.
No ejecutes herramientas ante agradecimientos o conversación casual.
Nunca inventes resultados de herramientas.

IMPORTANTE: tu respuesta se convierte a voz y se lee en alto, nunca se
lee en pantalla. Por eso, NUNCA uses markdown: nada de tablas (|),
negritas (**), encabezados (#), ni listas con guiones o viñetas.
Respondé siempre en prosa corrida, como hablarías en una conversación
real. Si la información tiene varias partes, decilas como una frase
fluida ("hay tres tipos: A, que hace tal cosa; B, que hace tal otra...")
en vez de una lista o tabla.

Fecha y hora actual: {fecha_hora_actual}"""

    historial_chat.append({"role": "user", "content": mensaje_usuario})

    if len(historial_chat) > MAX_HISTORIAL:
        historial_chat = historial_chat[-MAX_HISTORIAL:]

    mensajes = [{"role": "system", "content": system_prompt}]

    contexto = contexto_para_pregunta(mensaje_usuario)
    if contexto:
        mensajes.append({"role": "system", "content": contexto})

    mensajes.extend(historial_chat)
    tools_mensaje = tools_necesarias(mensaje_usuario)

    parametros = {
        "model": MODEL,
        "messages": mensajes,
        "temperature": 0.2,
        # openai/gpt-oss-120b (y los modelos de razonamiento en
        # general) esperan max_completion_tokens, no max_tokens — el
        # nombre viejo del parámetro. Con el modelo anterior daba lo
        # mismo, con este probablemente esté siendo la causa del 500.
        # 220 se quedaba corto seguido para preguntas que necesitan
        # una explicación de verdad (se cortaba a mitad de frase).
        # 350 da más margen sin ser un cheque en blanco — la
        # instrucción de brevedad en el system_prompt sigue haciendo
        # que la mayoría de las respuestas cortas no lo usen igual.
        "max_completion_tokens": 350,
        "reasoning_effort": "low",
    }

    if tools_mensaje:
        parametros["tools"] = tools_mensaje
        parametros["tool_choice"] = "auto"

    try:
        response = client.chat.completions.create(**parametros)
        response_message = response.choices[0].message
    except Exception as e:
        error_texto = str(e)

        # Defensa extra por si el bug de gpt-oss (alucinar un tool
        # que no existe) se repite en algún otro caso: en vez de
        # tirar 500 directo, reintentamos UNA vez sin herramientas —
        # sin nada que "confundir" al modelo, suele responder normal.
        if "tool_use_failed" in error_texto or "Tool choice is none" in error_texto:
            print(f"[!] Groq alucinó un tool inexistente, reintentando sin tools: {e}")
            parametros.pop("tools", None)
            parametros.pop("tool_choice", None)

            try:
                response = client.chat.completions.create(**parametros)
                response_message = response.choices[0].message
            except Exception as e2:
                print(f"[ERROR GROQ] {e2}")
                raise HTTPException(status_code=500, detail=f"Error al contactar con la IA: {e2}")
        else:
            print(f"[ERROR GROQ] {e}")
            raise HTTPException(status_code=500, detail=f"Error al contactar con la IA: {e}")

    # Si el modelo se quedó sin tokens antes de terminar la idea,
    # esto queda en "length" en vez de "stop" — lo usamos más abajo
    # para recortar prolijo en vez de leer algo cortado a mitad.
    finish_reason = response.choices[0].finish_reason

    # 5. TOOL CALL
    if response_message.tool_calls:
        resultados_tool = []

        for tool_call in response_message.tool_calls:
            fn_name = tool_call.function.name
            fn_args = json.loads(tool_call.function.arguments or "{}")

            print(f"[*] Tool call: {fn_name} {fn_args}")

            if fn_name == "ajustar_volumen":
                resultado = ajustar_volumen(**fn_args)
            elif fn_name == "abrir_aplicacion":
                resultado = abrir_aplicacion(**fn_args)
            elif fn_name == "cerrar_aplicacion":
                resultado = cerrar_aplicacion(**fn_args)
            elif fn_name == "control_multimedia":
                resultado = control_multimedia(**fn_args)
            elif fn_name == "info_cancion":
                resultado = info_cancion()
                if resultado.startswith("Actualmente está sonando:"):
                    memoria_contexto["cancion"] = resultado.replace(
                        "Actualmente está sonando:", ""
                    ).strip()
            elif fn_name == "control_energia":
                resultado = control_energia(**fn_args)
            elif fn_name == "cerrar_todo":
                resultado = cerrar_todo()
            elif fn_name == "reporte_clima":
                resultado = reporte_clima()
            elif fn_name == "estado_sistema":
                resultado = estado_sistema()
            elif fn_name == "tomar_captura":
                resultado = tomar_captura()
                if resultado.startswith("CAPTURA_LISTA"):
                    captura_id_groq = resultado.split(":", 1)[1]
                    if ruta_captura_disponible(captura_id_groq):
                        image_path_response = f"/captura/{captura_id_groq}"
                        enviar_captura_telegram(captura_id_groq)
            elif fn_name == "buscar_en_web":
                resultado = buscar_en_web(**fn_args)
            elif fn_name == "mover_ventana":
                resultado = mover_ventana(**fn_args)
            elif fn_name == "controlar_bluetooth":
                resultado = controlar_bluetooth(**fn_args)
            elif fn_name == "conectar_bluetooth":
                resultado = conectar_bluetooth(**fn_args)
            elif fn_name == "desconectar_bluetooth":
                resultado = desconectar_bluetooth(**fn_args)
            elif fn_name == "actualizar_sistema":
                resultado = actualizar_sistema()
            elif fn_name == "verificar_actualizaciones":
                resultado = verificar_actualizaciones()
            elif fn_name == "abrir_radio_musica":
                resultado = abrir_radio_musica()
            elif fn_name == "abrir_pestanas_busqueda":
                resultado = abrir_pestanas_busqueda(**fn_args)
            elif fn_name == "analizar_pantalla":
                resultado = analizar_pantalla(**fn_args)
            else:
                resultado = "Error: Herramienta no encontrada."

            resultados_tool.append((fn_name, fn_args, resultado))
            memoria_contexto["ultimo_resultado"] = str(resultado)
            memoria_contexto["ultimo_tema"] = fn_name

        texto_final = " ".join(
            respuesta_local(nombre, args, resultado)
            for nombre, args, resultado in resultados_tool
            if not resultado.startswith("CAPTURA_LISTA")
        )
        texto_final = limpiar_texto_para_voz(texto_final)

        if not texto_final:
            texto_final = "Por supuesto, señor. Hecho."

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
            response_message.content or "No tengo nada que añadir, señor."
        )

        if finish_reason == "length":
            texto_final = recortar_a_oracion_completa(texto_final)

    # 6. MEMORIA
    historial_chat.append({"role": "assistant", "content": texto_final})
    if len(historial_chat) > MAX_HISTORIAL:
        historial_chat = historial_chat[-MAX_HISTORIAL:]

    print(f"[Jarvis] {texto_final}")

    audio_path_response = await procesar_salida_de_voz(
        texto_final,
        origen
    )

    return ChatResponse(
        response=texto_final,
        image_path=image_path_response,
        audio_path=audio_path_response
    )

# Proceso de mpv reproduciendo la voz de Jarvis en el PC ahora mismo,
# o None si no está hablando. Se usa para poder interrumpirlo cuando
# arranca una grabación nueva (ver interrumpir_voz_pc()).
PROCESO_VOZ_PC = None


def interrumpir_voz_pc():
    """Corta lo que Jarvis esté diciendo en el PC ahora mismo, si hay
    algo sonando. Se llama apenas arranca una grabación nueva, para
    que la voz de Jarvis no se solape con lo que estés por decir."""
    global PROCESO_VOZ_PC

    if PROCESO_VOZ_PC is not None and PROCESO_VOZ_PC.poll() is None:
        try:
            PROCESO_VOZ_PC.terminate()
        except Exception as e:
            print(f"[!] No se pudo interrumpir la voz: {e}")

    PROCESO_VOZ_PC = None


async def hablar_por_parlantes(texto: str):
    """
    Genera la voz de Jarvis y la reproduce SOLO en los parlantes del PC.
    """
    global PROCESO_VOZ_PC

    if not texto:
        return

    ruta_respuesta = "/tmp/respuesta_jarvis_pc.ogg"
    proceso = None

    try:
        voz_jarvis = edge_tts.Communicate(
            texto,
            voice="es-DO-EmilioNeural",
            rate="+10%"
        )

        await voz_jarvis.save(ruta_respuesta)

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


async def generar_audio_para_telefono(texto: str) -> str | None:
    """
    Genera la voz de Jarvis para el teléfono.

    NO reproduce el audio en el PC.
    Simplemente genera el archivo y devuelve su ruta HTTP.
    """

    if not texto:
        return None

    nombre_archivo = f"respuesta_jarvis_{uuid.uuid4().hex[:12]}.ogg"

    ruta_archivo = os.path.join(
        CARPETA_AUDIOS,
        nombre_archivo
    )

    try:
        voz_jarvis = edge_tts.Communicate(
            texto,
            voice="es-DO-EmilioNeural",
            rate="+10%"
        )

        await voz_jarvis.save(ruta_archivo)

        print(
            f"[*] Audio para teléfono generado: "
            f"{ruta_archivo}"
        )

        # /tmp está montado como /audios
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
    Decide dónde debe salir la voz de Jarvis.

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
    y lo envía al cerebro principal de Jarvis.

    origen=pc:
        Jarvis habla por los parlantes del PC.

    origen=telefono:
        Jarvis genera el audio y lo devuelve
        para que Flutter lo reproduzca.
    """

    origen = origen.lower().strip()

    if origen not in ("pc", "telefono"):
        raise HTTPException(
            status_code=400,
            detail="El origen debe ser 'pc' o 'telefono'."
        )

    # Apenas arranca una grabación nueva, cortamos lo que Jarvis
    # estuviera diciendo en el PC — así no se solapa la respuesta
    # vieja con la nueva. Solo tiene sentido para origen=pc: el
    # teléfono reproduce su propio audio del lado de Flutter, Jarvis
    # no tiene control sobre eso desde acá.
    if origen == "pc":
        interrumpir_voz_pc()

    ruta_audio = f"/tmp/temp_{audio.filename}"

    try:

        # ----------------------------------------------------
        # 1. GUARDAR AUDIO
        # ----------------------------------------------------

        with open(ruta_audio, "wb") as buffer:
            buffer.write(
                await audio.read()
            )

        pista_vocabulario = (
            "Uso a Jarvis para controlar mi computadora: "
            "abro Steam, Discord, Zen Browser, "
            "Visual Studio Code, Heroic y Prism Launcher; "
            "conecto el Bluetooth a mi Alexa; "
            "controlo el volumen, tomo capturas "
            "y muevo ventanas entre escritorios."
        )

        # ----------------------------------------------------
        # 2. WHISPER
        # ----------------------------------------------------

        with open(ruta_audio, "rb") as f:

            transcripcion = (
                client.audio.transcriptions.create(
                    file=(
                        ruta_audio,
                        f.read()
                    ),
                    model="whisper-large-v3",
                    language="es",
                    prompt=pista_vocabulario,
                )
            )

        mensaje_usuario = (
            transcripcion.text.strip()
        )

    except Exception as e:

        print(
            f"[ERROR WHISPER] {e}"
        )

        raise HTTPException(
            status_code=500,
            detail=f"Error procesando el audio: {e}"
        )

    finally:

        if os.path.exists(ruta_audio):

            try:
                os.remove(ruta_audio)

            except Exception:
                pass

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

    resultado = await chat_endpoint(
        request_data,
        origen=origen
    )

    # IMPORTANTE:
    #
    # Ya NO hacemos:
    #
    # await hablar_por_parlantes(...)
    #
    # porque chat_endpoint() ya decidió dónde
    # debe salir la voz.
    #

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

    resultado = await chat_endpoint(
        request_data,
        origen=origen
    )

    return resultado

@app.get("/sensores")
async def leer_sensores():
    """Devuelve el estado de hardware en tiempo real para el Dashboard móvil."""
    try:
        # RAM en Megabytes
        ram_usada = subprocess.check_output("free -m | awk '/^Mem:/ {print $3}'", shell=True).decode().strip()
        ram_total = subprocess.check_output("free -m | awk '/^Mem:/ {print $2}'", shell=True).decode().strip()
        
        # Uso de CPU
        cpu_uso = subprocess.check_output("top -bn1 | grep 'Cpu(s)' | awk '{print $2 + $4}'", shell=True).decode().strip()
        
        # Temperatura de CPU (En Arch/CachyOS suele estar aquí, lo dividimos por 1000)
        try:
            cpu_temp_raw = subprocess.check_output("cat /sys/class/thermal/thermal_zone0/temp", shell=True).decode().strip()
            cpu_temp = str(int(cpu_temp_raw) // 1000)
        except Exception:
            cpu_temp = "N/A"

        # Uso y Temp de NVIDIA
        try:
            gpu_info = subprocess.check_output(
                "nvidia-smi --query-gpu=utilization.gpu,temperature.gpu --format=csv,noheader,nounits", 
                shell=True
            ).decode().strip()
            gpu_uso, gpu_temp = gpu_info.split(', ')
        except Exception:
            gpu_uso, gpu_temp = "0", "N/A" # Por si está apagada por el sistema híbrido

        return {
            "ram_usada": ram_usada,
            "ram_total": ram_total,
            "cpu_uso": cpu_uso,
            "cpu_temp": cpu_temp,
            "gpu_uso": gpu_uso,
            "gpu_temp": gpu_temp
        }
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    print("Iniciando Jarvis FastAPI en http://0.0.0.0:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)