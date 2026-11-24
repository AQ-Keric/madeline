"""Pruebas unitarias y de integración para el servicio de gobernanza de recursos (ResourceManager).

Valida todas las condiciones de admisión, continuidad, concurrencia, amortiguación de caché,
tolerancia a fallos, transparencia de telemetría GPU y ausencia de falsos positivos en navegación
web y herramientas de compatibilidad (Wine/Proton/Gamescope).
"""

from concurrent.futures import ThreadPoolExecutor
import os
import time
import unittest
from unittest.mock import patch

from server.services.resource_manager import (
    DEFAULT_CPU_CRITICA_PCT,
    DEFAULT_CPU_MAX_PCT,
    DEFAULT_GPU_CRITICA_PCT,
    DEFAULT_GPU_MAX_PCT,
    DEFAULT_RAM_CRITICA_MB,
    DEFAULT_RAM_MINIMA_MB,
    DecisionAdmision,
    DecisionContinuidad,
    MetricasRecursos,
    MotivoDecision,
    ResourceManager,
    _escanear_proc_con_cortocircuito,
    fijar_resource_manager,
    obtener_resource_manager,
)


class TestResourceManager(unittest.TestCase):
    """Suite de pruebas unitarias para ResourceManager."""

    def setUp(self):
        # Mocks base por defecto: sistema saludable con 8000 MB libres, CPU 20%, sin juegos
        self.sensores_ok = {
            "ram_total": 16000,
            "ram_usada": 8000,
            "cpu_uso": 20.0,
            "gpu_uso": None,
        }
        self.ventana_normal = {"class": "kitty", "title": "Terminal"}
        self.clientes_normales = [
            {"class": "kitty", "title": "Terminal"},
            {"class": "zen", "title": "Zen Browser"},
        ]
        self.proceso_juego_detector = lambda: None

    def _crear_rm(
        self,
        fallback_habilitado=True,
        perfil="DEFAULT",
        sensores=None,
        ventana=None,
        clientes=None,
        procesos_fn=None,
        **kwargs,
    ):
        """Helper para construir un ResourceManager con dependencias inyectadas."""
        lector_sensores = lambda: sensores if sensores is not None else self.sensores_ok
        lector_ventana = lambda: ventana if ventana is not None else self.ventana_normal
        lector_clientes = lambda: clientes if clientes is not None else self.clientes_normales
        lector_procesos = procesos_fn if procesos_fn is not None else self.proceso_juego_detector

        return ResourceManager(
            fallback_habilitado=fallback_habilitado,
            perfil_inicial=perfil,
            lector_sensores=lector_sensores,
            lector_ventana_activa=lector_ventana,
            lector_clientes=lector_clientes,
            lector_procesos=lector_procesos,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # 1. Admisión con recursos suficientes
    # ------------------------------------------------------------------

    def test_01_admision_con_recursos_suficientes_retorna_ok(self):
        """Con memoria y CPU saludables, sin juegos y fallback habilitado, la admisión es aprobada."""
        rm = self._crear_rm()
        decision = rm.evaluar_admision()

        self.assertTrue(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.OK)
        self.assertEqual(decision.metricas.ram_disponible_mb, 8000)
        self.assertEqual(decision.metricas.cpu_uso_pct, 20.0)
        self.assertIsNone(decision.metricas.gpu_uso_pct)
        self.assertEqual(decision.metricas.gpu_estado, "DESCONOCIDO")
        self.assertEqual(decision.metricas.perfil_activo, "DEFAULT")
        self.assertIsNone(decision.metricas.juego_detectado)

    # ------------------------------------------------------------------
    # 2. Rechazo por perfil JUEGO
    # ------------------------------------------------------------------

    def test_02a_rechazo_por_perfil_juego(self):
        """Si el perfil activo es 'JUEGO', la admisión es denegada."""
        rm = self._crear_rm(perfil="JUEGO")
        decision = rm.evaluar_admision()

        self.assertFalse(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.PERFIL_JUEGO)
        self.assertIn("perfil de juego activo", decision.detalle)

    def test_02b_rechazo_por_variantes_de_perfil_juego(self):
        """Perfiles como 'modo juego', 'perfil_mmo' o 'gaming' se reconocen como modo juego."""
        for p in ["modo juego", "modo_juego", "perfil_mmo", "perfil_juego", "gaming"]:
            rm = self._crear_rm(perfil=p)
            decision = rm.evaluar_admision()
            self.assertFalse(decision.admitida, f"Falló para perfil '{p}'")
            self.assertEqual(decision.motivo, MotivoDecision.PERFIL_JUEGO)

    # ------------------------------------------------------------------
    # 3. Ausencia de Falsos Positivos en Navegadores y Capas de Compatibilidad
    # ------------------------------------------------------------------

    def test_03a_falso_positivo_vino_wine_en_navegador_no_bloquea(self):
        """Pestaña de navegador con 'Guía de WineHQ para Linux' no debe bloquear inferencias."""
        ventana_web = {"class": "zen", "title": "Guía de WineHQ para Linux"}
        rm = self._crear_rm(ventana=ventana_web)
        decision = rm.evaluar_admision()

        self.assertTrue(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.OK)

    def test_03b_falso_positivo_soberania_en_navegador_no_bloquea(self):
        """Pestaña con 'Artículo sobre soberanía nacional' no coincide con juego 'sober'."""
        ventana_web = {"class": "firefox", "title": "Artículo sobre soberanía nacional y territorio"}
        rm = self._crear_rm(ventana=ventana_web)
        decision = rm.evaluar_admision()

        self.assertTrue(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.OK)

    def test_03c_falso_positivo_celeste_en_youtube_no_bloquea(self):
        """Pestaña viendo 'Video de Celeste en YouTube' en navegador no es un proceso de juego."""
        ventana_web = {"class": "zen-browser", "title": "Video de Celeste en YouTube"}
        rm = self._crear_rm(ventana=ventana_web)
        decision = rm.evaluar_admision()

        self.assertTrue(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.OK)

    def test_03d_falso_positivo_hades_en_wikipedia_no_bloquea(self):
        """Pestaña leyendo 'Mito del dios Hades - Wikipedia' no es un proceso de juego."""
        ventana_web = {"class": "chrome", "title": "Mito del dios Hades - Wikipedia"}
        rm = self._crear_rm(ventana=ventana_web)
        decision = rm.evaluar_admision()

        self.assertTrue(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.OK)

    def test_03e_app_productividad_en_wine_con_wineserver_no_bloquea(self):
        """Una aplicación de Windows como Foobar2000 con wineserver activo no es un juego."""
        ventana_foobar = {"class": "foobar2000.exe", "title": "foobar2000 v2.0"}
        # wineserver activo pero no hay ningún proceso de juego
        rm = self._crear_rm(ventana=ventana_foobar, procesos_fn=lambda: None)
        decision = rm.evaluar_admision()

        self.assertTrue(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.OK)

    def test_03f_gamescope_aislado_sin_juego_no_bloquea(self):
        """Gamescope corriendo como compositor de escalado sin un juego dentro no bloquea."""
        ventana_gamescope = {"class": "gamescope", "title": "Nested Desktop"}
        rm = self._crear_rm(ventana=ventana_gamescope, procesos_fn=lambda: None)
        decision = rm.evaluar_admision()

        self.assertTrue(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.OK)

    # ------------------------------------------------------------------
    # 4. Detección Efectiva de Juegos Reales (Foco, Segundo Plano y Procesos)
    # ------------------------------------------------------------------

    def test_04a_juego_real_celeste_detectado_por_clase(self):
        """Juego nativo Celeste con clase 'Celeste' es detectado y deniega admisión."""
        ventana_celeste = {"class": "Celeste", "title": "Celeste"}
        rm = self._crear_rm(ventana=ventana_celeste)
        decision = rm.evaluar_admision()

        self.assertFalse(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.JUEGO_EN_EJECUCION)
        self.assertIn("clase_juego:celeste", decision.detalle)

    def test_04b_juego_real_steam_app_detectado_por_patron(self):
        """Juego de Steam en Wayland/XWayland (steam_app_1145360 = Hades) es detectado."""
        ventana_hades = {"class": "steam_app_1145360", "title": "Hades"}
        rm = self._crear_rm(ventana=ventana_hades)
        decision = rm.evaluar_admision()

        self.assertFalse(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.JUEGO_EN_EJECUCION)
        self.assertIn("steam_app_1145360", decision.detalle)

    def test_04c_juego_real_battlefield_detectado_en_segundo_plano(self):
        """Battlefield 4 en cliente Hyprland en segundo plano es detectado."""
        clientes = [
            {"class": "kitty", "title": "Terminal"},
            {"class": "battlefield 4", "title": "Battlefield 4"},
        ]
        rm = self._crear_rm(ventana=self.ventana_normal, clientes=clientes)
        decision = rm.evaluar_admision()

        self.assertFalse(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.JUEGO_EN_EJECUCION)
        self.assertIn("cliente_hyprland", decision.detalle)

    def test_04d_juego_real_detectado_por_proceso_proc(self):
        """Proceso nativo de juego 'terraria' detectado vía lector de procesos."""
        rm = self._crear_rm(procesos_fn=lambda: "terraria")
        decision = rm.evaluar_admision()

        self.assertFalse(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.JUEGO_EN_EJECUCION)
        self.assertIn("proceso:terraria", decision.detalle)

    def test_04e_juego_real_sober_roblox_detectado(self):
        """Sober (Roblox) ejecutándose como proceso es detectado."""
        rm = self._crear_rm(procesos_fn=lambda: "sober")
        decision = rm.evaluar_admision()

        self.assertFalse(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.JUEGO_EN_EJECUCION)
        self.assertIn("proceso:sober", decision.detalle)

    # ------------------------------------------------------------------
    # 5. Optimización de /proc: Cortocircuito, Amortiguación de Caché y TTL
    # ------------------------------------------------------------------

    def test_05a_cache_proc_reutiliza_resultado_dentro_del_ttl(self):
        """Consultas dentro del TTL de procesos (1.5 s) retornan el resultado cacheado."""
        llamadas = {"cuenta": 0}

        def mock_lector():
            llamadas["cuenta"] += 1
            return None

        rm = self._crear_rm(procesos_fn=mock_lector, ttl_procesos_segundos=1.5)

        # Primera consulta: escaneo real
        juego1, desde_cache1 = rm.detectar_juego_activo()
        self.assertIsNone(juego1)
        self.assertFalse(desde_cache1)
        self.assertEqual(llamadas["cuenta"], 1)

        # Segunda consulta inmediata: debe resolver desde caché
        juego2, desde_cache2 = rm.detectar_juego_activo()
        self.assertIsNone(juego2)
        self.assertTrue(desde_cache2)
        self.assertEqual(llamadas["cuenta"], 1)  # No volvió a invocar el lector

    def test_05b_cache_proc_expira_tras_el_ttl(self):
        """Al superar el TTL, la caché de procesos expira y realiza un nuevo escaneo."""
        llamadas = {"cuenta": 0}

        def mock_lector():
            llamadas["cuenta"] += 1
            return None

        rm = self._crear_rm(procesos_fn=mock_lector, ttl_procesos_segundos=0.05)

        rm.detectar_juego_activo()
        self.assertEqual(llamadas["cuenta"], 1)

        # Esperar expiración del TTL
        time.sleep(0.06)

        juego, desde_cache = rm.detectar_juego_activo()
        self.assertFalse(desde_cache)
        self.assertEqual(llamadas["cuenta"], 2)

    def test_05c_forzar_refresco_ignora_cache_proc(self):
        """forzar_refresco=True fuerza la reevaluación sin esperar al TTL."""
        llamadas = {"cuenta": 0}

        def mock_lector():
            llamadas["cuenta"] += 1
            return None

        rm = self._crear_rm(procesos_fn=mock_lector, ttl_procesos_segundos=10.0)

        rm.detectar_juego_activo()
        self.assertEqual(llamadas["cuenta"], 1)

        # Forzar refresco
        _, desde_cache = rm.detectar_juego_activo(forzar_refresco=True)
        self.assertFalse(desde_cache)
        self.assertEqual(llamadas["cuenta"], 2)

    def test_05d_escanear_proc_con_cortocircuito_maneja_errores_efimeros(self):
        """_escanear_proc_con_cortocircuito maneja procesos que desaparecen y errores de acceso."""
        with patch("os.listdir", return_value=["1", "2", "3", "not_digit"]), \
             patch("builtins.open") as mock_open:
            # Simular que el PID 1 desaparece (FileNotFoundError)
            # El PID 2 da PermissionError
            # El PID 3 retorna un juego real ('celeste')
            mock_open.side_effect = [
                FileNotFoundError("Proceso terminó"),
                PermissionError("Sin permisos de lectura"),
                unittest.mock.mock_open(read_data="celeste\n").return_value,
            ]

            juego = _escanear_proc_con_cortocircuito({"celeste"})
            self.assertEqual(juego, "celeste")

    def test_05e_integracion_real_contra_proc_del_sistema(self):
        """Prueba de integración real contra el procfs del entorno de pruebas.

        Verifica que el escaneo real no arroja excepciones, procesa entradas numéricas
        y retorna de forma acotada.
        """
        juegos_prueba = {"juego_imposible_de_prueba_xyz_123"}
        t0 = time.perf_counter()
        resultado = _escanear_proc_con_cortocircuito(juegos_prueba)
        dt_ms = (time.perf_counter() - t0) * 1000

        # No debe haber juego con ese nombre
        self.assertIsNone(resultado)
        # La medición debe completarse en tiempo acotado
        self.assertLess(dt_ms, 500.0, f"Escaneo de procfs excedió límite seguro: {dt_ms:.2f} ms")

    # ------------------------------------------------------------------
    # 6. Telemetría GPU: Diferenciación de Estado Desconocido vs. Libre
    # ------------------------------------------------------------------

    def test_06a_gpu_telemetria_none_indica_estado_desconocido_y_no_libre(self):
        """gpu_uso=None no se describe como libre ni aislada; detalla estado no disponible."""
        sensores_sin_gpu = {
            "ram_total": 16000,
            "ram_usada": 8000,
            "cpu_uso": 20.0,
            "gpu_uso": None,
        }
        rm = self._crear_rm(sensores=sensores_sin_gpu)
        decision = rm.evaluar_admision()

        self.assertTrue(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.OK)
        self.assertEqual(decision.metricas.gpu_estado, "DESCONOCIDO")
        self.assertIsNone(decision.metricas.gpu_uso_pct)
        self.assertIn("Telemetría GPU no disponible o desconocida", decision.detalle)
        self.assertIn("U-4.3B", decision.detalle)

    def test_06b_gpu_activa_dentro_del_limite_marca_disponible(self):
        """gpu_uso numérico válido marca estado DISPONIBLE y aprueba si <= 15%."""
        sensores_gpu_ok = {
            "ram_total": 16000,
            "ram_usada": 8000,
            "cpu_uso": 20.0,
            "gpu_uso": "8%",
        }
        rm = self._crear_rm(sensores=sensores_gpu_ok)
        decision = rm.evaluar_admision()

        self.assertTrue(decision.admitida)
        self.assertEqual(decision.metricas.gpu_estado, "DISPONIBLE")
        self.assertEqual(decision.metricas.gpu_uso_pct, 8.0)

    def test_06c_gpu_activa_saturada_rechaza_admision(self):
        """gpu_uso > 15% deniega admisión con motivo GPU_SATURADA."""
        sensores_gpu_alta = {
            "ram_total": 16000,
            "ram_usada": 8000,
            "cpu_uso": 20.0,
            "gpu_uso": "35",
        }
        rm = self._crear_rm(sensores=sensores_gpu_alta)
        decision = rm.evaluar_admision()

        self.assertFalse(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.GPU_SATURADA)
        self.assertEqual(decision.metricas.gpu_estado, "DISPONIBLE")

    def test_06d_gpu_telemetria_corrupta_rechaza_con_error_telemetria(self):
        """Si el dato de GPU no es convertible a número, deniega de forma conservadora."""
        sensores_gpu_corrupta = {
            "ram_total": 16000,
            "ram_usada": 8000,
            "cpu_uso": 20.0,
            "gpu_uso": "valor_no_parseable_xyz",
        }
        rm = self._crear_rm(sensores=sensores_gpu_corrupta)
        decision = rm.evaluar_admision()

        self.assertFalse(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.ERROR_TELEMETRIA)

    # ------------------------------------------------------------------
    # 7. Memoria y CPU: Admisión y Límites
    # ------------------------------------------------------------------

    def test_07a_rechazo_por_memoria_insuficiente(self):
        """Si la RAM disponible es menor a 4500 MB (ej. 3800 MB), deniega admisión."""
        sensores_poca_ram = {
            "ram_total": 16000,
            "ram_usada": 12200,  # 3800 MB libres
            "cpu_uso": 15.0,
            "gpu_uso": None,
        }
        rm = self._crear_rm(sensores=sensores_poca_ram)
        decision = rm.evaluar_admision()

        self.assertFalse(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.MEMORIA_INSUFICIENTE)
        self.assertIn("3800 MB", decision.detalle)

    def test_07b_rechazo_por_saturacion_cpu(self):
        """Si el uso de CPU es mayor a 70.0% (ej. 78.4%), deniega admisión."""
        sensores_cpu_alta = {
            "ram_total": 16000,
            "ram_usada": 8000,
            "cpu_uso": 78.4,
            "gpu_uso": None,
        }
        rm = self._crear_rm(sensores=sensores_cpu_alta)
        decision = rm.evaluar_admision()

        self.assertFalse(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.CPU_SATURADA)
        self.assertIn("78.4%", decision.detalle)

    def test_07c_fallback_deshabilitado_rechaza(self):
        """Si fallback_habilitado=False, deniega admisión inmediatamente."""
        rm = self._crear_rm(fallback_habilitado=False)
        decision = rm.evaluar_admision()

        self.assertFalse(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.FALLBACK_DESHABILITADO)

    # ------------------------------------------------------------------
    # 8. Evaluación de Continuidad: Cambios Dinámicos y Cancelación
    # ------------------------------------------------------------------

    def test_08a_continuidad_exitosa_condiciones_estables(self):
        """Continuidad aprobada cuando recursos y estado se mantienen estables."""
        rm = self._crear_rm()
        continuidad = rm.evaluar_continuidad()

        self.assertTrue(continuidad.continuar)
        self.assertFalse(continuidad.debe_cancelar)
        self.assertEqual(continuidad.motivo, MotivoDecision.OK)

    def test_08b_continuidad_cancela_si_perfil_cambia_a_juego(self):
        """Si durante la inferencia el perfil cambia a 'JUEGO', debe cancelarse."""
        rm = self._crear_rm(perfil="DEFAULT")
        self.assertTrue(rm.evaluar_continuidad().continuar)

        rm.fijar_perfil("JUEGO")
        continuidad = rm.evaluar_continuidad()

        self.assertFalse(continuidad.continuar)
        self.assertTrue(continuidad.debe_cancelar)
        self.assertEqual(continuidad.motivo, MotivoDecision.PERFIL_JUEGO)

    def test_08c_continuidad_cancela_si_fallback_se_desactiva(self):
        """Si el fallback se desactiva durante la inferencia, se ordena cancelación."""
        rm = self._crear_rm(fallback_habilitado=True)
        self.assertTrue(rm.evaluar_continuidad().continuar)

        rm.fijar_fallback_habilitado(False)
        continuidad = rm.evaluar_continuidad()

        self.assertFalse(continuidad.continuar)
        self.assertEqual(continuidad.motivo, MotivoDecision.FALLBACK_DESHABILITADO)

    def test_08d_continuidad_cancela_por_ram_critica(self):
        """Si la RAM disponible cae por debajo de 1500 MB (ej. 1300 MB), cancela."""
        sensores_criticos = {
            "ram_total": 16000,
            "ram_usada": 14700,  # 1300 MB libres
            "cpu_uso": 30.0,
            "gpu_uso": None,
        }
        rm = self._crear_rm(sensores=sensores_criticos)
        continuidad = rm.evaluar_continuidad()

        self.assertFalse(continuidad.continuar)
        self.assertEqual(continuidad.motivo, MotivoDecision.MEMORIA_CRITICA)

    def test_08e_continuidad_cancela_por_cpu_critica(self):
        """Si la CPU supera el límite crítico de continuidad (95.0%), cancela."""
        sensores_cpu_saturada = {
            "ram_total": 16000,
            "ram_usada": 8000,
            "cpu_uso": 98.2,
            "gpu_uso": None,
        }
        rm = self._crear_rm(sensores=sensores_cpu_saturada)
        continuidad = rm.evaluar_continuidad()

        self.assertFalse(continuidad.continuar)
        self.assertEqual(continuidad.motivo, MotivoDecision.CPU_CRITICA)

    def test_08f_continuidad_permite_cpu_moderada_en_generacion(self):
        """Durante la generación Ollama usará CPU (ej. 78%), lo cual es admisible si < 95%."""
        sensores_inferencia = {
            "ram_total": 16000,
            "ram_usada": 8000,
            "cpu_uso": 78.0,
            "gpu_uso": None,
        }
        rm = self._crear_rm(sensores=sensores_inferencia)
        continuidad = rm.evaluar_continuidad()

        self.assertTrue(continuidad.continuar)
        self.assertEqual(continuidad.motivo, MotivoDecision.OK)

    # ------------------------------------------------------------------
    # 9. Tolerancia a Fallos y Errores de Sensores
    # ------------------------------------------------------------------

    def test_09a_error_de_sensores_lanza_excepcion_deniega_admision(self):
        """Fallo de lectura en /proc/meminfo o sensores deniega admisión conservadoramente."""
        def lector_roto():
            raise OSError("Fallo en /proc/meminfo")

        rm = ResourceManager(fallback_habilitado=True, lector_sensores=lector_roto)
        decision = rm.evaluar_admision()

        self.assertFalse(decision.admitida)
        self.assertEqual(decision.motivo, MotivoDecision.ERROR_SENSORES)

    def test_09b_error_de_sensores_en_continuidad_cancela(self):
        """Fallo de sensores durante continuidad cancela de forma conservadora."""
        def lector_roto():
            raise RuntimeError("Fallo de bus I2C")

        rm = ResourceManager(fallback_habilitado=True, lector_sensores=lector_roto)
        continuidad = rm.evaluar_continuidad()

        self.assertFalse(continuidad.continuar)
        self.assertEqual(continuidad.motivo, MotivoDecision.ERROR_SENSORES)

    # ------------------------------------------------------------------
    # 10. Concurrencia y Thread-Safety
    # ------------------------------------------------------------------

    def test_10_concurrencia_multihilo_sin_carreras(self):
        """Múltiples hilos llamando a evaluar_admision y evaluar_continuidad concurrentemente."""
        rm = self._crear_rm()
        errores = []

        def worker(idx):
            try:
                for i in range(25):
                    if i % 5 == 0:
                        rm.fijar_perfil("JUEGO" if i % 2 == 0 else "DEFAULT")
                    if i % 7 == 0:
                        rm.fijar_fallback_habilitado(i % 3 != 0)
                    adm = rm.evaluar_admision()
                    cont = rm.evaluar_continuidad()
                    self.assertIsInstance(adm, DecisionAdmision)
                    self.assertIsInstance(cont, DecisionContinuidad)
            except Exception as e:
                errores.append(e)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker, i) for i in range(8)]
            for f in futures:
                f.result()

        self.assertEqual(errores, [], f"Hubo errores de concurrencia: {errores}")

    # ------------------------------------------------------------------
    # 11. Instancia Global (Singleton Helper)
    # ------------------------------------------------------------------

    def test_11_singleton_global_y_restablecimiento(self):
        """obtener_resource_manager y fijar_resource_manager operan correctamente."""
        rm_original = obtener_resource_manager()
        self.assertIsInstance(rm_original, ResourceManager)

        rm_custom = self._crear_rm(perfil="CUSTOM")
        fijar_resource_manager(rm_custom)
        self.assertIs(obtener_resource_manager(), rm_custom)
        self.assertEqual(obtener_resource_manager().obtener_perfil(), "CUSTOM")

        # Restaurar
        fijar_resource_manager(rm_original)
        self.assertIs(obtener_resource_manager(), rm_original)


if __name__ == "__main__":
    unittest.main()
