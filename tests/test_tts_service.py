"""
Pruebas exhaustivas para el subsistema de Síntesis de Voz (TTS) y ciclo de vida de procesos U-3.2.

Cubre:
1. Rollback post-spawn con proceso ininterrumpible (D-state): preservación de referencia y archivo.
2. Rollback post-spawn exitoso: terminación coordinada y eliminación única de archivo.
3. Cancelación de sesión con asyncio.shield: persistencia y compleción del worker de limpieza.
4. Barrera de shutdown (cerrar_servicio_tts): drenaje, detección de procesos huérfanos y timeout.
5. Admisión atómica: rechazo inmediato tras inicio de shutdown sin efectos colaterales.
6. Manejo de fallos en creación de archivo: retirada limpia de _SESIONES_ACTIVAS.
7. Atomicidad e idempotencia de señales SIGTERM y SIGKILL.
8. Fallos de señalización por OSError: preservación de estado sin flags falsos.
9. Identidad e independencia de tareas de terminación para diferentes procesos.
10. Desacoplamiento del avatar: preservación de estado visual ante interrupción/preempción.
11. Fallback síncrono de atexit (_limpieza_al_salir).
12. Seguridad del directorio de audios (permisos 0o700, detección de symlinks y UID).
13. Fachadas de compatibilidad en server.madeline_server.
"""

import asyncio
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import madeline_server as ms
from services import tts


class MockPopen:
    """Simulador determinista de subprocess.Popen para pruebas de concurrencia y señales."""

    def __init__(self, pid=9999, alive=True):
        self.pid = pid
        self._alive = alive
        self.terminate_called = 0
        self.kill_called = 0
        self.returncode = None if alive else 0
        self.morir_en_kill = False
        self.morir_en_terminate = False
        self.lanzar_en_terminate = None
        self.lanzar_en_kill = None

    def poll(self):
        if self._alive:
            return None
        return self.returncode if self.returncode is not None else 0

    def terminate(self):
        self.terminate_called += 1
        if self.lanzar_en_terminate:
            raise self.lanzar_en_terminate
        if self.morir_en_terminate:
            self._alive = False
            self.returncode = -15

    def kill(self):
        self.kill_called += 1
        if self.lanzar_en_kill:
            raise self.lanzar_en_kill
        if self.morir_en_kill:
            self._alive = False
            self.returncode = -9


class TestTTSServiceCicloDeVida(unittest.IsolatedAsyncioTestCase):
    """Pruebas del ciclo de vida y concurrencia del servicio TTS."""

    def setUp(self):
        self._archivos_para_borrar = []
        with tts._MUTEX_ESTADO:
            tts._PROCESO_VOZ_PC = None
            tts._RUTA_VOZ_ACTUAL = None
            tts._TOKEN_ACTUAL = None
            tts._EPOCH_INTERRUPCION = 0
            tts._SHUTDOWN_INICIADO = False
            tts._SESIONES_ACTIVAS.clear()
            tts._TAREAS_TERMINACION.clear()

    def tearDown(self):
        with tts._MUTEX_ESTADO:
            tts._PROCESO_VOZ_PC = None
            if tts._RUTA_VOZ_ACTUAL and os.path.exists(tts._RUTA_VOZ_ACTUAL):
                tts._eliminar_archivo_seguro(tts._RUTA_VOZ_ACTUAL)
            tts._RUTA_VOZ_ACTUAL = None
            tts._TOKEN_ACTUAL = None
            tts._EPOCH_INTERRUPCION = 0
            tts._SHUTDOWN_INICIADO = False
            tts._SESIONES_ACTIVAS.clear()
            tts._TAREAS_TERMINACION.clear()

        for ruta in self._archivos_para_borrar:
            tts._eliminar_archivo_seguro(ruta)

    async def test_01_rollback_post_spawn_proceso_ininterrumpible_preserva_archivo(self):
        """Si un proceso mpv no muere tras SIGTERM y SIGKILL (D-state), el archivo y la referencia se conservan."""
        proceso_zombie = MockPopen(alive=True)

        def popen_con_interrupcion(*a, **k):
            tts._EPOCH_INTERRUPCION += 1  # simula /callar o nueva sesión durante spawn
            return proceso_zombie

        with mock.patch.object(tts, "generar_voz", mock.AsyncMock()), \
             mock.patch("subprocess.Popen", side_effect=popen_con_interrupcion):

            await tts.hablar_por_parlantes("test mensaje")

        # El rollback debió registrar el proceso y tratar de matarlo
        self.assertEqual(proceso_zombie.terminate_called, 1)
        self.assertEqual(proceso_zombie.kill_called, 1)

        # Invariante de seguridad: si sigue vivo, se preserva en _PROCESO_VOZ_PC y su archivo NO se elimina
        with tts._MUTEX_ESTADO:
            self.assertIs(tts._PROCESO_VOZ_PC, proceso_zombie)
            self.assertIsNotNone(tts._RUTA_VOZ_ACTUAL)
            ruta_preservada = tts._RUTA_VOZ_ACTUAL
            self.assertTrue(os.path.exists(ruta_preservada))
            self._archivos_para_borrar.append(ruta_preservada)

    async def test_02_rollback_post_spawn_exitoso_limpia_archivo_y_referencia(self):
        """Si el rollback post-spawn logra terminar el proceso con SIGTERM, el archivo se borra limpiamente."""
        proceso_sumiso = MockPopen(alive=True)
        proceso_sumiso.morir_en_terminate = True

        ruta_creada = []
        orig_crear = tts._crear_archivo_seguro

        def fake_crear_archivo():
            r = orig_crear()
            ruta_creada.append(r)
            return r

        def popen_con_interrupcion(*a, **k):
            tts._EPOCH_INTERRUPCION += 1
            return proceso_sumiso

        with mock.patch.object(tts, "generar_voz", mock.AsyncMock()), \
             mock.patch.object(tts, "_crear_archivo_seguro", side_effect=fake_crear_archivo), \
             mock.patch("subprocess.Popen", side_effect=popen_con_interrupcion):

            await tts.hablar_por_parlantes("test rollback exitoso")

            self.assertEqual(proceso_sumiso.terminate_called, 1)
            self.assertEqual(proceso_sumiso.kill_called, 0)

            with tts._MUTEX_ESTADO:
                self.assertIsNone(tts._PROCESO_VOZ_PC)
                self.assertIsNone(tts._RUTA_VOZ_ACTUAL)

            self.assertEqual(len(ruta_creada), 1)
            self.assertFalse(os.path.exists(ruta_creada[0]))

    async def test_03_cancelacion_sesion_con_shield_persiste_worker_de_limpieza(self):
        """Una sesión cancelada mientras espera la terminación no debe abortar la tarea shielded de limpieza."""
        proceso = MockPopen(alive=True)
        # Muere solo tras kill
        proceso.morir_en_kill = True

        ruta_temp = tts._crear_archivo_seguro()
        self._archivos_para_borrar.append(ruta_temp)

        tarea_terminacion = asyncio.create_task(
            tts._esperar_o_matar_proceso_coordinado(
                proceso,
                ruta_archivo=ruta_temp,
                timeout_sigterm=0.05,
                timeout_sigkill=0.05,
            )
        )

        # Dar un ciclo para que el worker shielded sea creado y comience
        await asyncio.sleep(0.01)

        # Cancelar la corrutina que espera el shield
        tarea_terminacion.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tarea_terminacion

        # Esperar un instante para que el worker interno continúe ejecutándose bajo el shield
        await asyncio.sleep(0.15)

        # El worker debió ejecutar terminate y kill hasta confirmar la muerte
        self.assertEqual(proceso.terminate_called, 1)
        self.assertEqual(proceso.kill_called, 1)
        self.assertFalse(proceso._alive)
        self.assertFalse(os.path.exists(ruta_temp))

    async def test_04_shutdown_drenaje_completo_y_timeout(self):
        """cerrar_servicio_tts drena sesiones activas y retorna True solo si no quedan procesos vivos."""
        # 4a. Proceso que muere normalmente
        proc_normal = MockPopen(alive=True)
        proc_normal.morir_en_terminate = True

        with tts._MUTEX_ESTADO:
            tts._PROCESO_VOZ_PC = proc_normal

        resultado = await tts.cerrar_servicio_tts(timeout_drain=0.2)
        self.assertTrue(resultado)
        self.assertEqual(proc_normal.terminate_called, 1)
        self.assertFalse(proc_normal._alive)

        # 4b. Proceso que nunca muere (retorna False en timeout)
        self.setUp()
        proc_bloqueado = MockPopen(alive=True)

        with tts._MUTEX_ESTADO:
            tts._PROCESO_VOZ_PC = proc_bloqueado

        resultado_bloqueado = await tts.cerrar_servicio_tts(timeout_drain=0.1)
        self.assertFalse(resultado_bloqueado)
        self.assertTrue(proc_bloqueado.terminate_called >= 1)

    async def test_05_admision_atomica_rechazo_tras_shutdown(self):
        """Una llamada a hablar_por_parlantes tras iniciar el shutdown es rechazada de inmediato."""
        with tts._MUTEX_ESTADO:
            tts._SHUTDOWN_INICIADO = True

        with mock.patch.object(tts, "generar_voz", mock.AsyncMock()) as mock_gen:
            await tts.hablar_por_parlantes("no debe reproducirse")
            mock_gen.assert_not_called()

        with tts._MUTEX_ESTADO:
            self.assertEqual(len(tts._SESIONES_ACTIVAS), 0)

    async def test_06_fallo_en_creacion_de_archivo_limpia_sesion_activa(self):
        """Si la creación del archivo temporal arroja excepción, la sesión se retira de _SESIONES_ACTIVAS."""
        with mock.patch.object(tts, "_crear_archivo_seguro", side_effect=OSError("No space")):
            await tts.hablar_por_parlantes("test error archivo")

        with tts._MUTEX_ESTADO:
            self.assertEqual(len(tts._SESIONES_ACTIVAS), 0)

    async def test_07_atomicidad_e_idempotencia_de_senales(self):
        """_enviar_sigterm_atomico y _enviar_sigkill_atomico no duplican señales si ya fueron enviadas."""
        proc = MockPopen(alive=True)

        res1 = tts._enviar_sigterm_atomico(proc)
        res2 = tts._enviar_sigterm_atomico(proc)

        self.assertTrue(res1)
        self.assertTrue(res2)
        self.assertEqual(proc.terminate_called, 1)
        self.assertTrue(getattr(proc, "_sigterm_enviado"))

        res_kill1 = tts._enviar_sigkill_atomico(proc)
        res_kill2 = tts._enviar_sigkill_atomico(proc)

        self.assertTrue(res_kill1)
        self.assertTrue(res_kill2)
        self.assertEqual(proc.kill_called, 1)
        self.assertTrue(getattr(proc, "_sigkill_enviado"))

    async def test_08_fallo_de_senal_por_oserror_no_marca_flags_falsos(self):
        """Si terminate() arroja OSError que no es ProcessLookupError, no se establece el flag y retorna False."""
        proc = MockPopen(alive=True)
        proc.lanzar_en_terminate = PermissionError("EPERM")

        res = tts._enviar_sigterm_atomico(proc)
        self.assertFalse(res)
        self.assertFalse(getattr(proc, "_sigterm_enviado", False))

        proc.lanzar_en_kill = PermissionError("EPERM")
        res_kill = tts._enviar_sigkill_atomico(proc)
        self.assertFalse(res_kill)
        self.assertFalse(getattr(proc, "_sigkill_enviado", False))

    async def test_09_independencia_de_tareas_de_terminacion_por_proceso(self):
        """Dos procesos Popen diferentes tienen entradas y callbacks independientes en _TAREAS_TERMINACION."""
        p1 = MockPopen(pid=101, alive=True)
        p1.morir_en_kill = True
        p2 = MockPopen(pid=102, alive=True)
        p2.morir_en_kill = True

        t1 = asyncio.create_task(
            tts._esperar_o_matar_proceso_coordinado(p1, timeout_sigterm=0.05, timeout_sigkill=0.05)
        )
        t2 = asyncio.create_task(
            tts._esperar_o_matar_proceso_coordinado(p2, timeout_sigterm=0.05, timeout_sigkill=0.05)
        )

        await asyncio.sleep(0.01)

        with tts._MUTEX_ESTADO:
            self.assertIn(p1, tts._TAREAS_TERMINACION)
            self.assertIn(p2, tts._TAREAS_TERMINACION)
            self.assertIsNot(tts._TAREAS_TERMINACION[p1], tts._TAREAS_TERMINACION[p2])

        await asyncio.gather(t1, t2)

        with tts._MUTEX_ESTADO:
            self.assertNotIn(p1, tts._TAREAS_TERMINACION)
            self.assertNotIn(p2, tts._TAREAS_TERMINACION)

    async def test_10_avatar_desacoplado_no_sobreescribe_tras_interrupcion(self):
        """Si la sesión de voz fue interrumpida y el avatar pasó a 'escuchando', no se pisa con 'quieta'."""
        estados_avatar = []

        def callback_avatar(estado):
            estados_avatar.append(estado)

        proceso_hablando = MockPopen(alive=True)

        async def fake_sleep_con_interrupcion(segundos):
            # En medio de la reproducción, llega una orden de interrupción externa
            tts.interrumpir_voz_pc()
            callback_avatar("escuchando")
            proceso_hablando._alive = False
            proceso_hablando.returncode = -15

        with mock.patch.object(tts, "generar_voz", mock.AsyncMock()), \
             mock.patch("subprocess.Popen", return_value=proceso_hablando), \
             mock.patch("asyncio.sleep", side_effect=fake_sleep_con_interrupcion):

            await tts.hablar_por_parlantes("hola madeline", callback_avatar=callback_avatar)

        # La secuencia debió ser: "hablando", luego por interrupción externa "escuchando".
        # La sesión terminada NO debió llamar a "quieta".
        self.assertEqual(estados_avatar, ["hablando", "escuchando"])

    def test_11_limpieza_al_salir_atexit_sincrono(self):
        """_limpieza_al_salir envía SIGKILL y borra el archivo temporal de manera síncrona."""
        proc = MockPopen(alive=True)
        proc.morir_en_kill = True

        ruta_temp = tts._crear_archivo_seguro()
        self.assertTrue(os.path.exists(ruta_temp))

        with tts._MUTEX_ESTADO:
            tts._PROCESO_VOZ_PC = proc
            tts._RUTA_VOZ_ACTUAL = ruta_temp

        tts._limpieza_al_salir()

        self.assertEqual(proc.kill_called, 1)
        self.assertFalse(os.path.exists(ruta_temp))
        with tts._MUTEX_ESTADO:
            self.assertIsNone(tts._PROCESO_VOZ_PC)
            self.assertIsNone(tts._RUTA_VOZ_ACTUAL)

    def test_12_seguridad_directorio_audios(self):
        """asegurar_carpeta_audios rechaza symlinks y asegura permisos 0o700."""
        with tempfile.TemporaryDirectory() as base_tmp:
            ruta_real = os.path.join(base_tmp, "audios_reales")
            ruta_link = os.path.join(base_tmp, "audios_symlink")

            os.makedirs(ruta_real, exist_ok=True)
            os.symlink(ruta_real, ruta_link)

            # Debe rechazar enlaces simbólicos
            with self.assertRaises(RuntimeError):
                tts.asegurar_carpeta_audios(ruta_link)

            # Carpeta normal debe crearse con 0o700
            ruta_valida = os.path.join(base_tmp, "audios_seguros")
            res = tts.asegurar_carpeta_audios(ruta_valida)
            self.assertEqual(res, ruta_valida)
            self.assertTrue(os.path.exists(ruta_valida))

            st = os.stat(ruta_valida)
            permisos = stat.S_IMODE(st.st_mode)
            self.assertEqual(permisos, 0o700)

    async def test_13_fachadas_compatibilidad_en_madeline_server(self):
        """ms.interrumpir_voz_pc, ms.PROCESO_VOZ_PC, ms.generar_audio_para_telefono y ms.procesar_salida_de_voz funcionan como fachadas."""
        # 13a. Inspección de PROCESO_VOZ_PC vía __getattr__
        proc = MockPopen(alive=True)
        with tts._MUTEX_ESTADO:
            tts._PROCESO_VOZ_PC = proc

        self.assertIs(ms.PROCESO_VOZ_PC, proc)

        # 13b. ms.interrumpir_voz_pc
        proc.morir_en_terminate = True
        ms.interrumpir_voz_pc()
        self.assertEqual(proc.terminate_called, 1)

        # 13c. ms.generar_audio_para_telefono
        with mock.patch.object(tts, "generar_voz", mock.AsyncMock()):
            audio_url = await ms.generar_audio_para_telefono("mensaje teléfono")
            self.assertIsNotNone(audio_url)
            self.assertTrue(audio_url.startswith("/audios/respuesta_madeline_"))

        # 13d. ms.procesar_salida_de_voz
        with mock.patch.object(tts, "hablar_por_parlantes", mock.AsyncMock()) as mock_hablar, \
             mock.patch.object(tts, "generar_audio_para_telefono", mock.AsyncMock(return_value="/audios/test.mp3")) as mock_tel:

            res_pc = await ms.procesar_salida_de_voz("hola pc", "pc")
            self.assertIsNone(res_pc)
            mock_hablar.assert_called_once()

            res_tel = await ms.procesar_salida_de_voz("hola tel", "telefono")
            self.assertEqual(res_tel, "/audios/test.mp3")
            mock_tel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
