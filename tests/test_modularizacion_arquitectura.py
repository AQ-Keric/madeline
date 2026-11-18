"""Pruebas de arquitectura, modularización e identidad de estado para Stage 4.1."""

import inspect
import os
import sys
import time
import unittest

import madeline_server as ms
from core import models as core_models
from core import state as core_state


class TestModularizacionArquitecturaU1(unittest.TestCase):
    """Verifica que la extracción U-1 preserve la identidad de objetos y contratos."""

    def setUp(self):
        # Guardar estado previo para restaurar limpiamente
        self._prev_historiales = {
            "pc": list(core_state.historiales_por_origen["pc"]),
            "telefono": list(core_state.historiales_por_origen["telefono"]),
        }
        self._prev_acciones = dict(core_state.acciones_locales_recientes)
        self._prev_ultimos = dict(core_state.memoria_contexto._ultimos_resultados)
        self._prev_confirmaciones = dict(core_state.memoria_contexto._confirmaciones)

        # Inicializar estado limpio para el test
        core_state.memoria_contexto.fijar_ultimo_resultado(None, "pc")
        core_state.memoria_contexto.fijar_ultimo_resultado(None, "telefono")
        core_state.memoria_contexto.fijar_confirmacion(None, "pc")
        core_state.memoria_contexto.fijar_confirmacion(None, "telefono")

    def tearDown(self):
        # Restaurar estado exacto
        core_state.historiales_por_origen["pc"] = self._prev_historiales["pc"]
        core_state.historiales_por_origen["telefono"] = self._prev_historiales["telefono"]
        core_state.acciones_locales_recientes["pc"] = self._prev_acciones["pc"]
        core_state.acciones_locales_recientes["telefono"] = self._prev_acciones["telefono"]
        core_state.memoria_contexto._ultimos_resultados = self._prev_ultimos
        core_state.memoria_contexto._confirmaciones = self._prev_confirmaciones

    def test_identidad_estricta_instancias_compartidas(self):
        """1. Las instancias expuestas por madeline_server deben ser EXACTAMENTE las mismas que en core.state."""
        self.assertIs(ms.historiales_por_origen, core_state.historiales_por_origen)
        self.assertIs(ms.acciones_locales_recientes, core_state.acciones_locales_recientes)
        self.assertIs(ms.memoria_contexto, core_state.memoria_contexto)
        self.assertIs(ms.historial_chat, core_state.historial_chat)
        self.assertIs(ms._LOCKS_ORIGEN, core_state._LOCKS_ORIGEN)

    def test_identidad_locks_por_origen_y_separacion(self):
        """2. Los locks por origen deben mantener identidad estricta y aislamiento entre PC y teléfono."""
        lock_pc_ms = ms.obtener_lock_origen("pc")
        lock_pc_state = core_state.obtener_lock_origen("pc")
        self.assertIs(lock_pc_ms, lock_pc_state)

        lock_tel_ms = ms.obtener_lock_origen("telefono")
        lock_tel_state = core_state.obtener_lock_origen("telefono")
        self.assertIs(lock_tel_ms, lock_tel_state)

        # Aislamiento mutuo: el lock de PC no es el mismo que el de teléfono
        self.assertIsNot(lock_pc_state, lock_tel_state)

    def test_mutacion_reflejada_bidireccionalmente(self):
        """3. Mutaciones realizadas a través de madeline_server o core.state deben ser idénticas e instantáneas."""
        # Mutación vía core.state -> visible en ms
        turno_test = core_models.TurnoConversacion(usuario="test_u", asistente="test_a")
        core_state.historiales_por_origen["pc"].append(turno_test)
        self.assertIn(turno_test, ms.historiales_por_origen["pc"])
        self.assertEqual(len(ms.historiales_por_origen["pc"]), len(core_state.historiales_por_origen["pc"]))

        # Mutación vía ms -> visible en core.state
        ms.memoria_contexto.fijar_ultimo_resultado("RESULTADO_U1_TEST", origen="telefono")
        self.assertEqual(core_state.memoria_contexto.obtener_ultimo_resultado("telefono"), "RESULTADO_U1_TEST")
        self.assertEqual(ms.memoria_contexto.obtener_ultimo_resultado("telefono"), "RESULTADO_U1_TEST")

    def test_compatibilidad_imports_historicos(self):
        """4. Todos los símbolos históricos deben poder importarse directamente desde madeline_server."""
        import madeline_server
        simbolos_esperados = [
            "TurnoConversacion",
            "AccionLocalReciente",
            "ChatRequest",
            "ChatResponse",
            "ComandoTexto",
            "AccionDeck",
            "_calcular_edad",
            "historiales_por_origen",
            "MAX_TURNOS",
            "acciones_locales_recientes",
            "obtener_lock_origen",
            "HistorialChatCompat",
            "historial_chat",
            "MAX_HISTORIAL",
            "MemoriaContextoDict",
            "memoria_contexto",
        ]
        for simbolo in simbolos_esperados:
            self.assertTrue(
                hasattr(madeline_server, simbolo),
                f"El símbolo '{simbolo}' no está expuesto en madeline_server.",
            )

    def test_identidad_estricta_modelos_con_fachada_y_routers(self):
        """5. Los modelos expuestos por madeline_server y routers deben ser idénticos a core.models."""
        from routers import audio as router_audio, deck as router_deck

        # Fachada madeline_server
        self.assertIs(ms.ChatRequest, core_models.ChatRequest)
        self.assertIs(ms.ChatResponse, core_models.ChatResponse)
        self.assertIs(ms.ComandoTexto, core_models.ComandoTexto)
        self.assertIs(ms.AccionDeck, core_models.AccionDeck)
        self.assertIs(ms.TurnoConversacion, core_models.TurnoConversacion)
        self.assertIs(ms.AccionLocalReciente, core_models.AccionLocalReciente)

        # Routers
        self.assertIs(router_audio.ChatRequest, core_models.ChatRequest)
        self.assertIs(router_audio.ChatResponse, core_models.ChatResponse)
        self.assertIs(router_audio.ComandoTexto, core_models.ComandoTexto)
        self.assertIs(router_deck.AccionDeck, core_models.AccionDeck)

    def test_firmas_publicas_y_contratos_intactos(self):
        """6. Las firmas de constructores y funciones deben ser idénticas a las originales."""
        sig_turno = inspect.signature(core_models.TurnoConversacion)
        self.assertEqual(list(sig_turno.parameters.keys()), ["usuario", "asistente", "timestamp"])

        sig_accion = inspect.signature(core_models.AccionLocalReciente)
        self.assertEqual(list(sig_accion.parameters.keys()), ["comando", "args", "resultado", "timestamp", "origen"])

        sig_edad = inspect.signature(core_state._calcular_edad)
        self.assertEqual(list(sig_edad.parameters.keys()), ["timestamp"])

        sig_lock = inspect.signature(core_state.obtener_lock_origen)
        self.assertEqual(list(sig_lock.parameters.keys()), ["origen"])

    def test_compatibilidad_modelos_pydantic_y_dataclasses(self):
        """7. Modelos Pydantic y dataclasses deben conservar serialización y comportamiento de timestamp."""
        # Pydantic ChatRequest
        req = core_models.ChatRequest(message="Hola")
        self.assertEqual(req.message, "Hola")
        self.assertEqual(req.model_dump(), {"message": "Hola"})

        # Pydantic ChatResponse y sincronización audio_path / audio_url
        resp = core_models.ChatResponse(response="Mundo", audio_path="/tmp/a.wav", transcripcion="Hola")
        self.assertEqual(resp.response, "Mundo")
        self.assertEqual(resp.audio_path, "/tmp/a.wav")
        self.assertEqual(resp.audio_url, "/tmp/a.wav")
        self.assertEqual(resp.transcripcion, "Hola")
        self.assertIsNone(resp.image_path)
        d = resp.model_dump()
        self.assertEqual(d["audio_path"], "/tmp/a.wav")
        self.assertEqual(d["audio_url"], "/tmp/a.wav")

        # Dataclass timestamp por defecto monotonic
        t_antes = time.monotonic()
        turno = core_models.TurnoConversacion(usuario="u", asistente="a")
        t_despues = time.monotonic()
        self.assertGreaterEqual(turno.timestamp, t_antes)
        self.assertLessEqual(turno.timestamp, t_despues)

    def test_ausencia_de_carga_duplicada_en_sys_modules(self):
        """8. core.models y core.state deben tener una única identidad y no cargarse bajo nombres alternativos."""
        # Verificar que la identidad canónica esté presente
        self.assertIn("core.models", sys.modules)
        self.assertIn("core.state", sys.modules)

        # Verificar que NO existan entradas duplicadas como server.core.models o server.core.state
        self.assertNotIn("server.core.models", sys.modules)
        self.assertNotIn("server.core.state", sys.modules)

        # Verificar que ningún archivo físico de core/ esté registrado dos veces en sys.modules
        archivos_vistos = {}
        for nombre_mod, mod in list(sys.modules.items()):
            if mod is not None and hasattr(mod, "__file__") and mod.__file__:
                ruta = os.path.realpath(mod.__file__)
                if "/server/core/" in ruta:
                    if ruta in archivos_vistos:
                        self.fail(
                            f"El archivo físico '{ruta}' fue cargado doblemente bajo los módulos: "
                            f"'{archivos_vistos[ruta]}' y '{nombre_mod}'"
                        )
                    archivos_vistos[ruta] = nombre_mod

    def test_aislamiento_contextual_post_modularizacion(self):
        """9. ultimo_resultado y confirmaciones permanecen estrictamente aisladas por origen."""
        # Aislamiento ultimo_resultado
        core_state.memoria_contexto.fijar_ultimo_resultado("RES_PC_U1", origen="pc")
        self.assertEqual(ms.memoria_contexto.obtener_ultimo_resultado("pc"), "RES_PC_U1")
        self.assertIsNone(ms.memoria_contexto.obtener_ultimo_resultado("telefono"))

        # Aislamiento confirmaciones
        core_state.memoria_contexto.fijar_confirmacion({"accion": "apagar"}, origen="pc")
        self.assertIsNotNone(ms.memoria_contexto.obtener_confirmacion("pc"))
        self.assertIsNone(ms.memoria_contexto.obtener_confirmacion("telefono"))
        self.assertEqual(ms.memoria_contexto["confirmacion_pendiente"]["accion"], "apagar")

    def test_memoria_contexto_contrato_legacy_dict_pc(self):
        """10. El acceso legacy tipo dict memoria_contexto[...] debe operar sobre PC sin afectar teléfono."""
        # Fijar confirmación en teléfono
        core_state.memoria_contexto.fijar_confirmacion({"accion": "apagar_tel"}, origen="telefono")
        # El acceso tipo dict legacy de PC debe seguir siendo None
        self.assertIsNone(ms.memoria_contexto["confirmacion_pendiente"])
        self.assertIsNone(ms.memoria_contexto.get("confirmacion_pendiente"))

        # Modificar PC vía dict legacy
        ms.memoria_contexto["confirmacion_pendiente"] = {"accion": "reiniciar_pc"}
        self.assertEqual(ms.memoria_contexto["confirmacion_pendiente"], {"accion": "reiniciar_pc"})
        # Teléfono debe conservar su propio valor intacto
        self.assertEqual(core_state.memoria_contexto.obtener_confirmacion("telefono"), {"accion": "apagar_tel"})

    def test_importacion_directa_routers_sin_ciclo(self):
        """11. routers.audio y routers.deck deben poder importarse directamente sin ciclo ni error."""
        import subprocess
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'server'); from routers import audio, deck; assert audio.router is not None; assert deck.router is not None",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Fallo al importar routers directamente:\n{res.stderr}")
