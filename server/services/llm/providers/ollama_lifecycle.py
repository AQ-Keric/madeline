"""Gestión aislada del ciclo de vida de Ollama mediante systemd --user y cgroup v2.

Provee:
- Verificación de titularidad de sockets en /proc/net/tcp y /proc/net/tcp6 con
  comprobación de network namespace y clasificación loopback vs. comodín.
- Seguimiento independiente de runners mediante linaje (ppid), pertenencia al
  cgroup dedicado, pertenencia al PGID e identidad anclada por pidfd.
- Arranque, verificación de aislamiento en 5 compuertas y detención atómica de
  unidades transitorias `systemd --user` con:
    * PrivateUsers=yes
    * InaccessiblePaths=/sys/fs/cgroup
    * KillMode=control-group
    * CUDA_VISIBLE_DEVICES="" (y variables de ocultación GPU complementarias)
- Separación estricta frente a servidores externos preexistentes.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
import math
import os
import re
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Optional, Sequence, Union
import uuid

from ..coordinator import EstadoEndpoint, OllamaCoordinator
from ..exceptions import LLMConfigurationError, LLMConnectionError, LLMProviderError

DEFAULT_OLLAMA_HOST = os.getenv("MADELINE_OLLAMA_HOST", "127.0.0.1")
DEFAULT_OLLAMA_MANAGED_PORT = int(os.getenv("MADELINE_OLLAMA_PORT", "11435"))
DEFAULT_OLLAMA_KEEP_ALIVE = os.getenv("MADELINE_OLLAMA_KEEP_ALIVE", "5m")

_MAX_GO_DURATION_NS = (1 << 63) - 1
_NS_POR_SEGUNDO = 1_000_000_000
_KEEP_ALIVE_UNIDADES_NS: dict[str, int] = {
    "ns": 1,
    "us": 1_000,
    "µs": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "m": 60_000_000_000,
    "h": 3_600_000_000_000,
}
_KEEP_ALIVE_SEGMENTO_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ns|us|µs|ms|s|m|h)", re.ASCII)
_KEEP_ALIVE_DURACION_RE = re.compile(r"^(?:[0-9]+(?:\.[0-9]+)?(?:ns|us|µs|ms|s|m|h))+$", re.ASCII)
_KEEP_ALIVE_ENTERO_POSITIVO_RE = re.compile(r"^[1-9][0-9]*$", re.ASCII)


def validar_keep_alive(keep_alive: Any) -> Union[int, float, str]:
    """Valida estrictamente que `keep_alive` sea compatible con Go (`time.ParseDuration`).

    Formatos admitidos:
    - `int` o `float` finito > 0 (en segundos, excluye `bool`) equivalente a `[1, 2^63-1]` ns.
    - Cadena entera positiva ASCII sin espacios ni signo (ej. `"300"`) en `[1, 2^63-1]` ns.
    - Duración positiva ASCII sin espacios ni signo compuesta por segmentos
      `<digitos>[.<digitos>]<unidad>` (`ns|us|µs|ms|s|m|h`) con suma exacta en `[1, 2^63-1]` ns.
      Rechaza dígitos Unicode, valores `< 1ns`, overflows `> int64`, `0`, `0m0s`, `.0s`,
      negativos (`-5m`, `-1`), espacios y formatos malformados.
    """
    if isinstance(keep_alive, bool):
        raise LLMConfigurationError(
            "keep_alive no puede ser booleano; debe ser una duración positiva (ej. '5m').",
            provider="ollama",
        )
    if isinstance(keep_alive, (int, float)):
        if not math.isfinite(keep_alive) or keep_alive <= 0:
            raise LLMConfigurationError(
                f"keep_alive numérico inválido ({keep_alive!r}): debe ser finito y mayor que 0.",
                provider="ollama",
            )
        total_ns = Fraction(str(keep_alive)) * _NS_POR_SEGUNDO
        if total_ns < 1 or total_ns > _MAX_GO_DURATION_NS:
            raise LLMConfigurationError(
                f"keep_alive numérico fuera de rango Go int64 ns ({keep_alive!r}).",
                provider="ollama",
            )
        return keep_alive
    if not isinstance(keep_alive, str):
        raise LLMConfigurationError(
            f"keep_alive debe ser int, float o str positivo, recibido {type(keep_alive).__name__}.",
            provider="ollama",
        )
    if not keep_alive or any(ch.isspace() for ch in keep_alive):
        raise LLMConfigurationError(
            "keep_alive no puede estar vacío ni contener espacios.",
            provider="ollama",
        )
    if _KEEP_ALIVE_ENTERO_POSITIVO_RE.fullmatch(keep_alive):
        total_ns = Fraction(int(keep_alive)) * _NS_POR_SEGUNDO
        if total_ns > _MAX_GO_DURATION_NS:
            raise LLMConfigurationError(
                f"keep_alive entero excede el máximo representable por Go int64 ns ({keep_alive!r}).",
                provider="ollama",
            )
        return keep_alive
    if not _KEEP_ALIVE_DURACION_RE.fullmatch(keep_alive):
        raise LLMConfigurationError(
            f"Formato inválido de keep_alive ({keep_alive!r}): use duración positiva ASCII sin signo "
            "(ej. '5m', '1h30m', '250ms').",
            provider="ollama",
        )

    total_ns = Fraction(0)
    for num_str, unidad in _KEEP_ALIVE_SEGMENTO_RE.findall(keep_alive):
        total_ns += Fraction(num_str) * _KEEP_ALIVE_UNIDADES_NS[unidad]

    if total_ns < 1:
        raise LLMConfigurationError(
            f"keep_alive debe ser al menos 1ns para evitar truncamiento a 0 en Go ({keep_alive!r}).",
            provider="ollama",
        )
    if total_ns > _MAX_GO_DURATION_NS:
        raise LLMConfigurationError(
            f"keep_alive excede el máximo representable por Go int64 ns ({keep_alive!r}).",
            provider="ollama",
        )
    return keep_alive

ENV_AISLAMIENTO_GPU: dict[str, str] = {
    "CUDA_VISIBLE_DEVICES": "",
    "ROCR_VISIBLE_DEVICES": "",
    "GPU_DEVICE_ORDINAL": "",
    "GGML_VK_VISIBLE_DEVICES": "",
    "OLLAMA_VULKAN": "0",
}


class EstadoProceso(str, Enum):
    """Estado verificable de un proceso rastreado por pidfd o /proc."""

    VIVO = "VIVO"
    MUERTO = "MUERTO"
    DESCONOCIDO = "DESCONOCIDO"


class _ErrorArranquePermanente(LLMConnectionError):
    """Error interno cuando la unidad transitoria entra en ActiveState=failed."""


@dataclass(frozen=True)
class ProcStatInfo:
    """Campos relevantes parseados de /proc/<pid>/stat de forma robusta."""

    pid: int
    comm: str
    state: str
    ppid: int
    pgrp: int
    starttime: int


@dataclass
class RunnerHandle:
    """Referencia verificada a un proceso runner perteneciente a la instancia."""

    pid: int
    starttime: int
    pidfd: Optional[int] = None
    es_hijo_directo: bool = False
    mismo_pgrp: bool = False
    en_cgroup: bool = False

    def cerrar_fd(self) -> None:
        """Cierra de forma idempotente el descriptor pidfd si está abierto."""
        if self.pidfd is not None:
            fd = self.pidfd
            self.pidfd = None
            try:
                os.close(fd)
            except OSError:
                pass


@dataclass(frozen=True)
class CommandResult:
    """Resultado normalizado de ejecución de comandos systemd."""

    returncode: int
    stdout: str
    stderr: str


def _default_command_runner(
    cmd: Sequence[str],
    timeout: float = 10.0,
    env: Optional[dict[str, str]] = None,
) -> CommandResult:
    """Ejecuta un comando del sistema de forma síncrona sin shell=True."""
    proc = subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    return CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def parsear_stat_proc(pid: int, proc_root: str = "/proc") -> Optional[ProcStatInfo]:
    """Parsea /proc/<pid>/stat aislando `comm` entre el primer '(' y último ')'."""
    if pid <= 0:
        return None
    ruta = os.path.join(proc_root, str(pid), "stat")
    try:
        with open(ruta, "r", encoding="utf-8", errors="replace") as f:
            contenido = f.read()
    except OSError:
        return None

    idx_open = contenido.find("(")
    idx_close = contenido.rfind(")")
    if idx_open <= 0 or idx_close <= idx_open:
        return None

    pid_str = contenido[:idx_open].strip()
    comm = contenido[idx_open + 1 : idx_close]
    post = contenido[idx_close + 1 :].strip().split()
    if len(post) < 20:
        return None

    try:
        pid_leido = int(pid_str)
        state = post[0]
        ppid = int(post[1])
        pgrp = int(post[2])
        starttime = int(post[19])
    except ValueError:
        return None

    if pid_leido != pid:
        return None

    return ProcStatInfo(
        pid=pid_leido,
        comm=comm,
        state=state,
        ppid=ppid,
        pgrp=pgrp,
        starttime=starttime,
    )


def _leer_pid_desde_fdinfo(pidfd: int, proc_root: str = "/proc") -> Optional[int]:
    """Lee el campo `Pid:` de /proc/self/fdinfo/<pidfd>."""
    ruta = os.path.join(proc_root, "self", "fdinfo", str(pidfd))
    try:
        with open(ruta, "r", encoding="utf-8", errors="replace") as f:
            for linea in f:
                if linea.startswith("Pid:"):
                    return int(linea.split(":", 1)[1].strip())
    except (OSError, ValueError):
        return None
    return None


def abrir_pidfd_verificado(
    pid: int,
    ppid_esperado: Optional[int] = None,
    starttime_esperado: Optional[int] = None,
    es_hijo_directo: bool = False,
    mismo_pgrp: bool = False,
    en_cgroup: bool = False,
    proc_root: str = "/proc",
    pidfd_opener: Optional[Callable[[int, int], int]] = None,
) -> Optional[RunnerHandle]:
    """Abre un pidfd y revalida /proc/<pid>/stat y fdinfo para cerrar carreras TOCTOU."""
    opener = pidfd_opener or getattr(os, "pidfd_open", None)
    stat_antes = parsear_stat_proc(pid, proc_root=proc_root)
    if stat_antes is None or stat_antes.state == "Z":
        return None
    if ppid_esperado is not None and stat_antes.ppid != ppid_esperado:
        return None
    if starttime_esperado is not None and stat_antes.starttime != starttime_esperado:
        return None

    fd: Optional[int] = None
    if opener is not None:
        try:
            fd = opener(pid, 0)
        except OSError:
            return None

    # Revalidación post-apertura para descartar reciclaje de PID entre stat y pidfd_open
    stat_despues = parsear_stat_proc(pid, proc_root=proc_root)
    valido = (
        stat_despues is not None
        and stat_despues.starttime == stat_antes.starttime
        and (ppid_esperado is None or stat_despues.ppid == ppid_esperado)
    )
    if valido and fd is not None and pidfd_opener is None:
        pid_fdinfo = _leer_pid_desde_fdinfo(fd, proc_root=proc_root)
        if pid_fdinfo is not None and pid_fdinfo != pid:
            valido = False

    if not valido:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return None

    return RunnerHandle(
        pid=pid,
        starttime=stat_despues.starttime,
        pidfd=fd,
        es_hijo_directo=es_hijo_directo,
        mismo_pgrp=mismo_pgrp,
        en_cgroup=en_cgroup,
    )


def _ping_pidfd_vivo(pidfd: int) -> EstadoProceso:
    """Comprueba mediante pidfd_send_signal(fd, 0) si el proceso sigue vivo en kernel."""
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is None:
        return EstadoProceso.DESCONOCIDO
    try:
        sender(pidfd, 0)
        return EstadoProceso.VIVO
    except ProcessLookupError:
        return EstadoProceso.MUERTO
    except PermissionError:
        return EstadoProceso.VIVO
    except OSError:
        return EstadoProceso.DESCONOCIDO


def verificar_titularidad_socket(
    pid: int,
    starttime_esperado: int,
    puerto: int,
    proc_root: str = "/proc",
) -> tuple[bool, str, Optional[int]]:
    """Valida identidad del proceso, network namespace y enlace de socket en /proc/net/tcp(6)."""
    stat_info = parsear_stat_proc(pid, proc_root=proc_root)
    if stat_info is None or stat_info.starttime != starttime_esperado:
        return False, "PID_RECICLADO_O_INVALIDO", None

    try:
        ns_propio = os.readlink(os.path.join(proc_root, "self", "ns", "net"))
        ns_objetivo = os.readlink(os.path.join(proc_root, str(pid), "ns", "net"))
        if ns_propio != ns_objetivo:
            return False, "NAMESPACE_DIFERENTE", None
    except OSError:
        return False, "ERROR_NAMESPACE", None

    inodos_proceso: set[int] = set()
    ruta_fd = os.path.join(proc_root, str(pid), "fd")
    try:
        for fd_name in os.listdir(ruta_fd):
            try:
                enlace = os.readlink(os.path.join(ruta_fd, fd_name))
                if enlace.startswith("socket:[") and enlace.endswith("]"):
                    inodos_proceso.add(int(enlace[8:-1]))
            except (OSError, ValueError):
                continue
    except OSError:
        return False, "ERROR_LECTURA_FD", None

    if not inodos_proceso:
        return False, "SIN_SOCKETS", None

    puerto_hex = f"{puerto:04X}"
    for nombre_tabla, es_ipv6 in (("tcp", False), ("tcp6", True)):
        ruta_tabla = os.path.join(proc_root, "net", nombre_tabla)
        if not os.path.exists(ruta_tabla):
            continue
        try:
            with open(ruta_tabla, "r", encoding="utf-8", errors="replace") as f:
                next(f, None)
                for linea in f:
                    partes = linea.strip().split()
                    if len(partes) < 10 or partes[3] != "0A":
                        continue
                    dir_local = partes[1].upper()
                    try:
                        inodo = int(partes[9])
                    except ValueError:
                        continue
                    if inodo not in inodos_proceso:
                        continue
                    tipo = _clasificar_direccion_listen(dir_local, puerto_hex, es_ipv6)
                    if tipo is not None:
                        return True, tipo, inodo
        except OSError:
            return False, "ERROR_LECTURA_TCP", None

    return False, "NO_COINCIDE_SOCKET", None


def _clasificar_direccion_listen(dir_local: str, puerto_hex: str, es_ipv6: bool) -> Optional[str]:
    if not es_ipv6:
        if dir_local == f"0100007F:{puerto_hex}":
            return "LOOPBACK_IPV4"
        if dir_local == f"00000000:{puerto_hex}":
            return "COMODIN_IPV4"
        return None
    if dir_local == f"00000000000000000000000001000000:{puerto_hex}":
        return "LOOPBACK_IPV6"
    if dir_local == f"00000000000000000000000000000000:{puerto_hex}":
        return "COMODIN_IPV6"
    return None


def inodo_socket_sigue_escuchando(
    inodo_esperado: int,
    puerto: int,
    proc_root: str = "/proc",
) -> bool:
    """Comprueba conservadoramente si el inodo del socket original sigue en LISTEN."""
    puerto_hex = f"{puerto:04X}"
    for nombre_tabla in ("tcp", "tcp6"):
        ruta_tabla = os.path.join(proc_root, "net", nombre_tabla)
        if not os.path.exists(ruta_tabla):
            continue
        try:
            with open(ruta_tabla, "r", encoding="utf-8", errors="replace") as f:
                next(f, None)
                for linea in f:
                    partes = linea.strip().split()
                    if len(partes) >= 10 and partes[3] == "0A":
                        if partes[1].upper().endswith(f":{puerto_hex}"):
                            if int(partes[9]) == inodo_esperado:
                                return True
        except (OSError, ValueError):
            return True  # Fail-closed ante imposibilidad de lectura
    return False


def leer_pids_cgroup(ruta_cgroup_abs: str) -> Optional[set[int]]:
    """Lee PIDs residentes en `<cgroup>/cgroup.procs`; retorna None si no puede leerse."""
    ruta_procs = os.path.join(ruta_cgroup_abs, "cgroup.procs")
    if not os.path.exists(ruta_procs):
        return None
    pids: set[int] = set()
    try:
        with open(ruta_procs, "r", encoding="utf-8", errors="replace") as f:
            for linea in f:
                txt = linea.strip()
                if txt.isdigit():
                    pids.add(int(txt))
        return pids
    except OSError:
        return None


def cgroup_esta_vacio_o_eliminado(ruta_cgroup_abs: str) -> bool:
    """Confirma objetivamente que el cgroup no tiene procesos o fue eliminado."""
    if not os.path.exists(ruta_cgroup_abs):
        return True
    pids = leer_pids_cgroup(ruta_cgroup_abs)
    if pids is None or len(pids) > 0:
        return False

    ruta_events = os.path.join(ruta_cgroup_abs, "cgroup.events")
    if os.path.exists(ruta_events):
        try:
            with open(ruta_events, "r", encoding="utf-8", errors="replace") as f:
                contenido = f.read()
            for linea in contenido.splitlines():
                if linea.startswith("populated "):
                    return linea.split()[1] == "0"
        except OSError:
            return False
    return True


def _verificar_mountinfo_cgroup_inaccesible(pid: int, proc_root: str = "/proc") -> bool:
    """Verifica en /proc/<pid>/mountinfo que el montaje efectivo final de /sys/fs/cgroup sea inaccesible."""
    ruta = os.path.join(proc_root, str(pid), "mountinfo")
    try:
        with open(ruta, "r", encoding="utf-8", errors="replace") as f:
            lineas = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return False

    ultimo_montaje_cgroup: Optional[tuple[str, list[str], str]] = None
    for linea in lineas:
        partes_sep = linea.split(" - ", 1)
        if len(partes_sep) != 2:
            return False  # Línea malformada -> fail-closed
        izq = partes_sep[0].split()
        der = partes_sep[1].split()
        if len(izq) < 6 or len(der) < 2:
            return False  # Campos incompletos -> fail-closed
        root_mount = izq[3]
        mount_point = izq[4]
        mount_opts = izq[5].split(",")
        fstype = der[0]
        if mount_point == "/sys/fs/cgroup":
            ultimo_montaje_cgroup = (root_mount, mount_opts, fstype)

    if ultimo_montaje_cgroup is None:
        return False

    root_mount, mount_opts, fstype = ultimo_montaje_cgroup
    return (
        "/systemd/inaccessible/" in root_mount
        and "ro" in mount_opts
        and fstype == "tmpfs"
    )


def _verificar_uid_map_private_users(pid: int, proc_root: str = "/proc") -> bool:
    """Verifica en /proc/<pid>/uid_map que PrivateUsers=yes no mapea UID 0 del host."""
    ruta = os.path.join(proc_root, str(pid), "uid_map")
    try:
        with open(ruta, "r", encoding="utf-8", errors="replace") as f:
            lineas = [ln.strip().split() for ln in f if ln.strip()]
    except OSError:
        return False

    if not lineas:
        return False

    for campos in lineas:
        if len(campos) != 3:
            return False
        try:
            ns_uid = int(campos[0])
            host_uid = int(campos[1])
            length = int(campos[2])
        except ValueError:
            return False
        # Si mapea root (host UID 0) o un rango amplio multiusuario, falla aislamiento PrivateUsers
        if host_uid == 0 or ns_uid == 0 or length != 1:
            return False
    return True


def _verificar_proc_cgroup_pertenece(pid: int, control_group: str, proc_root: str = "/proc") -> bool:
    """Verifica que la entrada 0:: de /proc/<pid>/cgroup coincida con el cgroup esperado."""
    ruta = os.path.join(proc_root, str(pid), "cgroup")
    try:
        with open(ruta, "r", encoding="utf-8", errors="replace") as f:
            for linea in f:
                linea_limpia = linea.strip()
                if linea_limpia.startswith("0::"):
                    return linea_limpia[3:] == control_group
    except OSError:
        return False
    return False


class OllamaServerManager:
    """Gestor de ciclo de vida aislado para Ollama (systemd --user) o cliente externo.

    Reglas invariantes:
    - En modo gestionado (`modo_gestionado=True`), exige obligatoriamente una unidad
      transitoria `systemd --user` con PrivateUsers=yes, InaccessiblePaths=/sys/fs/cgroup,
      KillMode=control-group y puerto dedicado. JAMÁS usa Popen directo ni adopta un
      servidor externo preexistente.
    - En modo externo (`modo_gestionado=False`), JAMÁS inicia, detiene, mata ni descarga
      modelos del servidor externo.
    - Nunca utiliza `__del__` como mecanismo principal de cierre.
    """

    def __init__(
        self,
        coordinator: Optional[OllamaCoordinator] = None,
        modo_gestionado: bool = True,
        host: str = DEFAULT_OLLAMA_HOST,
        port: int = DEFAULT_OLLAMA_MANAGED_PORT,
        ollama_bin: str = "ollama",
        ollama_models: Optional[str] = None,
        ollama_home: Optional[str] = None,
        keep_alive: Union[int, float, str] = DEFAULT_OLLAMA_KEEP_ALIVE,
        proc_root: str = "/proc",
        cgroup_root: str = "/sys/fs/cgroup",
        command_runner: Optional[Callable[..., CommandResult]] = None,
        nvidia_smi_runner: Optional[Callable[..., CommandResult]] = None,
        pidfd_opener: Optional[Callable[[int, int], int]] = None,
        startup_timeout_s: float = 10.0,
        stop_timeout_s: float = 5.0,
        health_probe: Optional[Callable[[str], bool]] = None,
        register_atexit: bool = True,
    ) -> None:
        self._coordinator = coordinator or OllamaCoordinator()
        self._modo_gestionado = bool(modo_gestionado)
        self._host = host
        self._port = int(port)
        self._base_url = f"http://{self._host}:{self._port}"
        self._ollama_bin = ollama_bin
        self._ollama_models = ollama_models or os.getenv("MADELINE_OLLAMA_MODELS")
        self._ollama_home = ollama_home or os.getenv("MADELINE_OLLAMA_HOME")
        self._keep_alive = validar_keep_alive(keep_alive)
        self._proc_root = proc_root
        self._cgroup_root = cgroup_root
        self._run_cmd = command_runner or _default_command_runner
        self._nvidia_smi_runner = nvidia_smi_runner or _default_command_runner
        self._pidfd_opener = pidfd_opener
        self._startup_timeout_s = startup_timeout_s
        self._stop_timeout_s = stop_timeout_s
        self._health_probe = health_probe

        # Lock interno para proteger estado de runners, pidfds y cierre concurrente.
        # _shutdown_lock serializa detener()/revalidar/arranque; orden estricto: _shutdown_lock -> _lock.
        self._shutdown_lock = threading.Lock()
        self._lock = threading.Lock()

        # Estado de la unidad gestionada activa
        self._unit_name: Optional[str] = None
        self._control_group: Optional[str] = None
        self._main_pid: Optional[int] = None
        self._main_starttime: Optional[int] = None
        self._inodo_socket_original: Optional[int] = None
        self._ultimo_inodo_socket: Optional[int] = None
        self._runners: dict[int, RunnerHandle] = {}
        self._aislamiento_verificado: bool = False
        self._cerrado: bool = False
        self._nvidia_telemetria_estado: str = "DESCONOCIDA"
        self._nvidia_telemetria_detalle: str = ""
        self._ultimo_error_arranque: str = ""

        if register_atexit:
            atexit.register(self._atexit_cleanup)

    @property
    def coordinator(self) -> OllamaCoordinator:
        return self._coordinator

    @property
    def modo_gestionado(self) -> bool:
        return self._modo_gestionado

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def unit_name(self) -> Optional[str]:
        with self._lock:
            return self._unit_name

    @property
    def control_group(self) -> Optional[str]:
        with self._lock:
            return self._control_group

    @property
    def main_pid(self) -> Optional[int]:
        with self._lock:
            return self._main_pid

    @property
    def aislamiento_verificado(self) -> bool:
        with self._lock:
            return self._aislamiento_verificado

    @property
    def nvidia_telemetria_estado(self) -> str:
        with self._lock:
            return self._nvidia_telemetria_estado

    @property
    def nvidia_telemetria_detalle(self) -> str:
        with self._lock:
            return self._nvidia_telemetria_detalle

    @property
    def ultimo_error_arranque(self) -> str:
        with self._lock:
            return self._ultimo_error_arranque

    def _atexit_cleanup(self) -> None:
        if not self._modo_gestionado:
            return
        with self._shutdown_lock:
            with self._lock:
                pendiente = self._unit_name is not None
            if pendiente:
                try:
                    self._detener_serializado()
                except Exception:
                    pass

    def _limpiar_runners_fds(self) -> None:
        with self._lock:
            handles = list(self._runners.values())
            self._runners.clear()
        for handle in handles:
            handle.cerrar_fd()

    def _parsear_systemctl_show(self, stdout: str) -> dict[str, str]:
        props: dict[str, str] = {}
        for linea in stdout.splitlines():
            if "=" in linea:
                clave, valor = linea.split("=", 1)
                props[clave.strip()] = valor.strip()
        return props

    def _consultar_unidad_systemd(self, unit_name: str) -> dict[str, str]:
        res = self._run_cmd(
            [
                "systemctl",
                "--user",
                "show",
                unit_name,
                "-p",
                "Id",
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "ControlGroup",
                "-p",
                "MainPID",
                "-p",
                "PrivateUsers",
                "-p",
                "KillMode",
                "-p",
                "InaccessiblePaths",
                "-p",
                "Result",
                "-p",
                "ExecMainCode",
                "-p",
                "ExecMainStatus",
            ],
            timeout=5.0,
        )
        if res.returncode != 0:
            raise LLMConnectionError(
                f"No se pudo consultar la unidad systemd --user '{unit_name}': {res.stderr.strip() or res.stdout.strip()}",
                provider="ollama",
            )
        return self._parsear_systemctl_show(res.stdout)

    def _verificar_unidad_y_aislamiento(self, unit_name: str) -> tuple[str, int, int, str, int]:
        """Ejecuta las comprobaciones obligatorias 1-4 sobre la unidad recién creada.

        Nota sobre frontera DAC aceptada:
        Comprueba que la unidad se creó, MainPID pertenece al cgroup esperado,
        uid_map refleja PrivateUsers=yes y mountinfo refleja InaccessiblePaths=/sys/fs/cgroup.
        No afirma impedir que otro proceso externo del mismo UID migre PIDs fuera del
        mount namespace.
        """
        props = self._consultar_unidad_systemd(unit_name)
        load_state = props.get("LoadState", "")
        active_state = props.get("ActiveState", "")
        sub_state = props.get("SubState", "")
        control_group = props.get("ControlGroup", "")
        main_pid_str = props.get("MainPID", "0")

        # Corte rápido si la unidad entró en estado failed al arrancar
        if active_state == "failed" or sub_state == "failed":
            evidencia = (
                f"Result={props.get('Result', '')}, "
                f"ExecMainCode={props.get('ExecMainCode', '')}, "
                f"ExecMainStatus={props.get('ExecMainStatus', '')}"
            )
            with self._lock:
                self._ultimo_error_arranque = evidencia
            raise _ErrorArranquePermanente(
                f"La unidad '{unit_name}' falló al iniciar (ActiveState={active_state!r}, {evidencia}).",
                provider="ollama",
            )

        # 1. Confirmar que la unidad realmente se creó y está activa
        if load_state != "loaded" or active_state != "active" or not control_group:
            raise LLMProviderError(
                f"Unidad '{unit_name}' no activa o mal configurada "
                f"(LoadState={load_state!r}, ActiveState={active_state!r}, ControlGroup={control_group!r}).",
                provider="ollama",
            )

        if props.get("PrivateUsers", "no").lower() != "yes":
            raise LLMProviderError(
                f"Aislamiento fallido: PrivateUsers no está activo en '{unit_name}'.",
                provider="ollama",
            )
        if props.get("KillMode", "") != "control-group":
            raise LLMProviderError(
                f"Aislamiento fallido: KillMode no es 'control-group' en '{unit_name}'.",
                provider="ollama",
            )
        inaccessible = props.get("InaccessiblePaths", "")
        if "/sys/fs/cgroup" not in inaccessible.split():
            raise LLMProviderError(
                f"Aislamiento fallido: InaccessiblePaths=/sys/fs/cgroup ausente en '{unit_name}'.",
                provider="ollama",
            )

        ruta_cgroup_abs = os.path.join(self._cgroup_root, control_group.lstrip("/"))
        if not os.path.isdir(ruta_cgroup_abs):
            raise LLMProviderError(
                f"Aislamiento fallido: el directorio cgroup '{ruta_cgroup_abs}' no existe.",
                provider="ollama",
            )

        try:
            main_pid = int(main_pid_str)
        except ValueError as exc:
            raise LLMProviderError(
                f"MainPID inválido en '{unit_name}': {main_pid_str!r}.",
                provider="ollama",
            ) from exc

        if main_pid <= 0:
            raise LLMProviderError(
                f"La unidad '{unit_name}' no tiene MainPID válido ({main_pid}).",
                provider="ollama",
            )

        # 2. Confirmar pertenencia de MainPID al cgroup esperado y su starttime
        if not _verificar_proc_cgroup_pertenece(main_pid, control_group, proc_root=self._proc_root):
            raise LLMProviderError(
                f"MainPID {main_pid} no pertenece al cgroup '{control_group}'.",
                provider="ollama",
            )

        pids_cgroup = leer_pids_cgroup(ruta_cgroup_abs)
        if pids_cgroup is None or main_pid not in pids_cgroup:
            raise LLMProviderError(
                f"MainPID {main_pid} no figura en cgroup.procs de '{control_group}'.",
                provider="ollama",
            )

        stat_main = parsear_stat_proc(main_pid, proc_root=self._proc_root)
        if stat_main is None:
            raise LLMProviderError(
                f"No se pudo leer /proc/{main_pid}/stat para verificar MainPID.",
                provider="ollama",
            )

        # 3. Confirmar uid_map y mountinfo dentro de la unidad aislada
        if not _verificar_uid_map_private_users(main_pid, proc_root=self._proc_root):
            raise LLMProviderError(
                f"Aislamiento fallido: uid_map de PID {main_pid} no refleja PrivateUsers=yes.",
                provider="ollama",
            )
        if not _verificar_mountinfo_cgroup_inaccesible(main_pid, proc_root=self._proc_root):
            raise LLMProviderError(
                f"Aislamiento fallido: mountinfo de PID {main_pid} no muestra /sys/fs/cgroup inaccesible.",
                provider="ollama",
            )

        # 4. Verificar titularidad del socket en el puerto dedicado
        sock_ok, tipo_enlace, inodo = verificar_titularidad_socket(
            main_pid,
            stat_main.starttime,
            self._port,
            proc_root=self._proc_root,
        )
        if not sock_ok or inodo is None:
            raise LLMProviderError(
                f"No se pudo verificar titularidad del socket {self._host}:{self._port} para PID {main_pid} ({tipo_enlace}).",
                provider="ollama",
            )
        if tipo_enlace not in ("LOOPBACK_IPV4", "LOOPBACK_IPV6"):
            raise LLMProviderError(
                f"El servidor gestionado debe escuchar en loopback, detectado '{tipo_enlace}'.",
                provider="ollama",
            )

        return control_group, main_pid, stat_main.starttime, tipo_enlace, inodo

    def _esperar_y_validar_arranque(self, unit_name: str) -> None:
        deadline = time.monotonic() + self._startup_timeout_s
        ultimo_error: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                control_group, main_pid, starttime, _tipo, inodo = self._verificar_unidad_y_aislamiento(unit_name)
                if self._health_probe is not None and not self._health_probe(self._base_url):
                    raise LLMConnectionError(
                        f"El servidor Ollama en {self._base_url} aún no responde al endpoint de salud.",
                        provider="ollama",
                    )
                handle = abrir_pidfd_verificado(
                    main_pid,
                    starttime_esperado=starttime,
                    en_cgroup=True,
                    proc_root=self._proc_root,
                    pidfd_opener=self._pidfd_opener,
                )
                with self._lock:
                    self._unit_name = unit_name
                    self._control_group = control_group
                    self._main_pid = main_pid
                    self._main_starttime = starttime
                    self._inodo_socket_original = inodo
                    self._ultimo_inodo_socket = inodo
                    self._aislamiento_verificado = True
                    self._ultimo_error_arranque = ""
                    if handle is not None:
                        self._runners[main_pid] = handle
                self.descubrir_runners()
                self._coordinator.marcar_disponible(
                    f"Unidad '{unit_name}' verificada y aislada en {control_group}."
                )
                return
            except _ErrorArranquePermanente as exc:
                ultimo_error = exc
                break
            except Exception as exc:
                ultimo_error = exc
                time.sleep(0.1)

        detalle_err = str(ultimo_error)
        with self._lock:
            self._ultimo_error_arranque = detalle_err
        raise LLMConnectionError(
            f"Fallo de aislamiento o arranque en unidad '{unit_name}': {detalle_err}",
            provider="ollama",
        ) from ultimo_error

    def asegurar_servidor_activo(self) -> None:
        """Asegura disponibilidad del servidor gestionado (o valida precondición de externo).

        En modo gestionado:
        - Serializa comprobación, creación y limpieza de fallo bajo `_shutdown_lock`
          (orden estricto `_shutdown_lock -> _lock`) sin retener `_lock` en esperas externas.
        - Si no hay unidad activa verificada, crea una nueva unidad transitoria systemd --user.
        - Nunca adopta servidores externos en 11434 ni recurre a Popen directo.
        - Ante cualquier fallo de creación o verificación de aislamiento, limpia la unidad
          parcial y marca NO_DISPONIBLE (fail-closed).
        """
        if not self._coordinator.esta_disponible():
            raise LLMProviderError(
                f"Servidor Ollama no disponible ({self._coordinator.estado_endpoint.value}): "
                f"{self._coordinator.motivo_estado}",
                provider="ollama",
            )

        if not self._modo_gestionado:
            return

        with self._shutdown_lock:
            if not self._coordinator.esta_disponible():
                raise LLMProviderError(
                    f"Servidor Ollama no disponible ({self._coordinator.estado_endpoint.value}): "
                    f"{self._coordinator.motivo_estado}",
                    provider="ollama",
                )

            with self._lock:
                unidad_actual = self._unit_name
                ya_verificada = self._aislamiento_verificado

            if unidad_actual is not None and ya_verificada:
                try:
                    self._verificar_unidad_y_aislamiento(unidad_actual)
                    self.descubrir_runners()
                    return
                except Exception as exc:
                    with self._lock:
                        self._aislamiento_verificado = False
                    self._coordinator.marcar_degradado(f"Pérdida de verificación de unidad: {exc}")
                    raise

            # Si existe una unidad previa sin aislamiento verificado, limpiarla antes de arrancar otra
            if unidad_actual is not None and not ya_verificada:
                if not self._detener_serializado():
                    raise LLMConnectionError(
                        f"La unidad previa '{unidad_actual}' no completó limpieza; se impide arranque duplicado.",
                        provider="ollama",
                    )
                if not self._coordinator.esta_disponible():
                    raise LLMProviderError(
                        f"Servidor Ollama no disponible ({self._coordinator.estado_endpoint.value}): "
                        f"{self._coordinator.motivo_estado}",
                        provider="ollama",
                    )

            unit_name = f"madeline-ollama-{self._port}-{uuid.uuid4().hex[:8]}.service"
            cmd = [
                "systemd-run",
                "--user",
                f"--unit={unit_name}",
                "--property=PrivateUsers=yes",
                "--property=InaccessiblePaths=/sys/fs/cgroup",
                "--property=KillMode=control-group",
                f"--setenv=OLLAMA_HOST={self._host}:{self._port}",
                f"--setenv=OLLAMA_KEEP_ALIVE={self._keep_alive}",
            ]
            for clave, valor in ENV_AISLAMIENTO_GPU.items():
                cmd.append(f"--setenv={clave}={valor}")
            if self._ollama_models:
                cmd.append(f"--setenv=OLLAMA_MODELS={self._ollama_models}")
            if self._ollama_home:
                cmd.append(f"--setenv=HOME={self._ollama_home}")
            cmd.extend([self._ollama_bin, "serve"])

            try:
                res = self._run_cmd(cmd, timeout=10.0)
            except Exception as exc:
                msg = f"Error invocando systemd-run --user: {exc}"
                with self._lock:
                    self._ultimo_error_arranque = msg
                self._coordinator.marcar_no_disponible(msg)
                raise LLMConnectionError(
                    f"No se pudo invocar systemd-run --user para aislar Ollama: {exc}",
                    provider="ollama",
                ) from exc

            if res.returncode != 0:
                detalle = res.stderr.strip() or res.stdout.strip() or f"rc={res.returncode}"
                msg = f"systemd-run --user falló: {detalle}"
                with self._lock:
                    self._ultimo_error_arranque = msg
                self._coordinator.marcar_no_disponible(msg)
                raise LLMConnectionError(
                    f"No se pudo crear la unidad transitoria '{unit_name}': {detalle}",
                    provider="ollama",
                )

            try:
                self._esperar_y_validar_arranque(unit_name)
            except Exception as exc:
                # Fail-closed bajo _shutdown_lock: usar _detener_serializado() para no reentrar el lock
                with self._lock:
                    self._unit_name = unit_name
                    self._ultimo_error_arranque = str(exc)
                try:
                    self._detener_serializado()
                except Exception:
                    pass
                with self._lock:
                    self._aislamiento_verificado = False
                self._coordinator.marcar_no_disponible(str(exc))
                raise

    def descubrir_runners(self) -> dict[int, RunnerHandle]:
        """Descubre runners propios tratando parentesco, grupo y cgroup independientemente.

        - Parentesco verificado (`ppid == main_pid`): captura hijos directos incluso si
          cambiaron de PGID.
        - Membresía de cgroup (`cgroup.procs`): captura cualquier descendiente dentro del cgroup.
        - Membresía de PGID (`pgrp == main_pid`): solo si el líder sigue sin recolectar.
        - Revalida starttime en PIDs ya registrados: si el PID fue reciclado, cierra la
          referencia obsoleta y abre un nuevo pidfd verificado.
        - Nunca adopta procesos únicamente por nombre (`comm`).
        """
        with self._lock:
            if not self._modo_gestionado or self._main_pid is None:
                return dict(self._runners)
            main_pid = self._main_pid
            control_group = self._control_group

        pids_cgroup: set[int] = set()
        if control_group:
            ruta_cg = os.path.join(self._cgroup_root, control_group.lstrip("/"))
            leidos = leer_pids_cgroup(ruta_cg)
            if leidos is not None:
                pids_cgroup = leidos

        try:
            entradas = os.listdir(self._proc_root)
        except OSError:
            with self._lock:
                return dict(self._runners)

        for entrada in entradas:
            if not entrada.isdigit():
                continue
            pid = int(entrada)

            info = parsear_stat_proc(pid, proc_root=self._proc_root)
            if info is None or info.state == "Z":
                continue

            es_hijo = info.ppid == main_pid
            en_cg = pid in pids_cgroup
            mismo_pgrp = info.pgrp == main_pid

            with self._lock:
                existente = self._runners.get(pid)

            if existente is not None and existente.starttime == info.starttime:
                # Misma instancia física de proceso: actualizar banderas de pertenencia si corresponde
                with self._lock:
                    if pid in self._runners and self._runners[pid].starttime == info.starttime:
                        self._runners[pid].es_hijo_directo |= es_hijo
                        self._runners[pid].en_cgroup |= en_cg
                        self._runners[pid].mismo_pgrp |= mismo_pgrp
                continue

            if existente is None and not (es_hijo or en_cg or mismo_pgrp):
                continue

            # Si existía otro proceso con el mismo PID pero starttime diferente (PID reciclado):
            # cerrar la referencia antigua antes de re-evaluar al nuevo proceso.
            handle_obsoleto: Optional[RunnerHandle] = None
            if existente is not None and existente.starttime != info.starttime:
                with self._lock:
                    if pid in self._runners and self._runners[pid].starttime != info.starttime:
                        handle_obsoleto = self._runners.pop(pid)
                if handle_obsoleto is not None:
                    handle_obsoleto.cerrar_fd()
                if not (es_hijo or en_cg or mismo_pgrp):
                    continue

            handle = abrir_pidfd_verificado(
                pid,
                ppid_esperado=main_pid if es_hijo else None,
                starttime_esperado=info.starttime,
                es_hijo_directo=es_hijo,
                mismo_pgrp=mismo_pgrp,
                en_cgroup=en_cg,
                proc_root=self._proc_root,
                pidfd_opener=self._pidfd_opener,
            )
            if handle is not None:
                reemplazar_fd: Optional[RunnerHandle] = None
                with self._lock:
                    previo = self._runners.get(pid)
                    if previo is not None and previo.starttime != handle.starttime:
                        reemplazar_fd = self._runners.pop(pid)
                    if pid not in self._runners:
                        self._runners[pid] = handle
                    else:
                        reemplazar_fd = handle
                if reemplazar_fd is not None:
                    reemplazar_fd.cerrar_fd()

        with self._lock:
            return dict(self._runners)

    def _fijar_telemetria_nvidia(self, estado: str, detalle: str) -> str:
        with self._lock:
            self._nvidia_telemetria_estado = estado
            self._nvidia_telemetria_detalle = detalle
        return estado

    def verificar_nvidia_smi_sin_procesos(self) -> str:
        """Verifica en nvidia-smi que ningún PID conocido use la GPU.

        Retorna:
            - "SIN_PROCESOS_GPU" si nvidia-smi respondió con éxito, salida válida y
              ningún PID propio figura en GPU.
            - "DESCONOCIDA" si el binario no existe, falla o entrega salida inválida.
        Lanza LLMProviderError si detecta un PID propio usando la GPU ("VIOLACION_GPU").
        """
        try:
            res = self._nvidia_smi_runner(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,process_name,used_memory",
                    "--format=csv,noheader",
                ],
                timeout=3.0,
            )
        except FileNotFoundError:
            return self._fijar_telemetria_nvidia("DESCONOCIDA", "Binario nvidia-smi no encontrado.")
        except Exception as exc:
            return self._fijar_telemetria_nvidia("DESCONOCIDA", f"Error ejecutando nvidia-smi: {exc}")

        if res.returncode != 0:
            return self._fijar_telemetria_nvidia(
                "DESCONOCIDA",
                f"nvidia-smi falló con rc={res.returncode}: {res.stderr.strip()}",
            )

        with self._lock:
            pids_conocidos = set(self._runners.keys())
            if self._main_pid is not None:
                pids_conocidos.add(self._main_pid)

        linea_invalida: Optional[str] = None
        for linea in res.stdout.splitlines():
            limpia = linea.strip()
            if not limpia:
                continue
            campos = [c.strip() for c in limpia.split(",")]
            if len(campos) < 3 or not campos[0].isdigit() or not campos[1]:
                if linea_invalida is None:
                    linea_invalida = limpia
                continue
            pid_gpu = int(campos[0])
            if pid_gpu in pids_conocidos:
                self._fijar_telemetria_nvidia(
                    "VIOLACION_GPU",
                    f"PID propio {pid_gpu} detectado en nvidia-smi ({limpia}).",
                )
                raise LLMProviderError(
                    f"Violación de aislamiento GPU: PID propio {pid_gpu} detectado en nvidia-smi.",
                    provider="ollama",
                )

        if linea_invalida is not None:
            return self._fijar_telemetria_nvidia(
                "DESCONOCIDA",
                f"Salida inválida de nvidia-smi: {linea_invalida!r}",
            )

        return self._fijar_telemetria_nvidia("SIN_PROCESOS_GPU", "nvidia-smi sin procesos propios en GPU.")

    def _evaluar_limpieza_actual(self) -> tuple[bool, bool, list[int], bool, bool, Optional[str], Optional[str]]:
        """Evalúa estado de cgroup, pidfds, socket y unidad; libera solo pidfds confirmados MUERTO."""
        with self._lock:
            unit_name = self._unit_name
            control_group = self._control_group
            inodo_orig = self._inodo_socket_original
            ultimo_inodo = self._ultimo_inodo_socket
            runners_snapshot = list(self._runners.items())

        ruta_cg_abs = (
            os.path.join(self._cgroup_root, control_group.lstrip("/"))
            if control_group
            else None
        )
        cgroup_vacio = ruta_cg_abs is None or cgroup_esta_vacio_o_eliminado(ruta_cg_abs)

        runners_no_confirmados: list[int] = []
        runners_muertos: list[int] = []
        for pid, handle in runners_snapshot:
            if handle.pidfd is not None:
                estado_proc = _ping_pidfd_vivo(handle.pidfd)
            else:
                st = parsear_stat_proc(pid, proc_root=self._proc_root)
                if st is None or st.starttime != handle.starttime or st.state == "Z":
                    estado_proc = EstadoProceso.MUERTO
                else:
                    estado_proc = EstadoProceso.VIVO

            if estado_proc == EstadoProceso.MUERTO:
                runners_muertos.append(pid)
            else:
                runners_no_confirmados.append(pid)

        # Cerrar y descartar únicamente los pidfds cuya terminación quedó confirmada
        handles_a_cerrar: list[RunnerHandle] = []
        with self._lock:
            for pid in runners_muertos:
                h = self._runners.pop(pid, None)
                if h is not None:
                    handles_a_cerrar.append(h)
        for h in handles_a_cerrar:
            h.cerrar_fd()

        socket_cerrado = True
        for inodo_vigilado in {i for i in (inodo_orig, ultimo_inodo) if i is not None}:
            if inodo_socket_sigue_escuchando(
                inodo_vigilado,
                self._port,
                proc_root=self._proc_root,
            ):
                socket_cerrado = False
                break

        unidad_detenida = False
        if unit_name is not None:
            try:
                props = self._consultar_unidad_systemd(unit_name)
                load_state = props.get("LoadState", "")
                active_state = props.get("ActiveState", "")
                sub_state = props.get("SubState", "")
                unidad_detenida = load_state == "not-found" or (
                    active_state in ("inactive", "failed") and sub_state == "dead"
                )
            except Exception:
                unidad_detenida = False
        else:
            unidad_detenida = True

        return (
            cgroup_vacio,
            not runners_no_confirmados,
            runners_no_confirmados,
            socket_cerrado,
            unidad_detenida,
            unit_name,
            control_group,
        )

    def detener(self) -> bool:
        """Detiene la unidad transitoria y verifica cgroup vacío, pidfds muertos y socket cerrado.

        Si persisten runners vivos/inciertos, conserva sus pidfds para seguimiento posterior,
        marca DEGRADADO y retorna False.
        """
        if not self._modo_gestionado:
            return True

        with self._shutdown_lock:
            return self._detener_serializado()

    def _detener_serializado(self) -> bool:
        """Ejecución serializada de detención y verificación bajo _shutdown_lock."""
        with self._lock:
            unit_name = self._unit_name
            control_group = self._control_group

        if unit_name is None:
            with self._lock:
                self._aislamiento_verificado = False
            self._limpiar_runners_fds()
            return True

        self.descubrir_runners()

        try:
            res = self._run_cmd(["systemctl", "--user", "stop", unit_name], timeout=self._stop_timeout_s)
            stop_ok = res.returncode == 0
        except Exception:
            stop_ok = False

        ruta_cg_abs = (
            os.path.join(self._cgroup_root, control_group.lstrip("/"))
            if control_group
            else None
        )
        deadline = time.monotonic() + self._stop_timeout_s
        while time.monotonic() < deadline:
            if ruta_cg_abs is None or cgroup_esta_vacio_o_eliminado(ruta_cg_abs):
                break
            time.sleep(0.05)

        (
            cgroup_vacio,
            todos_runners_muertos,
            runners_no_confirmados,
            socket_cerrado,
            unidad_detenida,
            _,
            _,
        ) = self._evaluar_limpieza_actual()

        limpieza_completa = (
            stop_ok
            and cgroup_vacio
            and todos_runners_muertos
            and socket_cerrado
            and unidad_detenida
        )

        with self._lock:
            self._aislamiento_verificado = False
            if limpieza_completa:
                self._unit_name = None
                self._control_group = None
                self._main_pid = None
                self._main_starttime = None
                self._inodo_socket_original = None
                self._ultimo_inodo_socket = None
                self._cerrado = True

        if not limpieza_completa:
            self._coordinator.marcar_degradado(
                f"Limpieza incompleta al detener '{unit_name}' "
                f"(stop_ok={stop_ok}, cgroup_vacio={cgroup_vacio}, "
                f"runners_no_confirmados={runners_no_confirmados}, "
                f"socket_cerrado={socket_cerrado}, unidad_detenida={unidad_detenida})."
            )
            return False

        return True

    def _revalidar_limpieza_serializada(self) -> bool:
        """Ejecuta comprobación real de limpieza residual bajo `_shutdown_lock`."""
        with self._lock:
            unit_name = self._unit_name

        (
            cgroup_vacio,
            todos_runners_muertos,
            runners_no_confirmados,
            socket_cerrado,
            unidad_detenida,
            _,
            _,
        ) = self._evaluar_limpieza_actual()

        limpieza_ok = cgroup_vacio and todos_runners_muertos and socket_cerrado and unidad_detenida
        if not limpieza_ok:
            self._coordinator.marcar_degradado(
                f"Revalidación fallida para '{unit_name}' "
                f"(cgroup_vacio={cgroup_vacio}, runners_no_confirmados={runners_no_confirmados}, "
                f"socket_cerrado={socket_cerrado}, unidad_detenida={unidad_detenida})."
            )
            return False

        with self._lock:
            self._aislamiento_verificado = False
            self._unit_name = None
            self._control_group = None
            self._main_pid = None
            self._main_starttime = None
            self._inodo_socket_original = None
            self._ultimo_inodo_socket = None
            self._cerrado = True
        return True

    def revalidar_limpieza_tras_detencion(self) -> bool:
        """Reevalúa limpieza tras una detención previa incompleta sin cambiar estado DEGRADADO."""
        if not self._modo_gestionado:
            return self._coordinator.esta_disponible()

        with self._shutdown_lock:
            return self._revalidar_limpieza_serializada()

    def recuperar_desde_degradado(
        self,
        *,
        permitir_tras_violacion_gpu: bool = False,
        verificador_operaciones: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Recupera explícita y atómicamente el modo gestionado desde DEGRADADO a DISPONIBLE."""
        if not self._modo_gestionado:
            return False

        with self._shutdown_lock:
            with self._lock:
                telemetria_gpu = self._nvidia_telemetria_estado

            if telemetria_gpu == "VIOLACION_GPU":
                if not permitir_tras_violacion_gpu:
                    return False
                try:
                    estado_gpu = self.verificar_nvidia_smi_sin_procesos()
                except LLMProviderError:
                    return False
                if estado_gpu != "SIN_PROCESOS_GPU":
                    return False

            if not self._revalidar_limpieza_serializada():
                return False

            with self._lock:
                sin_residuos = (
                    self._unit_name is None
                    and not self._runners
                    and self._control_group is None
                    and self._inodo_socket_original is None
                    and self._ultimo_inodo_socket is None
                    and not self._aislamiento_verificado
                )

            if not sin_residuos:
                return False

            return self._coordinator.recuperar_disponible(
                motivo="Recuperación explícita verificada en modo gestionado.",
                verificador_seguro=verificador_operaciones,
            )

    def verificar_propiedad_unidad_activa(self) -> tuple[bool, str]:
        """Verifica inequívocamente bajo `_shutdown_lock` si la unidad activa pertenece a Madeline."""
        if not self._modo_gestionado:
            return False, "Modo externo: prohibido detener o modificar unidades externas."

        with self._shutdown_lock:
            with self._lock:
                unit_name = self._unit_name
                control_group = self._control_group
                main_pid = self._main_pid
                main_starttime = self._main_starttime

            if unit_name is None:
                return False, "No existe unidad gestionada registrada."

            prefijo = f"madeline-ollama-{self._port}-"
            if not (unit_name.startswith(prefijo) and unit_name.endswith(".service")):
                return (
                    False,
                    f"Unidad '{unit_name}' no coincide con el prefijo gestionado '{prefijo}*.service'.",
                )

            if control_group is None or main_pid is None or main_starttime is None:
                return False, f"Identidad incompleta para verificar propiedad de '{unit_name}'."

            try:
                props = self._consultar_unidad_systemd(unit_name)
            except Exception as exc:
                return False, f"No se pudo consultar identidad systemd de '{unit_name}': {exc}"

            load_state = props.get("LoadState", "")
            active_state = props.get("ActiveState", "")
            sub_state = props.get("SubState", "")
            if load_state == "not-found" or (
                active_state in ("inactive", "failed") and sub_state == "dead"
            ):
                return True, f"Unidad propia '{unit_name}' ya inactiva/eliminada."

            cg_actual = props.get("ControlGroup", "")
            if cg_actual != control_group:
                return (
                    False,
                    f"ControlGroup de '{unit_name}' ({cg_actual!r}) difiere del registrado ({control_group!r}).",
                )

            try:
                pid_actual = int(props.get("MainPID", "0"))
            except ValueError:
                return False, f"MainPID inválido en '{unit_name}'."

            if pid_actual > 0:
                if pid_actual != main_pid:
                    return (
                        False,
                        f"MainPID de '{unit_name}' ({pid_actual}) difiere del registrado ({main_pid}).",
                    )
                st = parsear_stat_proc(pid_actual, proc_root=self._proc_root)
                if st is None or st.starttime != main_starttime:
                    return (
                        False,
                        f"starttime de PID {pid_actual} no coincide con la instancia registrada ({main_starttime}).",
                    )
                if not _verificar_proc_cgroup_pertenece(pid_actual, control_group, proc_root=self._proc_root):
                    return (
                        False,
                        f"PID {pid_actual} no pertenece al cgroup registrado '{control_group}'.",
                    )

            return True, f"Propiedad verificada para '{unit_name}' (PID={main_pid})."

    def cerrar(self) -> bool:
        """Cierre síncrono explícito para shutdown de la aplicación."""
        return self.detener()

    async def aclose(self) -> bool:
        """Cierre asíncrono explícito sin bloquear el event loop."""
        import asyncio

        return await asyncio.to_thread(self.detener)
