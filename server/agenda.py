"""Agenda de Keric en Google Calendar.

Con la API de Google (OAuth, ver scripts/autorizar_google.py) lee y crea
eventos. Mientras no esté autorizada, lee con el mismo script que usa el
calendario de quickshell (URL iCal secreta, solo lectura).

La IA entrega la hora tal como se dijo ("a las 4" -> "4:00"); si es de
mañana o de tarde se decide aquí, igual que en Lucy.
"""
import json
import os
import re
import subprocess
import unicodedata
from datetime import date, datetime, timedelta

CARPETA_GOOGLE = os.getenv(
    "MADELINE_GOOGLE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "google"),
)
RUTA_CREDENCIALES = os.path.join(CARPETA_GOOGLE, "credentials.json")
RUTA_TOKEN = os.path.join(CARPETA_GOOGLE, "token.json")
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
CALENDARIO = os.getenv("MADELINE_CALENDARIO", "primary")
API_EVENTOS = f"https://www.googleapis.com/calendar/v3/calendars/{CALENDARIO}/events"

SCRIPT_ICAL = os.path.expanduser("~/.config/quickshell/CalendarApp/gcal-events.py")

DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


class SinAgenda(Exception):
    """No hay forma de leer o escribir el calendario (sin token, sin red)."""


class PermisoVencido(SinAgenda):
    """Google revocó el permiso: con la app en modo Prueba vence cada 7 días."""


# ------------------------------------------------------------
# Google Calendar
# ------------------------------------------------------------

def autorizado() -> bool:
    return os.path.exists(RUTA_TOKEN)


def reautorizar():
    """Abre el navegador para volver a dar el permiso (scripts/autorizar_google.py)."""
    carpeta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    subprocess.Popen(
        [os.path.join(carpeta, ".venv", "bin", "python"),
         os.path.join(carpeta, "scripts", "autorizar_google.py")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


def _sesion():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials

    credenciales = Credentials.from_authorized_user_file(RUTA_TOKEN, SCOPES)
    if not credenciales.valid:
        try:
            credenciales.refresh(Request())
        except RefreshError as e:
            raise PermisoVencido(str(e)) from e
        with open(RUTA_TOKEN, "w", encoding="utf-8") as f:
            f.write(credenciales.to_json())
    return AuthorizedSession(credenciales)


def _a_rfc3339(dia: date) -> str:
    return datetime.combine(dia, datetime.min.time()).astimezone().isoformat()


def _eventos_google(desde: date, hasta: date) -> list:
    respuesta = _sesion().get(API_EVENTOS, params={
        "timeMin": _a_rfc3339(desde),
        "timeMax": _a_rfc3339(hasta),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 50,
    }, timeout=10)
    respuesta.raise_for_status()
    eventos = []
    for item in respuesta.json().get("items", []):
        inicio = item.get("start", {})
        if "dateTime" in inicio:
            momento = datetime.fromisoformat(inicio["dateTime"]).astimezone().replace(tzinfo=None)
            dia, hora = momento.date(), momento.strftime("%H:%M")
        else:
            dia, hora = date.fromisoformat(inicio["date"]), ""
        eventos.append({"fecha": dia, "hora": hora, "titulo": item.get("summary", "(sin título)")})
    return eventos


def _eventos_ical(desde: date, hasta: date) -> list:
    """El script de quickshell usa el python del sistema (tiene icalendar)."""
    salida = subprocess.run(
        ["python3", SCRIPT_ICAL, desde.isoformat(), hasta.isoformat()],
        capture_output=True, text=True, timeout=20,
    )
    datos = json.loads(salida.stdout or "{}")
    if datos.get("error") and not datos.get("events"):
        raise SinAgenda(datos["error"])
    return [
        {"fecha": date.fromisoformat(e["date"]), "hora": e["start"], "titulo": e["title"]}
        for e in datos.get("events", [])
    ]


def eventos_entre(desde: date, hasta: date) -> list:
    """Eventos de [desde, hasta), ordenados; los de todo el día primero."""
    try:
        if autorizado():
            try:
                eventos = _eventos_google(desde, hasta)
            except PermisoVencido:
                # Para leer basta la URL iCal; el aviso llega al crear un evento.
                if not os.path.exists(SCRIPT_ICAL):
                    raise
                eventos = _eventos_ical(desde, hasta)
        elif os.path.exists(SCRIPT_ICAL):
            eventos = _eventos_ical(desde, hasta)
        else:
            raise SinAgenda("no hay calendario configurado")
    except SinAgenda:
        raise
    except Exception as e:
        raise SinAgenda(str(e)) from e
    return sorted(eventos, key=lambda e: (e["fecha"], e["hora"] != "", e["hora"]))


def crear(titulo: str, inicio: datetime | date, duracion_min: int = 60):
    if not autorizado():
        raise SinAgenda("falta autorizar Google (scripts/autorizar_google.py)")
    if isinstance(inicio, datetime):
        fin = inicio + timedelta(minutes=duracion_min)
        cuerpo = {
            "start": {"dateTime": inicio.astimezone().isoformat()},
            "end": {"dateTime": fin.astimezone().isoformat()},
        }
    else:
        cuerpo = {
            "start": {"date": inicio.isoformat()},
            "end": {"date": (inicio + timedelta(days=1)).isoformat()},
        }
    cuerpo["summary"] = titulo
    try:
        respuesta = _sesion().post(API_EVENTOS, json=cuerpo, timeout=10)
        respuesta.raise_for_status()
    except SinAgenda:
        raise
    except Exception as e:
        raise SinAgenda(str(e)) from e


# ------------------------------------------------------------
# Fechas y horas dichas
# ------------------------------------------------------------

def _normalizar(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(c) != "Mn"
    )


_PARTE_DEL_DIA = re.compile(r"\b(?:(?:de|en|por) la|esta) (manana|tarde|noche)\b|\b(madrugada)\b")
_DIA = re.compile(
    r"\b(?:hoy|(?<!de la )(?<!en la )(?<!por la )manana|lunes|martes|miercoles|jueves|viernes|"
    r"sabado|domingo|semana|mes|ano|el \d{1,2}\b|\d{1,2} de (?:"
    + "|".join(MESES) + r")|navidad|cumple)"
)


def menciona_dia(texto: str) -> bool:
    """Si dijeron qué día. Si no, es hoy, aunque la IA diga otra cosa."""
    return bool(_DIA.search(_normalizar(texto)))


def parte_del_dia(texto: str) -> str | None:
    """"manana", "tarde" o "noche" si lo dijeron junto a la hora."""
    match = _PARTE_DEL_DIA.search(_normalizar(texto))
    if not match:
        return None
    return "manana" if match.group(2) else match.group(1)


def interpretar_hora_dicha(dia: date, hora: str, parte: str | None, ahora: datetime) -> datetime | None:
    """Hora tal como la dijeron -> momento.

    - Con parte del día (o en 24 horas, como "16:00"), se usa esa.
    - De 1 a 6 sin aclarar es de la tarde ("a las 4" casi nunca es de madrugada).
    - Si no, la próxima vez que llegue esa hora: "a las 8" dicho a las 10 de
      la mañana es a las 20.
    """
    match = re.match(r"^(\d{1,2})(?::(\d{2}))?", hora.strip())
    if not match:
        return None
    h, m = int(match.group(1)), int(match.group(2) or 0)
    if h > 23 or m > 59:
        return None
    if h == 0 or h >= 13:
        candidatas = [h]
    elif parte == "tarde":
        candidatas = [h if h == 12 else h + 12]
    elif parte == "noche":
        candidatas = [24 if h == 12 else h + 24 if h <= 4 else h + 12]
    elif parte == "manana":
        candidatas = [h]
    elif h <= 6:
        candidatas = [h + 12, h + 36]
    elif h == 12:
        candidatas = [12, 36]
    else:
        candidatas = [h, h + 12, h + 24]
    momentos = [
        datetime.combine(dia + timedelta(days=horas // 24), datetime.min.time())
        .replace(hour=horas % 24, minute=m)
        for horas in candidatas
    ]
    return next((momento for momento in momentos if momento > ahora), momentos[0])


def a_la_hora(momento: datetime) -> str:
    """"a la 1 de la tarde", "a las 8:30 de la mañana"; para leer en voz alta."""
    h, m = momento.hour, momento.minute
    h12 = 12 if h % 12 == 0 else h % 12
    if h == 0 or h >= 20:
        parte = "de la noche"
    elif h <= 5:
        parte = "de la madrugada"
    elif h <= 11:
        parte = "de la mañana"
    elif h == 12:
        parte = "del día"
    else:
        parte = "de la tarde"
    numero = str(h12) if m == 0 else f"{h12}:{m:02d}"
    return f"{'a la' if h12 == 1 else 'a las'} {numero} {parte}"


def dia_relativo(dia: date, hoy: date) -> str:
    """"hoy", "mañana" o "el viernes 24 de octubre"."""
    diferencia = (dia - hoy).days
    if diferencia == 0:
        return "hoy"
    if diferencia == 1:
        return "mañana"
    return f"el {DIAS_SEMANA[dia.weekday()]} {dia.day} de {MESES[dia.month - 1]}"
