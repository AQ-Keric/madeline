"""Pruebas unitarias para services.system y compatibilidad con la fachada en Stage 4.1."""

import os
import subprocess
import threading
import time
import unittest
from unittest import mock

import madeline_server as ms
from services import system


class TestUsoCpu(unittest.TestCase):
    """Verifica la semántica de uso_cpu(), calibración, ventanas temporales y concurrencia."""

    def setUp(self):
        # Guardar y resetear estado interno
        self._prev_muestra = system._MUESTRA_CPU
        self._prev_ultimo = system._ULTIMO_USO_CPU
        system._MUESTRA_CPU = None
        system._ULTIMO_USO_CPU = 0.0

    def tearDown(self):
        system._MUESTRA_CPU = self._prev_muestra
        system._ULTIMO_USO_CPU = self._prev_ultimo

    def test_primera_medicion_en_frio(self):
        """La primera llamada sin muestra previa debe medir con sleep de 200 ms."""
        with mock.patch("services.system._leer_proc_stat", side_effect=[(1000, 500), (1200, 550)]), \
             mock.patch("services.system.time.sleep") as mock_sleep:
            res = system.uso_cpu()
            mock_sleep.assert_called_once_with(0.2)
            # total delta = 200, inactivo delta = 50 -> uso = 100 * (1 - 50/200) = 75.0%
            self.assertEqual(res, 75.0)
            self.assertEqual(system._ULTIMO_USO_CPU, 75.0)
            self.assertIsNotNone(system._MUESTRA_CPU)
            self.assertEqual(system._MUESTRA_CPU[1], 1200)
            self.assertEqual(system._MUESTRA_CPU[2], 550)

    def test_segunda_llamada_inmediata_reutiliza_ultimo_valor(self):
        """Llamadas con delta < 0.25 s deben reutilizar _ULTIMO_USO_CPU sin dormir ni leer procfs."""
        t_base = 1000.0
        with mock.patch("services.system.time.monotonic", side_effect=[t_base, t_base + 0.2, t_base + 0.25]), \
             mock.patch("services.system._leer_proc_stat", side_effect=[(1000, 500), (1200, 550)]), \
             mock.patch("services.system.time.sleep"):
            # Medición en frío inicial (registra muestra en t_base + 0.2)
            uso1 = system.uso_cpu()
            self.assertEqual(uso1, 75.0)

            # Segunda llamada en t_base + 0.25 (delta = 0.05 s < 0.25 s)
            with mock.patch("services.system._leer_proc_stat") as mock_stat, \
                 mock.patch("services.system.time.sleep") as mock_sleep:
                uso2 = system.uso_cpu()
                self.assertEqual(uso2, 75.0)
                mock_stat.assert_not_called()
                mock_sleep.assert_not_called()

    def test_llamadas_entre_025_y_10_segundos_ventana_caliente(self):
        """Llamadas entre 0.25 s y 10.0 s deben tomar una sola muestra y no dormir."""
        t_base = 1000.0
        with mock.patch("services.system.time.monotonic", side_effect=[t_base, t_base + 0.2, t_base + 2.0]), \
             mock.patch("services.system._leer_proc_stat", side_effect=[(1000, 500), (1200, 550), (1400, 650)]), \
             mock.patch("services.system.time.sleep") as mock_sleep:
            # Medición en frío
            system.uso_cpu()
            mock_sleep.assert_called_once_with(0.2)
            mock_sleep.reset_mock()

            # Medición caliente a los 1.8 s (t_base + 2.0 - (t_base + 0.2))
            # total previo = 1200, nuevo = 1400 -> delta total = 200
            # inactivo previo = 550, nuevo = 650 -> delta inactivo = 100
            # uso = 100 * (1 - 100/200) = 50.0%
            uso_caliente = system.uso_cpu()
            mock_sleep.assert_not_called()
            self.assertEqual(uso_caliente, 50.0)
            self.assertEqual(system._ULTIMO_USO_CPU, 50.0)

    def test_muestra_caducada_mayor_10_segundos(self):
        """Llamadas tras > 10.0 s deben descartar la muestra antigua y volver a medir con sleep."""
        t_base = 1000.0
        with mock.patch("services.system.time.monotonic", side_effect=[t_base, t_base + 0.2, t_base + 15.0, t_base + 15.2]), \
             mock.patch("services.system._leer_proc_stat", side_effect=[(1000, 500), (1200, 550), (2000, 1000), (2200, 1050)]), \
             mock.patch("services.system.time.sleep") as mock_sleep:
            system.uso_cpu()
            self.assertEqual(mock_sleep.call_count, 1)

            # A los 15 s (> 10 s): nuevo muestreo con sleep
            uso_caducado = system.uso_cpu()
            self.assertEqual(mock_sleep.call_count, 2)
            # delta total = 200, delta inactivo = 50 -> 75.0%
            self.assertEqual(uso_caducado, 75.0)

    def test_delta_total_cero_o_invalido_en_caliente(self):
        """Si delta_total <= 0 en ventana caliente, debe conservar _ULTIMO_USO_CPU y no corromper la muestra."""
        t_base = 1000.0
        with mock.patch("services.system.time.monotonic", side_effect=[t_base, t_base + 0.2, t_base + 1.0]), \
             mock.patch("services.system._leer_proc_stat", side_effect=[(1000, 500), (1200, 550), (1200, 550)]), \
             mock.patch("services.system.time.sleep"):
            system.uso_cpu()
            self.assertEqual(system._ULTIMO_USO_CPU, 75.0)
            muestra_valida = system._MUESTRA_CPU

            # Llamada con delta = 0
            uso_invalido = system.uso_cpu()
            self.assertEqual(uso_invalido, 75.0)
            # Debe mantener la muestra válida anterior intacta
            self.assertEqual(system._MUESTRA_CPU, muestra_valida)

    def test_delta_total_cero_o_invalido_en_frio(self):
        """Si delta_total <= 0 en medición fría, debe conservar _ULTIMO_USO_CPU y no registrar muestra inválida."""
        system._ULTIMO_USO_CPU = 33.3
        with mock.patch("services.system._leer_proc_stat", side_effect=[(1000, 500), (1000, 500)]), \
             mock.patch("services.system.time.sleep"):
            res = system.uso_cpu()
            self.assertEqual(res, 33.3)
            # _MUESTRA_CPU no debe reemplazarse por la muestra inválida
            self.assertIsNone(system._MUESTRA_CPU)

    def test_arranque_cpu_delta_cero_o_invalido(self):
        """Arranque con _ULTIMO_USO_CPU=0.0 y _MUESTRA_CPU=None donde la medición fría produce delta <= 0."""
        system._ULTIMO_USO_CPU = 0.0
        system._MUESTRA_CPU = None

        with mock.patch("services.system._leer_proc_stat", side_effect=[(1000, 500), (1000, 500)]), \
             mock.patch("services.system.time.sleep") as mock_sleep:
            res = system.uso_cpu()
            mock_sleep.assert_called_once_with(0.2)
            self.assertEqual(res, 0.0)
            self.assertIsNone(system._MUESTRA_CPU)
            self.assertEqual(system._ULTIMO_USO_CPU, 0.0)

        # Verificar que una consulta posterior puede realizar una nueva calibración exitosa
        with mock.patch("services.system._leer_proc_stat", side_effect=[(1000, 500), (1200, 550)]), \
             mock.patch("services.system.time.sleep") as mock_sleep2:
            res2 = system.uso_cpu()
            mock_sleep2.assert_called_once_with(0.2)
            self.assertEqual(res2, 75.0)
            self.assertIsNotNone(system._MUESTRA_CPU)
            self.assertEqual(system._ULTIMO_USO_CPU, 75.0)

    def test_marca_temporal_actualizada_post_sleep(self):
        """En medición en frío, la marca temporal debe registrarse después de completar el segundo muestreo."""
        t_start = 100.0
        t_end = 100.25
        with mock.patch("services.system.time.monotonic", side_effect=[t_start, t_end]), \
             mock.patch("services.system._leer_proc_stat", side_effect=[(1000, 500), (1200, 550)]), \
             mock.patch("services.system.time.sleep"):
            system.uso_cpu()
            self.assertEqual(system._MUESTRA_CPU[0], t_end)

    def test_concurrencia_multiples_hilos(self):
        """Múltiples hilos llamando a uso_cpu() simultáneamente deben producir exactamente una calibración fría."""
        num_hilos = 5
        barrera = threading.Barrier(num_hilos)
        resultados = []
        errores = []

        stat_calls = 0
        lock_stat = threading.Lock()

        def mock_stat():
            nonlocal stat_calls
            with lock_stat:
                stat_calls += 1
                if stat_calls == 1:
                    return (1000, 500)
                return (1200, 550)

        sleep_calls = 0
        lock_sleep = threading.Lock()

        def mock_sleep(d):
            nonlocal sleep_calls
            with lock_sleep:
                sleep_calls += 1

        def worker():
            try:
                barrera.wait(timeout=2.0)
                val = system.uso_cpu()
                resultados.append(val)
            except Exception as e:
                errores.append(e)

        with mock.patch("services.system._leer_proc_stat", side_effect=mock_stat), \
             mock.patch("services.system.time.sleep", side_effect=mock_sleep):
            hilos = [threading.Thread(target=worker) for _ in range(num_hilos)]
            for h in hilos:
                h.start()
            for h in hilos:
                h.join(timeout=3.0)

        # 1. Ausencia de deadlocks y finalización de todos los hilos
        self.assertEqual(len(resultados), num_hilos)
        # 2. Ausencia de excepciones
        self.assertEqual(errores, [])
        # 3. Exactamente una calibración fría: 2 lecturas a _leer_proc_stat
        self.assertEqual(stat_calls, 2)
        # 4. Exactamente una invocación a time.sleep(0.2)
        self.assertEqual(sleep_calls, 1)
        # 5. Resultados idénticos y válidos para todos los hilos
        for val in resultados:
            self.assertEqual(val, 75.0)

    def test_frontera_temporal_exacta_025_segundos(self):
        """En delta_tiempo == 0.25 s debe entrar a la ventana caliente (una lectura de proc_stat, sin sleep)."""
        system._MUESTRA_CPU = (100.0, 1000, 500)
        system._ULTIMO_USO_CPU = 50.0

        # Subcaso A: delta_tiempo < 0.25 (ej. 0.249 s) -> reutiliza sin leer proc_stat
        with mock.patch("services.system.time.monotonic", return_value=100.249), \
             mock.patch("services.system._leer_proc_stat") as m_stat, \
             mock.patch("services.system.time.sleep") as m_sleep:
            res_inmediato = system.uso_cpu()
            m_stat.assert_not_called()
            m_sleep.assert_not_called()
            self.assertEqual(res_inmediato, 50.0)

        # Subcaso B: delta_tiempo == 0.25 s exacto -> entra a ventana caliente
        with mock.patch("services.system.time.monotonic", return_value=100.25), \
             mock.patch("services.system._leer_proc_stat", return_value=(1200, 550)) as m_stat, \
             mock.patch("services.system.time.sleep") as m_sleep:
            res_borde = system.uso_cpu()
            m_stat.assert_called_once()
            m_sleep.assert_not_called()
            # delta_total = 200, inactivo_delta = 50 -> 75.0%
            self.assertEqual(res_borde, 75.0)
            self.assertEqual(system._ULTIMO_USO_CPU, 75.0)

    def test_frontera_temporal_exacta_100_segundos(self):
        """En delta_tiempo == 10.0 s debe entrar a la ventana caliente (una sola lectura, sin sleep)."""
        system._MUESTRA_CPU = (100.0, 1000, 500)
        system._ULTIMO_USO_CPU = 50.0

        with mock.patch("services.system.time.monotonic", return_value=110.0), \
             mock.patch("services.system._leer_proc_stat", return_value=(1400, 650)) as m_stat, \
             mock.patch("services.system.time.sleep") as m_sleep:
            res = system.uso_cpu()
            m_stat.assert_called_once()
            m_sleep.assert_not_called()
            # delta_total = 400, inactivo_delta = 150 -> 62.5%
            self.assertEqual(res, 62.5)
            self.assertEqual(system._ULTIMO_USO_CPU, 62.5)

    def test_frontera_temporal_mayor_100_segundos(self):
        """En delta_tiempo > 10.0 s (ej. 10.01 s) debe descartar muestra y calibrar en frío con sleep(0.2)."""
        system._MUESTRA_CPU = (100.0, 1000, 500)
        system._ULTIMO_USO_CPU = 50.0

        with mock.patch("services.system.time.monotonic", side_effect=[110.01, 110.21]), \
             mock.patch("services.system._leer_proc_stat", side_effect=[(2000, 1000), (2200, 1050)]) as m_stat, \
             mock.patch("services.system.time.sleep") as m_sleep:
            res = system.uso_cpu()
            m_sleep.assert_called_once_with(0.2)
            self.assertEqual(m_stat.call_count, 2)
            self.assertEqual(res, 75.0)
            self.assertEqual(system._ULTIMO_USO_CPU, 75.0)


class TestTelemetriaSensores(unittest.TestCase):
    """Verifica la lectura de sensores, caché NVIDIA con doble chequeo y tolerancia a fallos."""

    def setUp(self):
        self._prev_cache = system._CACHE_NVIDIA
        system._CACHE_NVIDIA = None

    def tearDown(self):
        system._CACHE_NVIDIA = self._prev_cache

    def test_leer_proc_stat_parseo(self):
        """_leer_proc_stat debe parsear jiffies correctamente de /proc/stat."""
        stat_mock = "cpu  2255 34 2290 22625563 6290 127 456 0 0 0\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=stat_mock)):
            total, inactivo = system._leer_proc_stat()
            # valores: 2255, 34, 2290, 22625563, 6290, 127, 456, 0
            # total = 22637015, inactivo = valores[3] + valores[4] = 22625563 + 6290 = 22631853
            self.assertEqual(total, 2255 + 34 + 2290 + 22625563 + 6290 + 127 + 456 + 0)
            self.assertEqual(inactivo, 22625563 + 6290)

    def test_leer_sensores_contrato_completo(self):
        """leer_sensores debe retornar un diccionario con todas las claves y tipos exactos."""
        meminfo_mock = (
            "MemTotal:       16384000 kB\n"
            "MemFree:         4096000 kB\n"
            "MemAvailable:    8192000 kB\n"
        )
        with mock.patch("builtins.open", mock.mock_open(read_data=meminfo_mock)) as mock_file, \
             mock.patch("services.system.uso_cpu", return_value=12.5), \
             mock.patch("services.system._leer_telemetria_nvidia", return_value=("20", "48")):
            # Para la lectura térmica
            mock_file.side_effect = [
                mock.mock_open(read_data=meminfo_mock).return_value,
                mock.mock_open(read_data="45000\n").return_value,
            ]
            s = system.leer_sensores()
            self.assertEqual(s["ram_total"], 16000)
            self.assertEqual(s["ram_usada"], 8000)
            self.assertEqual(s["cpu_uso"], 12.5)
            self.assertEqual(s["cpu_temp"], 45)
            self.assertEqual(s["gpu_uso"], "20")
            self.assertEqual(s["gpu_temp"], "48")

    def test_cache_nvidia_doble_chequeo_y_ttl(self):
        """La caché NVIDIA debe consultar nvidia-smi una sola vez dentro del TTL de 30 s."""
        mock_proc = mock.Mock()
        mock_proc.stdout = "25, 52\n"

        with mock.patch("services.system.subprocess.run", return_value=mock_proc) as mock_run:
            # 1. Primera llamada: consulta y cachea
            uso1, temp1 = system._leer_telemetria_nvidia()
            self.assertEqual(uso1, "25")
            self.assertEqual(temp1, "52")
            mock_run.assert_called_once()

            # 2. Llamadas inmediatas: leen de caché sin llamar subprocess.run
            uso2, temp2 = system._leer_telemetria_nvidia()
            uso3, temp3 = system._leer_telemetria_nvidia()
            self.assertEqual(uso2, "25")
            self.assertEqual(temp3, "52")
            mock_run.assert_called_once()

    def test_cache_nvidia_concurrencia_multiples_hilos(self):
        """Cinco hilos concurrentes compitiendo con la caché expirada deben invocar nvidia-smi exactamente 1 vez."""
        system._CACHE_NVIDIA = None
        mock_proc = mock.Mock()
        mock_proc.stdout = "25, 52\n"

        num_hilos = 5
        barrera_salida = threading.Barrier(num_hilos)
        evento_todos_llamaron_prelock = threading.Event()
        llamadas_prelock = 0
        lock_contador = threading.Lock()

        original_monotonic = time.monotonic

        def side_effect_monotonic():
            nonlocal llamadas_prelock
            with lock_contador:
                llamadas_prelock += 1
                if llamadas_prelock >= num_hilos:
                    evento_todos_llamaron_prelock.set()
            return original_monotonic()

        def fake_run(*args, **kwargs):
            # Esperar a que los 5 hilos hayan llamado a monotonic() (comprobación pre-lock)
            evento_todos_llamaron_prelock.wait(timeout=2.0)
            return mock_proc

        resultados = []
        errores = []

        def worker():
            try:
                barrera_salida.wait(timeout=2.0)
                res = system._leer_telemetria_nvidia()
                resultados.append(res)
            except Exception as e:
                errores.append(e)

        with mock.patch("services.system.subprocess.run", side_effect=fake_run) as mock_run, \
             mock.patch("services.system.time.monotonic", side_effect=side_effect_monotonic):
            hilos = [threading.Thread(target=worker) for _ in range(num_hilos)]
            for h in hilos:
                h.start()
            for h in hilos:
                h.join(timeout=3.0)

        # 1. Sin excepciones
        self.assertEqual(errores, [])
        # 2. Todos los hilos terminaron
        self.assertEqual(len(resultados), num_hilos)
        # 3. Exactamente una ejecución de subprocess.run (nvidia-smi)
        self.assertEqual(mock_run.call_count, 1)
        # 4. Todos los 5 hilos recibieron exactamente el mismo resultado
        for res in resultados:
            self.assertEqual(res, ("25", "52"))

    def test_cache_nvidia_expiracion_a_los_30_segundos(self):
        """Tras superar los 30 s de TTL, la siguiente llamada debe refrescar la caché."""
        mock_proc = mock.Mock()
        mock_proc.stdout = "30, 55\n"

        t_base = 2000.0
        with mock.patch("services.system.subprocess.run", return_value=mock_proc) as mock_run, \
             mock.patch("services.system.time.monotonic", side_effect=[t_base, t_base, t_base + 31.0, t_base + 31.0]):
            # 1. En t_base: consulta 1
            system._leer_telemetria_nvidia()
            self.assertEqual(mock_run.call_count, 1)

            # 2. En t_base + 31 s: expira TTL y vuelve a consultar
            system._leer_telemetria_nvidia()
            self.assertEqual(mock_run.call_count, 2)

    def test_cache_nvidia_retencion_de_fallos(self):
        """Si nvidia-smi falla, debe cachear (None, None) durante el TTL para prevenir tormentas de subprocesos."""
        t_base = 3000.0
        with mock.patch("services.system.subprocess.run", side_effect=subprocess.CalledProcessError(1, "nvidia-smi")) as mock_run, \
             mock.patch("services.system.time.monotonic", side_effect=[t_base, t_base, t_base + 5.0]):
            # 1. Fallo inicial
            uso1, temp1 = system._leer_telemetria_nvidia()
            self.assertIsNone(uso1)
            self.assertIsNone(temp1)
            self.assertEqual(mock_run.call_count, 1)

            # 2. Segunda llamada 5 s después (dentro del TTL de 30 s): no debe reintentar nvidia-smi
            uso2, temp2 = system._leer_telemetria_nvidia()
            self.assertIsNone(uso2)
            self.assertIsNone(temp2)
            self.assertEqual(mock_run.call_count, 1)

    def test_orden_telemetria_sin_anidamiento_locks(self):
        """Verifica que leer_sensores ejecute el uso_cpu real y telemetría NVIDIA real sin anidar locks."""
        class InstrumentedLock:
            def __init__(self, real_lock, check_other):
                self.real_lock = real_lock
                self.check_other = check_other
                self.adquirido = 0
                self.anidamiento = False

            def __enter__(self):
                if self.check_other().locked():
                    self.anidamiento = True
                self.adquirido += 1
                return self.real_lock.__enter__()

            def __exit__(self, *args):
                return self.real_lock.__exit__(*args)

            def locked(self):
                return self.real_lock.locked()

        real_cpu = threading.Lock()
        real_nvidia = threading.Lock()

        wrap_cpu = InstrumentedLock(real_cpu, lambda: wrap_nvidia)
        wrap_nvidia = InstrumentedLock(real_nvidia, lambda: wrap_cpu)

        meminfo_mock = "MemTotal: 8000 kB\nMemAvailable: 4000 kB\n"
        mock_proc = mock.Mock()
        mock_proc.stdout = "20, 50\n"

        system._CACHE_NVIDIA = None
        system._MUESTRA_CPU = None

        with mock.patch("services.system._LOCK_CPU", wrap_cpu), \
             mock.patch("services.system._LOCK_NVIDIA", wrap_nvidia), \
             mock.patch("builtins.open", mock.mock_open(read_data=meminfo_mock)), \
             mock.patch("services.system._leer_proc_stat", side_effect=[(1000, 500), (1200, 550)]), \
             mock.patch("services.system.time.sleep"), \
             mock.patch("services.system.subprocess.run", return_value=mock_proc):
            # Invocación real de leer_sensores: ejecuta uso_cpu() real y _leer_telemetria_nvidia() real
            s = system.leer_sensores()

        # 1. Ambos locks deben haberse adquirido realmente
        self.assertEqual(wrap_cpu.adquirido, 1, "El lock de CPU debió ser adquirido exactamente 1 vez.")
        self.assertEqual(wrap_nvidia.adquirido, 1, "El lock de NVIDIA debió ser adquirido exactamente 1 vez.")

        # 2. Ningún lock debe haberse adquirido mientras el otro estaba activo
        self.assertFalse(wrap_cpu.anidamiento, "Accidental nesting: _LOCK_CPU se adquirió mientras _LOCK_NVIDIA estaba activo.")
        self.assertFalse(wrap_nvidia.anidamiento, "Accidental nesting: _LOCK_NVIDIA se adquirió mientras _LOCK_CPU estaba activo.")

        # 3. Verificación de seguridad del detector: si se fuerza anidamiento accidental, el detector lo reporta
        with wrap_nvidia:
            with wrap_cpu:
                pass
        self.assertTrue(wrap_cpu.anidamiento, "El instrumento detector debe fallar si se introduce anidamiento de locks.")

    def test_aislamiento_fallo_nvidia_no_afecta_demas_sensores(self):
        """Un fallo total de NVIDIA no debe impedir que se entreguen RAM y CPU."""
        meminfo = "MemTotal: 8000 kB\nMemAvailable: 2000 kB\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=meminfo)), \
             mock.patch("services.system.uso_cpu", return_value=25.0), \
             mock.patch("services.system._leer_telemetria_nvidia", return_value=(None, None)):
            s = system.leer_sensores()
            self.assertEqual(s["ram_total"], 7)
            self.assertEqual(s["cpu_uso"], 25.0)
            self.assertIsNone(s["gpu_uso"])
            self.assertIsNone(s["gpu_temp"])


class TestControlEnergia(unittest.TestCase):
    """Verifica las órdenes del sistema operativo mediante control_energia()."""

    def test_control_energia_apagar(self):
        with mock.patch("services.system.subprocess.Popen") as mock_popen:
            res = system.control_energia("apagar")
            mock_popen.assert_called_once_with(["systemctl", "poweroff"])
            self.assertEqual(res, "Iniciando secuencia de apagado completo.")

    def test_control_energia_reiniciar(self):
        with mock.patch("services.system.subprocess.Popen") as mock_popen:
            res = system.control_energia("reiniciar")
            mock_popen.assert_called_once_with(["systemctl", "reboot"])
            self.assertEqual(res, "Reiniciando el sistema.")

    def test_control_energia_accion_invalida(self):
        with mock.patch("services.system.subprocess.Popen") as mock_popen:
            res = system.control_energia("suspender")
            mock_popen.assert_not_called()
            self.assertEqual(res, "Acción de energía no reconocida.")

    def test_control_energia_excepcion_manejada(self):
        with mock.patch("services.system.subprocess.Popen", side_effect=OSError("permiso denegado")):
            res = system.control_energia("apagar")
            self.assertIn("Error intentando cambiar el estado de energía: permiso denegado", res)


class TestIntegracionFachada(unittest.TestCase):
    """Verifica que madeline_server exponga los símbolos y mantenga la presentación."""

    def test_reexportacion_simbolos(self):
        """madeline_server debe reexportar los símbolos de services.system."""
        for simbolo in ["_leer_proc_stat", "uso_cpu", "leer_sensores", "control_energia"]:
            self.assertTrue(
                hasattr(ms, simbolo),
                f"El símbolo '{simbolo}' no está expuesto en madeline_server."
            )

    def test_estado_sistema_presentacion_con_y_sin_gpu(self):
        """estado_sistema() debe generar respuestas habladas en español respetando el estado de la GPU."""
        # 1. Con GPU activa
        s_con_gpu = {
            "ram_usada": 6144,
            "ram_total": 16384,
            "cpu_uso": 15.2,
            "cpu_temp": 48,
            "gpu_uso": "12",
            "gpu_temp": "45",
        }
        with mock.patch("madeline_server.leer_sensores", return_value=s_con_gpu):
            texto = ms.estado_sistema()
            self.assertIn("RAM 6,0 gigas ocupadas de 16,0", texto)
            self.assertIn("Procesador al 15% con una temperatura de 48°C", texto)
            self.assertIn("La tarjeta gráfica NVIDIA está al 12% de uso con 45°C.", texto)

        # 2. Con GPU en reposo (None)
        s_sin_gpu = dict(s_con_gpu, gpu_uso=None, gpu_temp=None)
        with mock.patch("madeline_server.leer_sensores", return_value=s_sin_gpu):
            texto = ms.estado_sistema()
            self.assertIn("La tarjeta de video dedicada está en reposo.", texto)

    def test_compatibilidad_mock_historico_control_energia(self):
        """El parche de unittest sobre madeline_server.control_energia debe funcionar limpiamente."""
        with mock.patch.object(ms, "control_energia", return_value="Apagando simulado.") as mock_ce:
            res = ms.control_energia("apagar")
            self.assertEqual(res, "Apagando simulado.")
            mock_ce.assert_called_once_with("apagar")


if __name__ == "__main__":
    unittest.main()
