"""
Servicio centralizado de Síntesis de Voz (TTS) y reproducción de audio de Madeline.

Responsabilidades:
- Síntesis de voz con edge-tts y fallback local (piper-tts / espeak-ng).
- Aplicación de filtros/efectos de audio con ffmpeg.
- Reproducción local segura y no bloqueante mediante mpv con Popen.
- Coordinación de terminación con escalamiento SIGTERM -> SIGKILL y confirmación de muerte.
- Gestión segura del ciclo de vida de archivos temporales en /tmp y audios del teléfono en CARPETA_AUDIOS.
- Barrera de parada ordenada (shutdown) y drenaje cooperativo de sesiones activas.
- Atexit fallback síncrono para emergencias de terminación del proceso principal.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import subprocess
import threading
import time
import uuid
from typing import Callable, Optional

import edge_tts

# Directorio de audios para teléfono (montado en /audios)
CARPETA_AUDIOS = "/tmp/madeline_audios"

# Configuración por defecto (leída del entorno)
VOZ_EDGE_DEFAULT = os.getenv("MADELINE_VOZ", "es-MX-DaliaNeural").strip()
FILTROS_VOZ_DEFAULT: dict[str, str] = {
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
EFECTO_VOZ_DEFAULT = os.getenv("MADELINE_EFECTO_VOZ", "ninguno").strip().lower()
PIPER_MODELO_DEFAULT = os.getenv("MADELINE_PIPER_MODELO", "").strip()

MINUTOS_VIDA_ARCHIVOS = 10

# --- ESTADO DE CONCURRENCIA Y CONTROL ---
_MUTEX_ESTADO = threading.RLock()
_PROCESO_VOZ_PC: Optional[subprocess.Popen] = None
_RUTA_VOZ_ACTUAL: Optional[str] = None
_TOKEN_ACTUAL: Optional[object] = None
_EPOCH_INTERRUPCION: int = 0
_SHUTDOWN_INICIADO: bool = False

_SESIONES_ACTIVAS: set[asyncio.Task] = set()
_TAREAS_TERMINACION: dict[subprocess.Popen, asyncio.Task] = {}
_LOCKS_TRANSICION: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


def _obtener_lock_transicion() -> asyncio.Lock:
    """Devuelve o inicializa un asyncio.Lock asociado al loop en ejecución."""
    loop = asyncio.get_running_loop()
    with _MUTEX_ESTADO:
        lock = _LOCKS_TRANSICION.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS_TRANSICION[loop] = lock
        return lock


def asegurar_carpeta_audios(ruta: str = CARPETA_AUDIOS) -> str:
    """
    Garantiza que el directorio de audios exista con permisos restrictivos (0o700),
    sea propiedad del usuario actual (UID) y no sea un enlace simbólico inseguro.
    """
    if os.path.islink(ruta):
        raise RuntimeError(f"Ruta de audios insegura (enlace simbólico): {ruta}")

    if not os.path.exists(ruta):
        os.makedirs(ruta, mode=0o700, exist_ok=True)

    st = os.lstat(ruta)
    if os.path.islink(ruta):
        raise RuntimeError(f"Ruta de audios insegura (enlace simbólico): {ruta}")

    if st.st_uid != os.getuid():
        raise PermissionError(f"El directorio {ruta} pertenece a otro usuario (UID {st.st_uid})")

    os.chmod(ruta, 0o700)
    return ruta


def _crear_archivo_seguro(prefijo: str = "respuesta_madeline_pc_", extension: str = ".mp3") -> str:
    """Crea atómicamente un archivo temporal seguro con permisos 0o600 en /tmp."""
    token = uuid.uuid4().hex[:12]
    nombre = f"{prefijo}{token}{extension}"
    ruta = os.path.join("/tmp", nombre)
    fd = os.open(ruta, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    return ruta


def _eliminar_archivo_seguro(ruta: Optional[str]) -> None:
    """Elimina un archivo del sistema de archivos suprimiendo excepciones de I/O."""
    if not ruta:
        return
    try:
        if os.path.exists(ruta):
            os.remove(ruta)
    except OSError:
        pass


def _enviar_sigterm_atomico(proceso: subprocess.Popen) -> bool:
    """Envía SIGTERM de manera idempotente bajo _MUTEX_ESTADO. Retorna True si se envió o ya estaba muerto."""
    with _MUTEX_ESTADO:
        if proceso.poll() is not None:
            return True
        if getattr(proceso, "_sigterm_enviado", False):
            return True
        try:
            proceso.terminate()
            setattr(proceso, "_sigterm_enviado", True)
            return True
        except ProcessLookupError:
            setattr(proceso, "_sigterm_enviado", True)
            return True
        except OSError:
            return False


def _enviar_sigkill_atomico(proceso: subprocess.Popen) -> bool:
    """Envía SIGKILL de manera idempotente bajo _MUTEX_ESTADO. Retorna True si se envió o ya estaba muerto."""
    with _MUTEX_ESTADO:
        if proceso.poll() is not None:
            return True
        if getattr(proceso, "_sigkill_enviado", False):
            return True
        try:
            proceso.kill()
            setattr(proceso, "_sigkill_enviado", True)
            return True
        except ProcessLookupError:
            setattr(proceso, "_sigkill_enviado", True)
            return True
        except OSError:
            return False


async def _ejecutar_escalamiento_proceso(
    proceso: subprocess.Popen,
    ruta_archivo: Optional[str] = None,
    timeout_sigterm: float = 0.5,
    timeout_sigkill: float = 0.5,
) -> bool:
    """
    Worker de terminación y escalamiento:
    1. Envía SIGTERM (vía _enviar_sigterm_atomico).
    2. Espera hasta timeout_sigterm a que muera (poll).
    3. Si sigue vivo, escala a SIGKILL (vía _enviar_sigkill_atomico).
    4. Espera hasta timeout_sigkill a que muera (poll).
    5. Si el proceso murió efectivamente:
       - Si _PROCESO_VOZ_PC es este proceso, lo desasigna bajo _MUTEX_ESTADO.
       - Si ruta_archivo fue provista o coincide con _RUTA_VOZ_ACTUAL, elimina el archivo.
       - Retorna True.
    6. Si el proceso sigue vivo (ej. proceso en D-state ininterrumpible):
       - Conserva _PROCESO_VOZ_PC y NO elimina el archivo.
       - Retorna False.
    """
    _enviar_sigterm_atomico(proceso)

    fin_term = time.monotonic() + timeout_sigterm
    while time.monotonic() < fin_term:
        if proceso.poll() is not None:
            break
        await asyncio.sleep(0.02)

    if proceso.poll() is None:
        _enviar_sigkill_atomico(proceso)
        fin_kill = time.monotonic() + timeout_sigkill
        while time.monotonic() < fin_kill:
            if proceso.poll() is not None:
                break
            await asyncio.sleep(0.02)

    muerto = proceso.poll() is not None

    with _MUTEX_ESTADO:
        global _PROCESO_VOZ_PC, _RUTA_VOZ_ACTUAL
        if muerto:
            if _PROCESO_VOZ_PC is proceso:
                _PROCESO_VOZ_PC = None
                ruta_a_borrar = ruta_archivo or _RUTA_VOZ_ACTUAL
                _RUTA_VOZ_ACTUAL = None
            else:
                ruta_a_borrar = ruta_archivo

            _eliminar_archivo_seguro(ruta_a_borrar)
            return True
        else:
            return False


async def _esperar_o_matar_proceso_coordinado(
    proceso: subprocess.Popen,
    ruta_archivo: Optional[str] = None,
    timeout_sigterm: float = 0.5,
    timeout_sigkill: float = 0.5,
) -> bool:
    """
    Coordina o reutiliza la tarea de escalamiento para un proceso Popen específico.
    Protege la ejecución del worker con asyncio.shield().
    """
    with _MUTEX_ESTADO:
        tarea_existente = _TAREAS_TERMINACION.get(proceso)
        if tarea_existente is None or tarea_existente.done():
            tarea = asyncio.create_task(
                _ejecutar_escalamiento_proceso(
                    proceso,
                    ruta_archivo=ruta_archivo,
                    timeout_sigterm=timeout_sigterm,
                    timeout_sigkill=timeout_sigkill,
                )
            )
            _TAREAS_TERMINACION[proceso] = tarea

            def _limpiar_callback(t: asyncio.Task, p: subprocess.Popen = proceso):
                with _MUTEX_ESTADO:
                    if _TAREAS_TERMINACION.get(p) is t:
                        _TAREAS_TERMINACION.pop(p, None)

            tarea.add_done_callback(_limpiar_callback)
            tarea_existente = tarea

    return await asyncio.shield(tarea_existente)


def _actualizar_avatar_seguro(
    token_esperado: object,
    epoch_esperado: int,
    callback_avatar: Optional[Callable[[str], None]],
    estado: str,
) -> None:
    """Actualiza el estado visual del avatar fuera de locks comprobando la propiedad de la sesión."""
    if callback_avatar is None:
        return
    with _MUTEX_ESTADO:
        es_dueno = (
            _TOKEN_ACTUAL is token_esperado
            and _EPOCH_INTERRUPCION == epoch_esperado
            and not _SHUTDOWN_INICIADO
        )
    if es_dueno:
        try:
            callback_avatar(estado)
        except Exception:
            pass


def interrumpir_voz_pc(callback_avatar: Optional[Callable[[str], None]] = None) -> None:
    """
    Corta inmediatamente cualquier reproducción de voz de Madeline en el PC.
    Invalida el epoch y el token visual, y envía SIGTERM al proceso activo.
    """
    with _MUTEX_ESTADO:
        global _EPOCH_INTERRUPCION, _TOKEN_ACTUAL
        _EPOCH_INTERRUPCION += 1
        _TOKEN_ACTUAL = None
        proceso = _PROCESO_VOZ_PC
        if proceso is not None:
            _enviar_sigterm_atomico(proceso)

    if callback_avatar is not None:
        try:
            callback_avatar("quieta")
        except Exception:
            pass


async def cerrar_servicio_tts(timeout_drain: float = 1.0) -> bool:
    """
    Barrera de parada ordenada (shutdown).
    1. Marca _SHUTDOWN_INICIADO = True bajo _MUTEX_ESTADO para rechazar nuevas admisiones.
    2. Envía interrupción / SIGTERM al proceso actual.
    3. Drena sesiones activas (_SESIONES_ACTIVAS) y tareas de terminación (_TAREAS_TERMINACION).
    4. Confirma que no queden procesos huérfanos ni tareas pendientes.
    Retorna True sii todo el estado fue drenado y los procesos confirmados muertos.
    """
    with _MUTEX_ESTADO:
        global _SHUTDOWN_INICIADO
        _SHUTDOWN_INICIADO = True
        interrumpir_voz_pc()

    fin = time.monotonic() + timeout_drain

    # Espera cooperativa inicial (60% del timeout)
    fin_coop = time.monotonic() + (timeout_drain * 0.6)
    while time.monotonic() < fin_coop:
        with _MUTEX_ESTADO:
            sesiones = [t for t in _SESIONES_ACTIVAS if not t.done()]
            terminaciones = [t for t in _TAREAS_TERMINACION.values() if not t.done()]
            proc = _PROCESO_VOZ_PC
            proc_vivo = proc is not None and proc.poll() is None
            if not sesiones and not terminaciones and not proc_vivo:
                break
        await asyncio.sleep(0.02)

    # Cancelación de sesiones que sigan activas
    with _MUTEX_ESTADO:
        sesiones_restantes = [t for t in _SESIONES_ACTIVAS if not t.done()]
        for s in sesiones_restantes:
            s.cancel()

    # Espera final
    while time.monotonic() < fin:
        with _MUTEX_ESTADO:
            sesiones = [t for t in _SESIONES_ACTIVAS if not t.done()]
            terminaciones = [t for t in _TAREAS_TERMINACION.values() if not t.done()]
            proc = _PROCESO_VOZ_PC
            proc_vivo = proc is not None and proc.poll() is None
            if not sesiones and not terminaciones and not proc_vivo:
                break
        await asyncio.sleep(0.02)

    with _MUTEX_ESTADO:
        sesiones_vivas = [t for t in _SESIONES_ACTIVAS if not t.done()]
        terminaciones_vivas = [t for t in _TAREAS_TERMINACION.values() if not t.done()]
        proc = _PROCESO_VOZ_PC
        proc_muerto = proc is None or proc.poll() is not None

        return not sesiones_vivas and not terminaciones_vivas and proc_muerto


def _limpieza_al_salir() -> None:
    """
    Handler síncrono de atexit como salvaguarda final.
    No asume event loop vivo.
    """
    with _MUTEX_ESTADO:
        global _PROCESO_VOZ_PC, _RUTA_VOZ_ACTUAL
        proc = _PROCESO_VOZ_PC
        ruta = _RUTA_VOZ_ACTUAL
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass

            fin = time.time() + 0.2
            while time.time() < fin:
                if proc.poll() is not None:
                    break
                time.sleep(0.01)

            if proc.poll() is not None:
                _eliminar_archivo_seguro(ruta)
                _PROCESO_VOZ_PC = None
                _RUTA_VOZ_ACTUAL = None


atexit.register(_limpieza_al_salir)


async def generar_voz_local(
    texto: str,
    ruta_salida: str,
    piper_modelo: Optional[str] = None,
) -> bool:
    """
    Genera la voz con un motor local en ruta_salida. Devuelve False
    si no hay ningún motor instalado o si falló.
    """
    modelo = piper_modelo if piper_modelo is not None else PIPER_MODELO_DEFAULT
    ruta_wav = f"{ruta_salida}.local.wav"

    if modelo and shutil.which("piper-tts"):
        comando = ["piper-tts", "--model", modelo, "--output_file", ruta_wav]
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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await proceso.communicate(entrada)

        if proceso.returncode != 0 or not os.path.exists(ruta_wav):
            return False

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
            try:
                os.remove(ruta_wav)
            except OSError:
                pass


async def generar_voz_edge(
    texto: str,
    ruta_salida: str,
    voz_edge: Optional[str] = None,
    efecto: Optional[str] = None,
    filtros: Optional[dict[str, str]] = None,
) -> None:
    """
    Genera la voz de Madeline en ruta_salida (MP3), con el efecto de
    efecto si hay ffmpeg. Si el efecto falla, queda la voz limpia.
    """
    voz_usada = voz_edge if voz_edge is not None else VOZ_EDGE_DEFAULT
    filtros_usados = filtros if filtros is not None else FILTROS_VOZ_DEFAULT
    efecto_usado = efecto if efecto is not None else EFECTO_VOZ_DEFAULT

    voz = edge_tts.Communicate(texto, voice=voz_usada)
    filtro = filtros_usados.get(efecto_usado)

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
            err_msg = error.decode(errors="replace")[:200] if error else ""
            print(f"[!] Falló el efecto de voz, uso la voz limpia: {err_msg}")
            os.replace(ruta_cruda, ruta_salida)
    finally:
        if os.path.exists(ruta_cruda):
            try:
                os.remove(ruta_cruda)
            except OSError:
                pass


async def generar_voz(
    texto: str,
    ruta_salida: str,
    efecto: Optional[str] = None,
    filtros: Optional[dict[str, str]] = None,
    voz_edge: Optional[str] = None,
    piper_modelo: Optional[str] = None,
) -> None:
    """
    Genera la voz de Madeline en ruta_salida con edge-tts y, si falla,
    con la voz local de respaldo. Solo lanza la excepción de edge-tts
    si tampoco hay voz local.
    """
    try:
        await generar_voz_edge(
            texto,
            ruta_salida,
            voz_edge=voz_edge,
            efecto=efecto,
            filtros=filtros,
        )
    except Exception as e:
        print(f"[!] edge-tts falló, uso la voz local de respaldo: {e}")
        if not await generar_voz_local(texto, ruta_salida, piper_modelo=piper_modelo):
            raise


async def hablar_por_parlantes(
    texto: str,
    callback_avatar: Optional[Callable[[str], None]] = None,
    efecto: Optional[str] = None,
    filtros: Optional[dict[str, str]] = None,
    voz_edge: Optional[str] = None,
    piper_modelo: Optional[str] = None,
) -> None:
    """
    Genera la voz de Madeline y la reproduce en los parlantes locales de manera segura y no bloqueante.
    """
    if not texto:
        return

    global _PROCESO_VOZ_PC, _RUTA_VOZ_ACTUAL, _TOKEN_ACTUAL

    tarea_actual = asyncio.current_task()
    if tarea_actual is None:
        return

    with _MUTEX_ESTADO:
        if _SHUTDOWN_INICIADO:
            return
        _SESIONES_ACTIVAS.add(tarea_actual)
        epoch_sesion = _EPOCH_INTERRUPCION

    ruta_respuesta = None
    proceso = None
    mi_token = object()

    try:
        try:
            ruta_respuesta = _crear_archivo_seguro()
        except Exception as e:
            print(f"[!] Error al crear archivo seguro para TTS: {e}")
            return

        try:
            await generar_voz(
                texto,
                ruta_respuesta,
                efecto=efecto,
                filtros=filtros,
                voz_edge=voz_edge,
                piper_modelo=piper_modelo,
            )
        except Exception as e:
            print(f"[!] Error al generar voz: {e}")
            return

        with _MUTEX_ESTADO:
            if _EPOCH_INTERRUPCION != epoch_sesion or _SHUTDOWN_INICIADO:
                return

        lock_transicion = _obtener_lock_transicion()
        async with lock_transicion:
            with _MUTEX_ESTADO:
                if _EPOCH_INTERRUPCION != epoch_sesion or _SHUTDOWN_INICIADO:
                    return

                proceso_anterior = _PROCESO_VOZ_PC
                ruta_anterior = _RUTA_VOZ_ACTUAL

            if proceso_anterior is not None:
                await _esperar_o_matar_proceso_coordinado(proceso_anterior, ruta_anterior)

            with _MUTEX_ESTADO:
                if _EPOCH_INTERRUPCION != epoch_sesion or _SHUTDOWN_INICIADO:
                    return

            try:
                nuevo_proceso = subprocess.Popen(
                    ["mpv", "--no-video", ruta_respuesta],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                print(f"[!] Error al ejecutar mpv: {e}")
                return

            proceso = nuevo_proceso

            with _MUTEX_ESTADO:
                if _EPOCH_INTERRUPCION != epoch_sesion or _SHUTDOWN_INICIADO:
                    _PROCESO_VOZ_PC = proceso
                    _RUTA_VOZ_ACTUAL = ruta_respuesta
                    _TOKEN_ACTUAL = None
                    necesita_rollback = True
                else:
                    _PROCESO_VOZ_PC = proceso
                    _RUTA_VOZ_ACTUAL = ruta_respuesta
                    _TOKEN_ACTUAL = mi_token
                    necesita_rollback = False

            if necesita_rollback:
                await _esperar_o_matar_proceso_coordinado(proceso, ruta_respuesta)
                return

            _actualizar_avatar_seguro(mi_token, epoch_sesion, callback_avatar, "hablando")

        while True:
            if proceso.poll() is not None:
                break

            with _MUTEX_ESTADO:
                interrumpido = (_EPOCH_INTERRUPCION != epoch_sesion or _SHUTDOWN_INICIADO)

            if interrumpido:
                await _esperar_o_matar_proceso_coordinado(proceso, ruta_respuesta)
                break

            await asyncio.sleep(0.05)

    finally:
        try:
            if proceso is None:
                _eliminar_archivo_seguro(ruta_respuesta)
            else:
                if proceso.poll() is None:
                    try:
                        await _esperar_o_matar_proceso_coordinado(proceso, ruta_respuesta)
                    except Exception:
                        pass
                else:
                    with _MUTEX_ESTADO:
                        if _PROCESO_VOZ_PC is proceso:
                            _PROCESO_VOZ_PC = None
                            _RUTA_VOZ_ACTUAL = None
                    _eliminar_archivo_seguro(ruta_respuesta)

            _actualizar_avatar_seguro(mi_token, epoch_sesion, callback_avatar, "quieta")
        finally:
            with _MUTEX_ESTADO:
                _SESIONES_ACTIVAS.discard(tarea_actual)


def limpiar_audios_viejos(carpeta: str = CARPETA_AUDIOS, minutos: int = MINUTOS_VIDA_ARCHIVOS) -> None:
    """Borra audios para el teléfono en CARPETA_AUDIOS con más de 'minutos' minutos de antigüedad."""
    if not os.path.exists(carpeta):
        return
    limite = time.time() - (minutos * 60)
    try:
        nombres = os.listdir(carpeta)
    except OSError:
        return
    for n in nombres:
        ruta = os.path.join(carpeta, n)
        try:
            if os.path.getmtime(ruta) < limite:
                os.remove(ruta)
        except OSError:
            pass


async def generar_audio_para_telefono(
    texto: str,
    carpeta: str = CARPETA_AUDIOS,
    efecto: Optional[str] = None,
    filtros: Optional[dict[str, str]] = None,
    voz_edge: Optional[str] = None,
    piper_modelo: Optional[str] = None,
) -> Optional[str]:
    """
    Genera la voz de Madeline para el teléfono en CARPETA_AUDIOS.
    No reproduce en el PC. Devuelve la ruta HTTP '/audios/<nombre>.mp3' o None.
    """
    if not texto:
        return None

    asegurar_carpeta_audios(carpeta)
    limpiar_audios_viejos(carpeta)

    nombre_archivo = f"respuesta_madeline_{uuid.uuid4().hex[:12]}.mp3"
    ruta_archivo = os.path.join(carpeta, nombre_archivo)

    try:
        await generar_voz(
            texto,
            ruta_archivo,
            efecto=efecto,
            filtros=filtros,
            voz_edge=voz_edge,
            piper_modelo=piper_modelo,
        )
        print(f"[*] Audio para teléfono generado: {ruta_archivo}")
        return f"/audios/{nombre_archivo}"
    except Exception as e:
        print(f"[!] Falló la generación de voz para teléfono: {e}")
        _eliminar_archivo_seguro(ruta_archivo)
        return None


async def procesar_salida_de_voz(
    texto: str,
    origen: str,
    callback_avatar: Optional[Callable[[str], None]] = None,
    efecto: Optional[str] = None,
    filtros: Optional[dict[str, str]] = None,
    voz_edge: Optional[str] = None,
    piper_modelo: Optional[str] = None,
) -> Optional[str]:
    """
    Decide dónde debe salir la voz de Madeline.
    PC: reproduce localmente y devuelve None.
    TELEFONO: genera archivo y devuelve ruta HTTP.
    """
    origen_norm = (origen or "pc").lower().strip()
    if origen_norm == "telefono":
        return await generar_audio_para_telefono(
            texto,
            efecto=efecto,
            filtros=filtros,
            voz_edge=voz_edge,
            piper_modelo=piper_modelo,
        )

    await hablar_por_parlantes(
        texto,
        callback_avatar=callback_avatar,
        efecto=efecto,
        filtros=filtros,
        voz_edge=voz_edge,
        piper_modelo=piper_modelo,
    )
    return None
