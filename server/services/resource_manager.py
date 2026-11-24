"""Servicio de gobernanza de recursos y admisión para inferencias locales de Madeline.

Proporciona una política centralizada y no invasiva para:
1. Evaluar la admisión de nuevas inferencias locales (Groq fallback a Ollama).
2. Evaluar la continuidad o cancelación de inferencias locales en curso.

Principios rectores de diseño:
- Pasivo: NO modifica prioridades ni afinidades de procesos ajenos, NO mata procesos.
- Sin polling permanente ni demonios residentes: se evalúa bajo demanda.
- Conservador ante fallas: si una señal crítica no puede leerse, deniega admisión o cancela.
- Umbrales configurables con advertencia: son políticas provisionales de protección del
  sistema, no garantías matemáticas de rendimiento de hardware.
- No confunde ausencia de telemetría GPU con verificación de aislamiento (U-4.3B).
- Distingue capas de compatibilidad (Wine/Proton/Gamescope) de juegos reales para
  evitar falsos positivos en navegación web o software de productividad.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# Umbrales provisionales de política de admisión (configurables vía variables de entorno).
# IMPORTANTE: Estos valores representan políticas provisionales de seguridad del sistema
# operativo, no garantías de rendimiento de inferencia ni promesas absolutas.
DEFAULT_RAM_MINIMA_MB: int = int(os.getenv("MADELINE_RES_RAM_MINIMA_MB", "4500"))
DEFAULT_RAM_CRITICA_MB: int = int(os.getenv("MADELINE_RES_RAM_CRITICA_MB", "1500"))
DEFAULT_CPU_MAX_PCT: float = float(os.getenv("MADELINE_RES_CPU_MAX_PCT", "70.0"))
DEFAULT_CPU_CRITICA_PCT: float = float(os.getenv("MADELINE_RES_CPU_CRITICA_PCT", "95.0"))
DEFAULT_GPU_MAX_PCT: float = float(os.getenv("MADELINE_RES_GPU_MAX_PCT", "15.0"))
DEFAULT_GPU_CRITICA_PCT: float = float(os.getenv("MADELINE_RES_GPU_CRITICA_PCT", "50.0"))
DEFAULT_TTL_PROCESOS: float = float(os.getenv("MADELINE_RES_PROC_TTL", "1.5"))

# Clases de ventana de aplicaciones que NUNCA deben considerarse juegos por su título
CLASES_NO_JUEGO_DEFAULT: Set[str] = {
    "zen", "zen-browser", "firefox", "chrome", "chromium", "brave", "edge",
    "code", "vscodium", "sublime_text", "sublime", "kate", "gedit",
    "libreoffice", "soffice", "terminal", "kitty", "alacritty", "foot",
    "wezterm", "thunar", "nautilus", "dolphin", "pcmanfm",
}

# Clases de ventana asociadas a juegos reales (exactas en minúsculas)
CLASES_VENTANA_JUEGO_DEFAULT: Set[str] = {
    "celeste", "hades", "terraria", "projectzomboid",
    "pokemmo", "sober", "prismlauncher",
    "battlefield 4", "bf4", "outlast trials",
    "guild wars 2", "gw2", "gravity circuit", "hacknet",
    "katana zero", "ultimate chicken horse",
    "retroarch", "rpcs3", "yuzu", "ryujinx", "pcsx2", "dolphin-emu", "dosbox",
}

# Patrones regex inequívocos para clases de ventana de juegos (ej. steam_app_\d+)
PATRONES_CLASE_JUEGO_DEFAULT: List[re.Pattern] = [
    re.compile(r"^steam_app_\d+$", re.IGNORECASE),
]

# Títulos inequívocos de juegos (con límites de palabra).
# NO deben incluirse palabras comunes aisladas como 'wine', 'sober', 'hades', 'celeste'.
TITULOS_JUEGOS_INEQUIVOCOS_DEFAULT: List[re.Pattern] = [
    re.compile(r"\bbattlefield\s+4\b", re.IGNORECASE),
    re.compile(r"\bproject\s+zomboid\b", re.IGNORECASE),
    re.compile(r"\bguild\s+wars\s+2\b", re.IGNORECASE),
    re.compile(r"\bkatana\s+zero\b", re.IGNORECASE),
    re.compile(r"\bultimate\s+chicken\s+horse\b", re.IGNORECASE),
    re.compile(r"\bgravity\s+circuit\b", re.IGNORECASE),
    re.compile(r"\bhacknet\b", re.IGNORECASE),
    re.compile(r"\boutlast\s+trials\b", re.IGNORECASE),
    re.compile(r"\bhow\s+to\s+fish\b", re.IGNORECASE),
]

# Nombres de ejecutables (/proc/comm) que corresponden estrictamente a juegos.
# NOTA: wine, wine64, wineserver, proton y gamescope NO son juegos por sí solos.
PROCESOS_JUEGOS_DEFAULT: Set[str] = {
    "battlefield", "bf4", "celeste", "gravitycircuit", "gw2", "guildwars2",
    "hacknet", "hades", "how to fish", "katanazero", "projectzomboid",
    "terraria", "outlast", "pokemmo", "sober", "prismlauncher",
    "retroarch", "rpcs3", "yuzu", "ryujinx", "pcsx2", "dolphin-emu", "dosbox",
}

# Nombres de perfiles que denotan actividad de juego
PERFILES_JUEGO_DEFAULT: Set[str] = {
    "juego", "modo juego", "modo_juego", "perfil_juego", "perfil_mmo", "game", "gaming"
}


class MotivoDecision(str, Enum):
    """Motivos estandarizados e identificables para las decisiones del ResourceManager."""
    OK = "OK"
    FALLBACK_DESHABILITADO = "FALLBACK_DESHABILITADO"
    PERFIL_JUEGO = "PERFIL_JUEGO"
    JUEGO_EN_EJECUCION = "JUEGO_EN_EJECUCION"
    MEMORIA_INSUFICIENTE = "MEMORIA_INSUFICIENTE"
    MEMORIA_CRITICA = "MEMORIA_CRITICA"
    CPU_SATURADA = "CPU_SATURADA"
    CPU_CRITICA = "CPU_CRITICA"
    GPU_SATURADA = "GPU_SATURADA"
    ERROR_SENSORES = "ERROR_SENSORES"
    ERROR_TELEMETRIA = "ERROR_TELEMETRIA"


@dataclass(frozen=True)
class MetricasRecursos:
    """Instantánea de telemetría y estado utilizada para tomar la decisión."""
    ram_disponible_mb: Optional[int] = None
    ram_total_mb: Optional[int] = None
    cpu_uso_pct: Optional[float] = None
    gpu_uso_pct: Optional[float] = None
    gpu_estado: str = "DESCONOCIDO"  # "DESCONOCIDO", "DISPONIBLE", "ERROR"
    perfil_activo: str = "DEFAULT"
    juego_detectado: Optional[str] = None
    ventana_activa: Optional[str] = None
    procesos_desde_cache: bool = False


@dataclass(frozen=True)
class DecisionAdmision:
    """Resultado formal de la evaluación de admisión para una nueva inferencia local."""
    admitida: bool
    motivo: MotivoDecision
    detalle: str
    metricas: MetricasRecursos


@dataclass(frozen=True)
class DecisionContinuidad:
    """Resultado formal de la evaluación de continuidad para una inferencia en curso."""
    continuar: bool
    motivo: MotivoDecision
    detalle: str
    metricas: MetricasRecursos

    @property
    def debe_cancelar(self) -> bool:
        """True si la inferencia en curso debe interrumpirse inmediatamente."""
        return not self.continuar


def _escanear_proc_con_cortocircuito(procesos_juego: Set[str]) -> Optional[str]:
    """Escanea /proc con cortocircuito: se detiene al encontrar el primer proceso de juego.

    Maneja de forma conservadora procesos efímeros (FileNotFoundError, ProcessLookupError)
    y restricciones de permisos sin arrojar excepciones.
    """
    try:
        for entrada in os.listdir("/proc"):
            if entrada.isdigit():
                try:
                    ruta_comm = os.path.join("/proc", entrada, "comm")
                    with open(ruta_comm, "r", encoding="utf-8", errors="ignore") as f:
                        comm = f.read().strip().lower()
                        if comm and comm in procesos_juego:
                            return comm  # Cortocircuito inmediato
                except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
                    continue
    except Exception:
        pass
    return None


class ResourceManager:
    """Gobernador pasivo de hardware y compuerta de admisión para inferencias locales."""

    def __init__(
        self,
        fallback_habilitado: Optional[bool] = None,
        perfil_inicial: Optional[str] = None,
        ram_minima_mb: Optional[int] = None,
        ram_critica_mb: Optional[int] = None,
        cpu_max_pct: Optional[float] = None,
        cpu_critica_pct: Optional[float] = None,
        gpu_max_pct: Optional[float] = None,
        gpu_critica_pct: Optional[float] = None,
        ttl_procesos_segundos: Optional[float] = None,
        clases_no_juego: Optional[Set[str]] = None,
        clases_juego: Optional[Set[str]] = None,
        titulos_juego: Optional[List[re.Pattern]] = None,
        procesos_juego: Optional[Set[str]] = None,
        lector_sensores: Optional[Callable[[], Dict[str, Any]]] = None,
        lector_ventana_activa: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        lector_clientes: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        lector_procesos: Optional[Callable[[], Optional[str]]] = None,
    ):
        self._lock = threading.Lock()

        # Configuración de estado
        if fallback_habilitado is not None:
            self._fallback_habilitado = fallback_habilitado
        else:
            self._fallback_habilitado = (
                os.getenv("MADELINE_FALLBACK_LOCAL_HABILITADO", "false").lower()
                in ("true", "1", "yes")
            )

        self._perfil_activo: str = (
            perfil_inicial
            if perfil_inicial is not None
            else os.getenv("MADELINE_PERFIL_ACTIVO", "DEFAULT")
        )

        # Umbrales provisionales de política
        self.ram_minima_mb = ram_minima_mb if ram_minima_mb is not None else DEFAULT_RAM_MINIMA_MB
        self.ram_critica_mb = ram_critica_mb if ram_critica_mb is not None else DEFAULT_RAM_CRITICA_MB
        self.cpu_max_pct = cpu_max_pct if cpu_max_pct is not None else DEFAULT_CPU_MAX_PCT
        self.cpu_critica_pct = cpu_critica_pct if cpu_critica_pct is not None else DEFAULT_CPU_CRITICA_PCT
        self.gpu_max_pct = gpu_max_pct if gpu_max_pct is not None else DEFAULT_GPU_MAX_PCT
        self.gpu_critica_pct = gpu_critica_pct if gpu_critica_pct is not None else DEFAULT_GPU_CRITICA_PCT
        self.ttl_procesos_segundos = (
            ttl_procesos_segundos if ttl_procesos_segundos is not None else DEFAULT_TTL_PROCESOS
        )

        # Catálogo discriminado de juegos
        self._clases_no_juego: Set[str] = (
            set(clases_no_juego) if clases_no_juego is not None else set(CLASES_NO_JUEGO_DEFAULT)
        )
        self._clases_juego: Set[str] = (
            set(clases_juego) if clases_juego is not None else set(CLASES_VENTANA_JUEGO_DEFAULT)
        )
        self._patrones_clase: List[re.Pattern] = list(PATRONES_CLASE_JUEGO_DEFAULT)
        self._titulos_juego: List[re.Pattern] = (
            list(titulos_juego) if titulos_juego is not None else list(TITULOS_JUEGOS_INEQUIVOCOS_DEFAULT)
        )
        self._procesos_juego: Set[str] = (
            set(procesos_juego) if procesos_juego is not None else set(PROCESOS_JUEGOS_DEFAULT)
        )

        # Estado de caché para lectura de procesos /proc
        # (monotonic_timestamp, Optional[juego_detectado])
        self._cache_proc: Optional[Tuple[float, Optional[str]]] = None

        # Inyección de lectores (con carga diferida para evitar llamadas síncronas en import)
        if lector_sensores is None:
            def _lector_sensores_default() -> Dict[str, Any]:
                try:
                    from server.services.system import leer_sensores
                    return leer_sensores()
                except ImportError:
                    return {}
            self._lector_sensores = _lector_sensores_default
        else:
            self._lector_sensores = lector_sensores

        if lector_ventana_activa is None:
            def _lector_ventana_default() -> Optional[Dict[str, Any]]:
                try:
                    from server.services.hyprland import obtener_ventana_activa
                    return obtener_ventana_activa()
                except ImportError:
                    return None
            self._lector_ventana_activa = _lector_ventana_default
        else:
            self._lector_ventana_activa = lector_ventana_activa

        if lector_clientes is None:
            def _lector_clientes_default() -> List[Dict[str, Any]]:
                try:
                    from server.services.hyprland import obtener_clientes
                    return obtener_clientes()
                except ImportError:
                    return []
            self._lector_clientes = _lector_clientes_default
        else:
            self._lector_clientes = lector_clientes

        if lector_procesos is None:
            self._lector_procesos = lambda: _escanear_proc_con_cortocircuito(self._procesos_juego)
        else:
            self._lector_procesos = lector_procesos

    # ------------------------------------------------------------------
    # Gestión de estado (Thread-Safe)
    # ------------------------------------------------------------------

    def fijar_fallback_habilitado(self, habilitado: bool) -> None:
        """Habilita o deshabilita dinámicamente el fallback local."""
        with self._lock:
            self._fallback_habilitado = bool(habilitado)

    def es_fallback_habilitado(self) -> bool:
        """True si el fallback local a Ollama está permitido por configuración."""
        with self._lock:
            return self._fallback_habilitado

    def fijar_perfil(self, perfil: str) -> None:
        """Actualiza el perfil operativo activo de Madeline."""
        with self._lock:
            self._perfil_activo = str(perfil).strip()

    def obtener_perfil(self) -> str:
        """Devuelve el perfil operativo activo de Madeline."""
        with self._lock:
            return self._perfil_activo

    def es_perfil_juego(self, perfil: Optional[str] = None) -> bool:
        """Determina si un perfil dado (o el activo) corresponde a modo juego."""
        p = (perfil if perfil is not None else self.obtener_perfil()).strip().lower()
        if p in PERFILES_JUEGO_DEFAULT:
            return True
        return "juego" in p or "gaming" in p

    # ------------------------------------------------------------------
    # Detección de juegos (Ventanas, Clientes y Procesos con Amortiguación)
    # ------------------------------------------------------------------

    def _coincide_firma_ventana(self, clase: str, titulo: str) -> Optional[str]:
        r"""Comprueba si una ventana corresponde a un juego, evitando falsos positivos.

        Reglas:
        1. Si la clase pertenece a navegadores, editores o herramientas de trabajo
           (ej. Zen, Firefox, Code), se omite la evaluación de títulos para evitar
           bloquear al usuario cuando navega por artículos o documentación.
        2. Clases inequívocas de juegos (steam_app_\d+ o nombres directos de juegos)
           se identifican directamente.
        3. Títulos inequívocos se comprueban con límites de palabra (\b) solo en ventanas
           que no pertenezcan a la lista de exclusión de aplicaciones comunes.
        """
        clase_limpia = clase.strip().lower()
        titulo_limpio = titulo.strip()

        # 1. Protección contra falsos positivos en navegadores y editores
        if clase_limpia in self._clases_no_juego:
            return None

        # 2. Comprobar patrones de clase de juego (ej. steam_app_\d+)
        for patron in self._patrones_clase:
            if patron.search(clase_limpia):
                return f"clase_patron:{clase_limpia}"

        # 3. Comprobar clases de juego exactas
        if clase_limpia in self._clases_juego:
            return f"clase_juego:{clase_limpia}"

        # 4. Comprobar títulos inequívocos con límites de palabra (\b)
        for patron_tit in self._titulos_juego:
            if patron_tit.search(titulo_limpio):
                return f"titulo_inequivoco:{patron_tit.pattern}"

        return None

    def detectar_proceso_juego(self, forzar_refresco: bool = False) -> Tuple[Optional[str], bool]:
        """Detecta si hay un proceso de juego en /proc, utilizando una caché con TTL corto.

        Devuelve:
            (nombre_juego_o_None, desde_cache_bool)
        """
        ahora = time.monotonic()
        with self._lock:
            if not forzar_refresco and self._cache_proc is not None:
                ts, juego_cached = self._cache_proc
                if (ahora - ts) < self.ttl_procesos_segundos:
                    return juego_cached, True

        # Escaneo con cortocircuito (fuera del lock para no bloquear el recurso)
        juego = self._lector_procesos()

        with self._lock:
            self._cache_proc = (ahora, juego)

        return juego, False

    def detectar_juego_activo(self, forzar_refresco: bool = False) -> Tuple[Optional[str], bool]:
        """Detecta si hay un juego en ejecución a través de tres fuentes:
        1. Ventana con foco activo (señal complementaria rápida).
        2. Lista completa de clientes/ventanas de Hyprland (juegos en segundo plano).
        3. Procesos del sistema operativo vía /proc (con cortocircuito y caché corta).

        Devuelve:
            (identificador_juego_o_None, procesos_desde_cache_bool)
        """
        # 1. Ventana activa (foco en primer plano)
        try:
            activa = self._lector_ventana_activa()
            if activa and isinstance(activa, dict):
                clase = str(activa.get("class", ""))
                titulo = str(activa.get("title", ""))
                coincidencia = self._coincide_firma_ventana(clase, titulo)
                if coincidencia:
                    return f"ventana_activa:{coincidencia}", False
        except Exception:
            pass

        # 2. Clientes Hyprland en segundo plano
        try:
            clientes = self._lector_clientes()
            if clientes and isinstance(clientes, list):
                for c in clientes:
                    if isinstance(c, dict):
                        clase = str(c.get("class", ""))
                        titulo = str(c.get("title", ""))
                        coincidencia = self._coincide_firma_ventana(clase, titulo)
                        if coincidencia:
                            return f"cliente_hyprland:{coincidencia}", False
        except Exception:
            pass

        # 3. Procesos del sistema (/proc con cortocircuito y caché)
        try:
            juego_proc, desde_cache = self.detectar_proceso_juego(forzar_refresco=forzar_refresco)
            if juego_proc:
                return f"proceso:{juego_proc}", desde_cache
            return None, desde_cache
        except Exception:
            pass

        return None, False

    # ------------------------------------------------------------------
    # Lectura y normalización de sensores
    # ------------------------------------------------------------------

    def _leer_metricas_sensores(
        self,
    ) -> Tuple[Optional[int], Optional[int], Optional[float], Optional[float], str, Optional[str]]:
        """Lee y normaliza RAM disponible (MB), RAM total (MB), CPU (%) y GPU (%).

        Devuelve:
            (ram_disp_mb, ram_tot_mb, cpu_pct, gpu_pct, gpu_estado, error_str)
            donde gpu_estado puede ser 'DESCONOCIDO', 'DISPONIBLE' o 'ERROR'.
        """
        try:
            sensores = self._lector_sensores()
            if not isinstance(sensores, dict):
                return None, None, None, None, "ERROR", "Formato de sensores inválido (no dict)"

            # 1. RAM: disponible directa o total - usada
            ram_total = sensores.get("ram_total")
            ram_usada = sensores.get("ram_usada")
            ram_disponible = sensores.get("ram_disponible")

            if ram_disponible is not None:
                ram_disp_mb = int(ram_disponible)
                ram_tot_mb = int(ram_total) if ram_total is not None else None
            elif ram_total is not None and ram_usada is not None:
                ram_tot_mb = int(ram_total)
                ram_disp_mb = ram_tot_mb - int(ram_usada)
            else:
                return None, None, None, None, "ERROR", "Datos de memoria RAM ausentes en sensores"

            # 2. CPU
            cpu_raw = sensores.get("cpu_uso")
            if cpu_raw is None:
                return None, None, None, None, "ERROR", "Dato de uso de CPU ausente en sensores"
            cpu_pct = float(cpu_raw)

            # 3. GPU (Señal opcional: None si GPU apagada, suspendida o sin nvidia-smi)
            gpu_pct: Optional[float] = None
            gpu_estado: str = "DESCONOCIDO"
            gpu_raw = sensores.get("gpu_uso")

            if gpu_raw is not None:
                try:
                    gpu_str = str(gpu_raw).replace("%", "").strip()
                    if gpu_str and gpu_str.lower() != "none":
                        gpu_pct = float(gpu_str)
                        gpu_estado = "DISPONIBLE"
                except (ValueError, TypeError):
                    return (
                        ram_disp_mb,
                        ram_tot_mb,
                        cpu_pct,
                        None,
                        "ERROR",
                        f"Telemetría GPU corrupta o no interpretable: {gpu_raw}",
                    )

            return ram_disp_mb, ram_tot_mb, cpu_pct, gpu_pct, gpu_estado, None

        except Exception as e:
            return None, None, None, None, "ERROR", f"Error al consultar sensores del sistema: {e}"

    def _obtener_identificador_ventana_activa(self) -> Optional[str]:
        try:
            activa = self._lector_ventana_activa()
            if activa and isinstance(activa, dict):
                return str(activa.get("class") or activa.get("title") or "") or None
        except Exception:
            pass
        return None

    def _construir_metricas_parciales(
        self,
        perfil: Optional[str] = None,
        juego: Optional[str] = None,
        desde_cache: bool = False,
    ) -> MetricasRecursos:
        return MetricasRecursos(
            perfil_activo=perfil if perfil is not None else self.obtener_perfil(),
            juego_detectado=juego,
            ventana_activa=self._obtener_identificador_ventana_activa(),
            procesos_desde_cache=desde_cache,
        )

    # ------------------------------------------------------------------
    # Evaluación de Admisión (Nuevas Operaciones)
    # ------------------------------------------------------------------

    def evaluar_admision(self, forzar_refresco: bool = False) -> DecisionAdmision:
        """Evalúa de forma no invasiva si Madeline puede admitir una nueva inferencia local."""
        # 1. Bandera de habilitación de fallback local
        if not self.es_fallback_habilitado():
            return DecisionAdmision(
                admitida=False,
                motivo=MotivoDecision.FALLBACK_DESHABILITADO,
                detalle="El fallback local a Ollama se encuentra deshabilitado por configuración.",
                metricas=self._construir_metricas_parciales(),
            )

        # 2. Perfil activo (especialmente JUEGO)
        perfil = self.obtener_perfil()
        if self.es_perfil_juego(perfil):
            return DecisionAdmision(
                admitida=False,
                motivo=MotivoDecision.PERFIL_JUEGO,
                detalle=f"Admisión denegada: perfil de juego activo ('{perfil}').",
                metricas=self._construir_metricas_parciales(perfil=perfil),
            )

        # 3. Detección de juegos en ejecución (ventana activa, clientes Hyprland o /proc)
        juego_detectado, desde_cache = self.detectar_juego_activo(forzar_refresco=forzar_refresco)
        if juego_detectado:
            return DecisionAdmision(
                admitida=False,
                motivo=MotivoDecision.JUEGO_EN_EJECUCION,
                detalle=f"Admisión denegada: juego detectado en ejecución ({juego_detectado}).",
                metricas=self._construir_metricas_parciales(
                    perfil=perfil, juego=juego_detectado, desde_cache=desde_cache
                ),
            )

        # 4. Lectura de sensores (RAM, CPU, GPU)
        ram_disp_mb, ram_tot_mb, cpu_pct, gpu_pct, gpu_estado, error_sensores = (
            self._leer_metricas_sensores()
        )
        if error_sensores:
            motivo = (
                MotivoDecision.ERROR_TELEMETRIA
                if "GPU" in error_sensores
                else MotivoDecision.ERROR_SENSORES
            )
            return DecisionAdmision(
                admitida=False,
                motivo=motivo,
                detalle=f"Admisión denegada de forma conservadora: {error_sensores}.",
                metricas=self._construir_metricas_parciales(perfil=perfil, desde_cache=desde_cache),
            )

        metricas = MetricasRecursos(
            ram_disponible_mb=ram_disp_mb,
            ram_total_mb=ram_tot_mb,
            cpu_uso_pct=cpu_pct,
            gpu_uso_pct=gpu_pct,
            gpu_estado=gpu_estado,
            perfil_activo=perfil,
            juego_detectado=None,
            ventana_activa=self._obtener_identificador_ventana_activa(),
            procesos_desde_cache=desde_cache,
        )

        # 5. Memoria RAM disponible para admisión (Umbral provisional: >= 4500 MB)
        if ram_disp_mb < self.ram_minima_mb:
            return DecisionAdmision(
                admitida=False,
                motivo=MotivoDecision.MEMORIA_INSUFICIENTE,
                detalle=(
                    f"Admisión denegada: Memoria disponible ({ram_disp_mb} MB) inferior "
                    f"al umbral de seguridad ({self.ram_minima_mb} MB)."
                ),
                metricas=metricas,
            )

        # 6. Uso de CPU para admisión (Umbral provisional: <= 70.0%)
        if cpu_pct > self.cpu_max_pct:
            return DecisionAdmision(
                admitida=False,
                motivo=MotivoDecision.CPU_SATURADA,
                detalle=(
                    f"Admisión denegada: Uso de CPU ({cpu_pct}%) superior "
                    f"al límite de admisión ({self.cpu_max_pct}%)."
                ),
                metricas=metricas,
            )

        # 7. Telemetría GPU (cuando está disponible y es numérica) (Umbral provisional: <= 15.0%)
        if gpu_pct is not None and gpu_pct > self.gpu_max_pct:
            return DecisionAdmision(
                admitida=False,
                motivo=MotivoDecision.GPU_SATURADA,
                detalle=(
                    f"Admisión denegada: Uso de GPU activa ({gpu_pct}%) superior "
                    f"al límite de admisión ({self.gpu_max_pct}%)."
                ),
                metricas=metricas,
            )

        # Detalle transparente respecto al estado de la GPU
        if gpu_pct is None:
            detalle_gpu = (
                "Telemetría GPU no disponible o desconocida (no evaluada como libre ni aislada; "
                "la verificación de aislamiento corresponde a U-4.3B)."
            )
            detalle = f"Recursos suficientes de CPU y RAM. {detalle_gpu}"
        else:
            detalle = "Recursos suficientes de CPU, RAM y GPU dentro de los límites admisibles."

        return DecisionAdmision(
            admitida=True,
            motivo=MotivoDecision.OK,
            detalle=detalle,
            metricas=metricas,
        )

    # ------------------------------------------------------------------
    # Evaluación de Continuidad (Operaciones en Curso)
    # ------------------------------------------------------------------

    def evaluar_continuidad(self, forzar_refresco: bool = False) -> DecisionContinuidad:
        """Evalúa si una inferencia local en curso debe continuar o cancelarse inmediatamente.

        NOTA DE DISEÑO: La evaluación de procesos en /proc utiliza una caché con TTL corto
        (1.5 s) para amortiguar la latencia de I/O en bucles de streaming. Las comprobaciones
        de ventana activa y clientes Hyprland se evalúan en tiempo real en cada llamada.
        """
        # 1. Bandera de fallback deshabilitada durante la operación
        if not self.es_fallback_habilitado():
            return DecisionContinuidad(
                continuar=False,
                motivo=MotivoDecision.FALLBACK_DESHABILITADO,
                detalle="Cancelación: Fallback local desactivado por configuración durante la inferencia.",
                metricas=self._construir_metricas_parciales(),
            )

        # 2. Perfil cambia a JUEGO durante la operación
        perfil = self.obtener_perfil()
        if self.es_perfil_juego(perfil):
            return DecisionContinuidad(
                continuar=False,
                motivo=MotivoDecision.PERFIL_JUEGO,
                detalle=f"Cancelación: Perfil de juego activado ('{perfil}') durante la inferencia.",
                metricas=self._construir_metricas_parciales(perfil=perfil),
            )

        # 3. Detección de juego que inició o tomó foco durante la operación
        juego_detectado, desde_cache = self.detectar_juego_activo(forzar_refresco=forzar_refresco)
        if juego_detectado:
            return DecisionContinuidad(
                continuar=False,
                motivo=MotivoDecision.JUEGO_EN_EJECUCION,
                detalle=f"Cancelación: Se detectó juego en ejecución ({juego_detectado}) durante la inferencia.",
                metricas=self._construir_metricas_parciales(
                    perfil=perfil, juego=juego_detectado, desde_cache=desde_cache
                ),
            )

        # 4. Lectura de sensores
        ram_disp_mb, ram_tot_mb, cpu_pct, gpu_pct, gpu_estado, error_sensores = (
            self._leer_metricas_sensores()
        )
        if error_sensores:
            motivo = (
                MotivoDecision.ERROR_TELEMETRIA
                if "GPU" in error_sensores
                else MotivoDecision.ERROR_SENSORES
            )
            return DecisionContinuidad(
                continuar=False,
                motivo=motivo,
                detalle=f"Cancelación conservadora ante error de sensores: {error_sensores}.",
                metricas=self._construir_metricas_parciales(perfil=perfil, desde_cache=desde_cache),
            )

        metricas = MetricasRecursos(
            ram_disponible_mb=ram_disp_mb,
            ram_total_mb=ram_tot_mb,
            cpu_uso_pct=cpu_pct,
            gpu_uso_pct=gpu_pct,
            gpu_estado=gpu_estado,
            perfil_activo=perfil,
            juego_detectado=None,
            ventana_activa=self._obtener_identificador_ventana_activa(),
            procesos_desde_cache=desde_cache,
        )

        # 5. Memoria RAM cae por debajo del umbral crítico (evitar swap thrashing: < 1500 MB)
        if ram_disp_mb < self.ram_critica_mb:
            return DecisionContinuidad(
                continuar=False,
                motivo=MotivoDecision.MEMORIA_CRITICA,
                detalle=(
                    f"Cancelación crítica: Memoria disponible ({ram_disp_mb} MB) por debajo "
                    f"del umbral de seguridad del sistema ({self.ram_critica_mb} MB)."
                ),
                metricas=metricas,
            )

        # 6. Saturación crítica de CPU (saturación sostenida: > 95.0%)
        if cpu_pct > self.cpu_critica_pct:
            return DecisionContinuidad(
                continuar=False,
                motivo=MotivoDecision.CPU_CRITICA,
                detalle=(
                    f"Cancelación crítica: CPU saturada ({cpu_pct}%) por encima "
                    f"del límite crítico ({self.cpu_critica_pct}%)."
                ),
                metricas=metricas,
            )

        # 7. Saturación crítica de GPU (si está activa y > 50.0%)
        if gpu_pct is not None and gpu_pct > self.gpu_critica_pct:
            return DecisionContinuidad(
                continuar=False,
                motivo=MotivoDecision.GPU_SATURADA,
                detalle=(
                    f"Cancelación crítica: GPU activa saturada ({gpu_pct}%) por encima "
                    f"del límite crítico ({self.gpu_critica_pct}%)."
                ),
                metricas=metricas,
            )

        # Detalle transparente de continuidad
        if gpu_pct is None:
            detalle_gpu = "Telemetría GPU no disponible (aislamiento delegado a U-4.3B)."
            detalle = f"Condiciones operativas estables de CPU y RAM. {detalle_gpu}"
        else:
            detalle = "Condiciones operativas estables; la inferencia puede continuar."

        return DecisionContinuidad(
            continuar=True,
            motivo=MotivoDecision.OK,
            detalle=detalle,
            metricas=metricas,
        )


# Instancia y métodos globales
_instancia_global: Optional[ResourceManager] = None
_lock_global = threading.Lock()


def obtener_resource_manager() -> ResourceManager:
    """Obtiene la instancia global o crea una por defecto."""
    global _instancia_global
    with _lock_global:
        if _instancia_global is None:
            _instancia_global = ResourceManager()
        return _instancia_global


def fijar_resource_manager(rm: Optional[ResourceManager]) -> None:
    """Fija o restablece la instancia global (útil para inyección y tests)."""
    global _instancia_global
    with _lock_global:
        _instancia_global = rm
