"""Servicio de comunicación IPC con Hyprland.

Permite enviar comandos y consultar estado directamente a través del
socket UNIX nativo de Hyprland ($XDG_RUNTIME_DIR/hypr/$HIS/.socket.sock),
reduciendo la latencia de ~25 ms (vía subprocess hyprctl) a <0.5 ms.

Si el socket no está disponible (tests, entorno headless o compositor
apagado), recurre de forma transparente a hyprctl via subprocess.
"""

import json
import os
import re
import socket
import subprocess
from typing import Any, List, Optional, Tuple


def _detectar_hyprland_lua() -> bool:
    for cmd in (["Hyprland", "--version"], ["hyprctl", "version"]):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            match = re.search(r"Hyprland\s+(\d+)\.(\d+)", res.stdout)
            if match:
                major, minor = int(match.group(1)), int(match.group(2))
                return (major > 0 or minor >= 57)
        except Exception:
            pass
    return True


_USA_HYPRLAND_LUA: bool = _detectar_hyprland_lua()


def usa_hyprland_lua() -> bool:
    """True si la versión de Hyprland es >= 0.57 (requiere Lua en dispatch)."""
    return _USA_HYPRLAND_LUA


def obtener_ruta_socket() -> Optional[str]:
    """Busca la ruta del socket UNIX de control (.socket.sock) de Hyprland."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        try:
            runtime_dir = f"/run/user/{os.getuid()}"
        except AttributeError:
            return None

    his = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    hypr_base = os.path.join(runtime_dir, "hypr")

    if his:
        candidato = os.path.join(hypr_base, his, ".socket.sock")
        if os.path.exists(candidato):
            return candidato

    # Si no está en el entorno, buscar instancias activas en la carpeta
    if os.path.isdir(hypr_base):
        try:
            for entrada in os.listdir(hypr_base):
                candidato = os.path.join(hypr_base, entrada, ".socket.sock")
                if os.path.exists(candidato):
                    return candidato
        except OSError:
            pass

    return None


def enviar_comando_socket(payload: str, timeout: float = 1.0) -> Optional[str]:
    """Envía un comando al socket UNIX nativo y devuelve la respuesta en texto."""
    ruta = obtener_ruta_socket()
    if not ruta:
        return None

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(ruta)
            s.sendall(payload.encode("utf-8"))
            buffer = bytearray()
            while True:
                data = s.recv(4096)
                if not data:
                    break
                buffer.extend(data)
            return buffer.decode("utf-8", errors="replace")
    except Exception:
        return None


# ------------------------------------------------------------
# Formato de comandos según versión (Lua 0.57+ o Legacy)
# ------------------------------------------------------------

def payload_dispatch_exec(comando: str) -> str:
    if usa_hyprland_lua():
        return f"dispatch hl.dsp.exec_cmd({json.dumps(comando)})"
    return f"dispatch exec {comando}"


def payload_dispatch_workspace(escritorio: int | str) -> str:
    if usa_hyprland_lua():
        return f"dispatch hl.dsp.focus({{ workspace = '{escritorio}' }})"
    return f"dispatch workspace {escritorio}"


def payload_dispatch_move(clase: str, escritorio: int | str) -> str:
    if usa_hyprland_lua():
        selector = f"class:.*{clase}.*"
        return f"dispatch hl.dsp.window.move({{ window = '{selector}', workspace = {escritorio}, follow = false }})"
    return f"dispatch movetoworkspacesilent {escritorio},class:.*{clase}.*"


def comando_hypr_exec(comando: str) -> List[str]:
    if usa_hyprland_lua():
        return ["hyprctl", "dispatch", f"hl.dsp.exec_cmd({json.dumps(comando)})"]
    return ["hyprctl", "dispatch", "exec", comando]


def comando_hypr_workspace(escritorio: int | str) -> List[str]:
    if usa_hyprland_lua():
        return ["hyprctl", "dispatch", f"hl.dsp.focus({{ workspace = '{escritorio}' }})"]
    return ["hyprctl", "dispatch", "workspace", str(escritorio)]


def comando_hypr_movetoworkspacesilent(clase: str, escritorio: int | str) -> List[str]:
    if usa_hyprland_lua():
        selector = f"class:.*{clase}.*"
        return ["hyprctl", "dispatch", f"hl.dsp.window.move({{ window = '{selector}', workspace = {escritorio}, follow = false }})"]
    return ["hyprctl", "dispatch", "movetoworkspacesilent", f"{escritorio},class:.*{clase}.*"]


# ------------------------------------------------------------
# Operaciones principales (Socket con fallback a hyprctl)
# ------------------------------------------------------------

def ejecutar_dispatch(payload: str, fallback_cmd: List[str]) -> bool:
    """Intenta enviar por socket; si no, ejecuta fallback por hyprctl."""
    resp = enviar_comando_socket(payload)
    if resp is not None and "ok" in resp.lower():
        return True

    # Fallback transparente
    try:
        res = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=2)
        return res.returncode == 0
    except Exception:
        return False


def lanzar_app(comando: str) -> None:
    """Ejecuta una aplicación en Hyprland."""
    payload = payload_dispatch_exec(comando)
    resp = enviar_comando_socket(payload)
    if resp is not None:
        return

    # Fallback con Popen para no bloquear
    subprocess.Popen(comando_hypr_exec(comando))


def cambiar_escritorio(escritorio: int | str) -> str:
    """Cambia el foco al escritorio solicitado."""
    payload = payload_dispatch_workspace(escritorio)
    fallback = comando_hypr_workspace(escritorio)
    ejecutar_dispatch(payload, fallback)
    return f"Cambiado al escritorio {escritorio}."


def mover_ventana(clase: str, escritorio: int | str) -> bool:
    """Mueve silenciosamente una ventana al escritorio especificado."""
    payload = payload_dispatch_move(clase, escritorio)
    fallback = comando_hypr_movetoworkspacesilent(clase, escritorio)
    return ejecutar_dispatch(payload, fallback)


def leer_escritorios() -> Tuple[List[int], Optional[int]]:
    """Consulta la lista de escritorios activos y el actual (usado por el Deck)."""
    # 1. Intentar por socket nativo con prefijo j/
    resp_lista = enviar_comando_socket("j/workspaces")
    resp_activo = enviar_comando_socket("j/activeworkspace")

    if resp_lista and resp_activo:
        try:
            workspaces = json.loads(resp_lista)
            active = json.loads(resp_activo)
            escritorios = sorted(w["id"] for w in workspaces if w["id"] > 0)
            return escritorios, active.get("id")
        except Exception:
            pass

    # 2. Fallback a hyprctl
    escritorios, activo = [], None
    try:
        lista = subprocess.run(["hyprctl", "workspaces", "-j"], capture_output=True, text=True, timeout=3)
        escritorios = sorted(w["id"] for w in json.loads(lista.stdout) if w["id"] > 0)
        act = subprocess.run(["hyprctl", "activeworkspace", "-j"], capture_output=True, text=True, timeout=3)
        activo = json.loads(act.stdout)["id"]
    except Exception as e:
        print(f"[!] No pude leer los escritorios de Hyprland: {e}")

    return escritorios, activo
