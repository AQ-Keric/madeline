"""Servicio de telemetría de hardware, sensores del sistema y control de energía."""

import subprocess
import threading
import time
from typing import Any, Dict, Optional, Tuple

# Estado interno de muestreo de CPU
_MUESTRA_CPU: Optional[Tuple[float, int, int]] = None  # (momento_muestra, total, inactivo)
_ULTIMO_USO_CPU: float = 0.0                        # Último porcentaje válido registrado
_LOCK_CPU = threading.Lock()

# Estado interno de caché de GPU NVIDIA
_CACHE_NVIDIA: Optional[Tuple[float, Optional[str], Optional[str]]] = None  # (timestamp, uso, temp)
_TTL_NVIDIA: float = 30.0  # segundos
_LOCK_NVIDIA = threading.Lock()


def _leer_proc_stat() -> Tuple[int, int]:
    """Lee /proc/stat y calcula tiempo total y tiempo inactivo (idle + iowait)."""
    with open("/proc/stat") as f:
        # Solo los 8 primeros campos: guest y guest_nice ya vienen
        # incluidos dentro de user y nice.
        valores = [int(x) for x in f.readline().split()[1:9]]
    # idle + iowait cuentan como tiempo inactivo
    return sum(valores), valores[3] + valores[4]


def uso_cpu() -> float:
    """% de uso de CPU desde la lectura anterior.

    Semántica:
    - Si transcurrieron < 0.25 s desde la última muestra, reutiliza _ULTIMO_USO_CPU.
    - Si transcurrieron entre 0.25 y 10.0 s, calcula el delta contra la muestra anterior.
    - Si no hay muestra previa o transcurrieron > 10.0 s, realiza medición en frío
      con un intervalo de 200 ms (time.sleep(0.2)).
    - Si delta_total <= 0 (en frío o en caliente), conserva _ULTIMO_USO_CPU y no
      sustituye _MUESTRA_CPU por una muestra inválida.
    """
    global _MUESTRA_CPU, _ULTIMO_USO_CPU

    with _LOCK_CPU:
        ahora = time.monotonic()

        if _MUESTRA_CPU is not None:
            momento_previo, total_previo, inactivo_previo = _MUESTRA_CPU
            delta_tiempo = ahora - momento_previo

            # Llamada inmediata (< 0.25 s): reutilizar último porcentaje válido
            if delta_tiempo < 0.25:
                return _ULTIMO_USO_CPU

            # Ventana caliente (0.25 s - 10.0 s): un único punto de lectura
            if delta_tiempo <= 10.0:
                total, inactivo = _leer_proc_stat()
                delta_total = total - total_previo
                if delta_total <= 0:
                    return _ULTIMO_USO_CPU

                inactivo_delta = inactivo - inactivo_previo
                nuevo_uso = round(100 * (1 - inactivo_delta / delta_total), 1)
                _MUESTRA_CPU = (ahora, total, inactivo)
                _ULTIMO_USO_CPU = nuevo_uso
                return nuevo_uso

        # Medición en frío: sin muestra previa o caducada (> 10.0 s)
        total_1, inactivo_1 = _leer_proc_stat()
        time.sleep(0.2)
        total_2, inactivo_2 = _leer_proc_stat()
        ahora_post = time.monotonic()

        delta_total = total_2 - total_1
        if delta_total <= 0:
            return _ULTIMO_USO_CPU

        inactivo_delta = inactivo_2 - inactivo_1
        nuevo_uso = round(100 * (1 - inactivo_delta / delta_total), 1)
        _MUESTRA_CPU = (ahora_post, total_2, inactivo_2)
        _ULTIMO_USO_CPU = nuevo_uso
        return nuevo_uso


def _leer_telemetria_nvidia() -> Tuple[Optional[str], Optional[str]]:
    """Consulta uso y temperatura de GPU NVIDIA bajo demanda con caché de 30 s.

    Aplica doble chequeo (double-checked locking) para garantizar que ante
    caducidad solo un hilo ejecute nvidia-smi. Retiene también resultados
    fallidos durante el TTL de 30 s para prevenir ráfagas de subprocesos.
    """
    global _CACHE_NVIDIA

    ahora = time.monotonic()

    # 1. Chequeo optimista sin lock
    if _CACHE_NVIDIA is not None and (ahora - _CACHE_NVIDIA[0]) < _TTL_NVIDIA:
        return _CACHE_NVIDIA[1], _CACHE_NVIDIA[2]

    # 2. Adquisición del lock exclusivo de NVIDIA
    with _LOCK_NVIDIA:
        ahora = time.monotonic()
        # 3. Doble comprobación dentro del lock
        if _CACHE_NVIDIA is not None and (ahora - _CACHE_NVIDIA[0]) < _TTL_NVIDIA:
            return _CACHE_NVIDIA[1], _CACHE_NVIDIA[2]

        try:
            gpu_info = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            ).stdout.strip()
            gpu_uso, gpu_temp = [x.strip() for x in gpu_info.split(",")]
            _CACHE_NVIDIA = (ahora, gpu_uso, gpu_temp)
        except Exception:
            # Retener resultado nulo durante el TTL para mitigar fallos repetitivos
            _CACHE_NVIDIA = (ahora, None, None)

        return _CACHE_NVIDIA[1], _CACHE_NVIDIA[2]


def leer_sensores() -> Dict[str, Any]:
    """RAM (MB), CPU (% y °C) y GPU NVIDIA (% y °C, None si está
    apagada por el sistema híbrido, no hay nvidia-smi o falla).

    Orden de telemetría:
    1. Memoria RAM (/proc/meminfo)
    2. Temperatura de CPU (/sys/class/thermal/thermal_zone0/temp)
    3. Porcentaje de uso de CPU (uso_cpu, con _LOCK_CPU)
    4. Telemetría de GPU NVIDIA (_leer_telemetria_nvidia, con _LOCK_NVIDIA)
    Los locks de CPU y NVIDIA nunca se anidan.
    """
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

    cpu_pct = uso_cpu()
    gpu_uso, gpu_temp = _leer_telemetria_nvidia()

    return {
        "ram_usada": ram_usada,
        "ram_total": ram_total,
        "cpu_uso": cpu_pct,
        "cpu_temp": cpu_temp,
        "gpu_uso": gpu_uso,
        "gpu_temp": gpu_temp,
    }


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
