"""Tests para la Etapa 1: Confiabilidad, Verificación de Acciones y Protección contra Falsos Positivos.

Verifica:
1. Objeto Resultado: construcción, atributos, igualdad, compatibilidad con str, serialización JSON y Pydantic.
2. Verificadores reales: Hyprland, Bluetooth y Capturas de pantalla (éxito, fallo, timeouts y verificación negativa).
3. Diferenciación entre operación despachada (PENDING) y completada (SUCCESS).
4. Guardián contra falsos éxitos de Groq sin tool calls (evitando falsos positivos y negativos).
5. Protección de procesos esenciales en cerrar_aplicacion y cerrar_todo.
6. Supresión total de muletillas de confirmación en mensajes de error.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import entorno  # noqa: F401
import madeline_server as j
from resultado import EstadoResultado, Resultado, normalizar_resultado


class TestResultadoEstructurado(unittest.TestCase):
    """Condición 1: Pruebas específicas de construcción, atributos, igualdad, JSON y serialización."""

    def test_construccion_y_atributos_exito(self):
        r = Resultado(
            ok=True,
            mensaje="Operación completada con éxito.",
            evidencia={"pid": 1234, "verificado": True},
            estado=EstadoResultado.SUCCESS,
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.mensaje, "Operación completada con éxito.")
        self.assertEqual(r.evidencia, {"pid": 1234, "verificado": True})
        self.assertEqual(r.estado, EstadoResultado.SUCCESS)

    def test_estados_semanticos(self):
        r_pend = Resultado(ok=True, mensaje="Iniciando...", estado=EstadoResultado.PENDING)
        self.assertEqual(r_pend.estado, EstadoResultado.PENDING)
        self.assertTrue(r_pend.ok)

        r_conf = Resultado(ok=True, mensaje="¿Confirmas?", estado=EstadoResultado.NEEDS_CONFIRMATION)
        self.assertEqual(r_conf.estado, EstadoResultado.NEEDS_CONFIRMATION)

        r_fail = Resultado(ok=False, mensaje="Fallo de hardware", estado=EstadoResultado.FAILURE)
        self.assertEqual(r_fail.estado, EstadoResultado.FAILURE)
        self.assertFalse(r_fail.ok)

    def test_compatibilidad_con_str_y_metodos(self):
        r = Resultado(ok=True, mensaje="Cambiado al escritorio 3.", evidencia={"escritorio": 3})
        # Es instancia de str
        self.assertIsInstance(r, str)
        # Igualdad directa contra cadena
        self.assertEqual(r, "Cambiado al escritorio 3.")
        self.assertTrue(r == "Cambiado al escritorio 3.")
        self.assertFalse(r == "Otro texto")
        # Métodos de cadena
        self.assertTrue(r.startswith("Cambiado"))
        self.assertTrue(r.endswith("3."))
        self.assertIn("escritorio", r)
        self.assertEqual(len(r), len("Cambiado al escritorio 3."))
        self.assertEqual(f"Mensaje: {r}", "Mensaje: Cambiado al escritorio 3.")

    def test_igualdad_entre_resultados(self):
        r1 = Resultado(ok=True, mensaje="ok", evidencia={"x": 1}, estado=EstadoResultado.SUCCESS)
        r2 = Resultado(ok=True, mensaje="ok", evidencia={"x": 1}, estado=EstadoResultado.SUCCESS)
        r3 = Resultado(ok=False, mensaje="ok", evidencia={"x": 1}, estado=EstadoResultado.FAILURE)

        self.assertEqual(r1, r2)
        self.assertNotEqual(r1, r3)

    def test_serializacion_json_y_fastapi(self):
        r = Resultado(ok=True, mensaje="Captura lista", evidencia={"captura_id": "abc123"})
        # Serialización directa de r como string
        json_directo = json.dumps(r)
        self.assertEqual(json_directo, '"Captura lista"')

        # Serialización de r.a_dict()
        d = r.a_dict()
        self.assertEqual(d["ok"], True)
        self.assertEqual(d["estado"], "success")
        self.assertEqual(d["evidencia"], {"captura_id": "abc123"})
        self.assertEqual(json.loads(json.dumps(d)), d)

        # Serialización en modelo Pydantic de FastAPI
        chat_resp = j.ChatResponse(response=r)
        self.assertEqual(chat_resp.response, "Captura lista")

    def test_normalizar_resultado_soporta_legacy_y_tuplas(self):
        # Legacy str exitoso
        res1 = normalizar_resultado("Operación realizada con éxito.")
        self.assertTrue(res1.ok)
        self.assertEqual(res1.estado, EstadoResultado.SUCCESS)

        # Legacy str con prefijo de error
        res2 = normalizar_resultado("No pude conectar al parlante.")
        self.assertFalse(res2.ok)
        self.assertEqual(res2.estado, EstadoResultado.FAILURE)

        # Tupla (ok, mensaje, evidencia)
        res3 = normalizar_resultado((True, "Todo bien", {"pasos": 2}))
        self.assertTrue(res3.ok)
        self.assertEqual(res3.evidencia, {"pasos": 2})

        # Resultado existente se preserva sin cambios
        r_orig = Resultado(ok=True, mensaje="original", estado=EstadoResultado.PENDING)
        self.assertIs(normalizar_resultado(r_orig), r_orig)


class TestVerificadoresAcciones(unittest.TestCase):
    """Condición 2 y 6: Verificadores reales, reintentos, timeouts y verificaciones negativas."""

    @mock.patch.object(j.hyprland, "obtener_escritorio_activo")
    def test_verificar_escritorio_activo_exito_y_negativo(self, mock_activo):
        # Éxito directo
        mock_activo.return_value = 4
        ok, act = j.hyprland.verificar_escritorio_activo(4)
        self.assertTrue(ok)
        self.assertEqual(act, 4)

        # Verificación negativa (no cambió)
        mock_activo.return_value = 2
        ok, act = j.hyprland.verificar_escritorio_activo(4, reintentos=1, delay=0.01)
        self.assertFalse(ok)
        self.assertEqual(act, 2)

    @mock.patch.object(j.hyprland, "obtener_clientes")
    def test_verificar_ventana_en_escritorio(self, mock_clientes):
        mock_clientes.return_value = [
            {"class": "steam", "title": "Steam", "workspace": {"id": 3}},
            {"class": "vesktop", "title": "Discord", "workspace": {"id": 1}},
        ]
        # Steam está en el 3
        ok, v = j.hyprland.verificar_ventana_en_escritorio("steam", 3)
        self.assertTrue(ok)
        self.assertEqual(len(v), 1)

        # Steam no está en el 5
        ok, v = j.hyprland.verificar_ventana_en_escritorio("steam", 5, reintentos=1, delay=0.01)
        self.assertFalse(ok)

        # Aplicación que ni siquiera existe
        ok, v = j.hyprland.verificar_ventana_en_escritorio("gimp", 1, reintentos=0)
        self.assertFalse(ok)

    @mock.patch.object(j.subprocess, "run")
    def test_bluetoothctl_info_conexion_exito_y_fallo(self, mock_run):
        # Caso conexión exitosa
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout="Device 2C:71:FF:6D:CD:88 (public)\n\tName: Echo Dot\n\tConnected: yes\n",
        )
        self.assertTrue(j.verificar_conexion_bluetooth("2C:71:FF:6D:CD:88"))

        # Caso no conectado
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout="Device 2C:71:FF:6D:CD:88 (public)\n\tConnected: no\n",
        )
        self.assertFalse(j.verificar_conexion_bluetooth("2C:71:FF:6D:CD:88"))

    @mock.patch.object(j, "verificar_conexion_bluetooth", return_value=False)
    @mock.patch.object(j.subprocess, "run")
    def test_conectar_bluetooth_falla_con_verificacion_negativa(self, mock_run, mock_verif):
        # Simula error real de BlueZ extraído de conversaciones.jsonl
        mock_run.return_value = mock.Mock(
            returncode=1,
            stdout="Attempting to connect to 2C:71:FF:6D:CD:88\nFailed to connect: org.bluez.Error.Failed br-connection-unknown\n",
        )
        res = j.conectar_bluetooth("alexa")
        self.assertFalse(res.ok)
        self.assertEqual(res.estado, EstadoResultado.FAILURE)
        self.assertIn("No pude conectar a alexa", res.mensaje)
        self.assertNotIn("\x1b", res.mensaje)  # Sin secuencias ANSI

    @mock.patch.object(j.subprocess, "run")
    def test_conectar_bluetooth_timeout(self, mock_run):
        mock_run.side_effect = j.subprocess.TimeoutExpired(cmd=["bluetoothctl"], timeout=15)
        res = j.conectar_bluetooth("alexa")
        self.assertFalse(res.ok)
        self.assertIn("Se agotó el tiempo", res.mensaje)

    def test_captura_falla_si_archivo_no_se_crea(self):
        with mock.patch.object(j.subprocess, "run", return_value=mock.Mock(returncode=0)), \
             mock.patch.object(j, "ruta_captura_disponible", return_value=None):
            res = j.tomar_captura()
            self.assertFalse(res.ok)
            self.assertEqual(res.estado, EstadoResultado.FAILURE)
            self.assertIn("Falló el módulo de captura", res.mensaje)


class TestOperacionesDespachadasVsCompletadas(unittest.TestCase):
    """Condición 3: Diferenciar entre operación despachada (PENDING) y completada (SUCCESS)."""

    @mock.patch.object(j.subprocess, "Popen")
    @mock.patch.object(j.hyprland, "enviar_comando_socket", return_value=True)
    def test_abrir_aplicacion_devuelve_pending_no_success(self, mock_socket, mock_popen):
        res = j.abrir_aplicacion("discord")
        self.assertTrue(res.ok)
        # Debe ser PENDING porque lanzar una app GUI es asíncrono
        self.assertEqual(res.estado, EstadoResultado.PENDING)
        self.assertEqual(res.evidencia.get("despachado"), True)
        self.assertEqual(res.evidencia.get("completado"), False)
        self.assertIn("He dado la orden de abrir", res.mensaje)

    def test_abrir_aplicacion_desconocida_devuelve_failure(self):
        res = j.abrir_aplicacion("programa_inexistente_123")
        self.assertFalse(res.ok)
        self.assertEqual(res.estado, EstadoResultado.FAILURE)
        self.assertTrue(res.startswith("No pude"))


class TestProteccionProcesosEsenciales(unittest.TestCase):
    """Condición 5: Proteger herramientas de cierre de aplicaciones."""

    def test_cerrar_proceso_protegido_se_rechaza(self):
        with mock.patch.dict(j.DICCIONARIO_APPS, {"mi_bash": "bash"}), \
             mock.patch.object(j.subprocess, "run") as mock_run:
            res = j.cerrar_aplicacion("mi_bash")
            self.assertFalse(res.ok)
            self.assertEqual(res.estado, EstadoResultado.FAILURE)
            self.assertIn("proceso esencial", res.mensaje)
            mock_run.assert_not_called()

    @mock.patch.object(j, "cerrar_aplicacion")
    def test_cerrar_todo_usa_ok_y_lista_permitida(self, mock_cerrar):
        mock_cerrar.side_effect = [
            Resultado(ok=True, mensaje="Se cerró la aplicación navegador."),
            Resultado(ok=False, mensaje="Pucha, no pude cerrar minecraft."),
            Resultado(ok=True, mensaje="Se cerró la aplicación bottles."),
        ]
        res = j.cerrar_todo()
        self.assertTrue(res.ok)
        self.assertEqual(res.evidencia["cerradas"], ["navegador", "bottles"])
        self.assertIn("navegador, bottles", res.mensaje)


class TestGuardianFalsosExitosGroq(unittest.IsolatedAsyncioTestCase):
    """Condición 4: El guardián de Groq detecta intenciones de acción y evita falsos éxitos."""

    def setUp(self):
        patcher = mock.patch.object(j, "procesar_salida_de_voz", mock.AsyncMock(return_value=None))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher_rl = mock.patch.object(j, "router_local", return_value=[])
        patcher_rl.start()
        self.addCleanup(patcher_rl.stop)

    async def test_detecta_alucinacion_abrir_steam_sin_tools(self):
        # Reproducción exacta del caso en conversaciones.jsonl:
        # Usuario: "MADELINE, abro Steam." -> Groq responde sin tools afirmando que abrió Steam.
        respuesta_groq = mock.Mock(
            choices=[
                mock.Mock(
                    message=mock.Mock(
                        tool_calls=[],
                        content="¡Listo, Keric! Steam está abierto y listo para que le eches un vistazo.",
                    ),
                    finish_reason="stop",
                )
            ]
        )
        with mock.patch.object(j.client.chat.completions, "create", return_value=respuesta_groq):
            resp = await j.procesar_mensaje(j.ChatRequest(message="MADELINE, abro Steam."), origen="pc")

        # La respuesta alucinada debe ser interceptada
        self.assertNotIn("Steam está abierto", resp.response)
        self.assertIn("No pude realizar esa acción porque no se ejecutó ninguna herramienta", resp.response)

    async def test_detecta_alucinacion_reiniciar_servidor_sin_tools(self):
        respuesta_groq = mock.Mock(
            choices=[
                mock.Mock(
                    message=mock.Mock(
                        tool_calls=[],
                        content="¡Hecho, Keric! El servidor ya está reiniciado y funcionando.",
                    ),
                    finish_reason="stop",
                )
            ]
        )
        with mock.patch.object(j.client.chat.completions, "create", return_value=respuesta_groq):
            resp = await j.procesar_mensaje(j.ChatRequest(message="Reinicio el servidor."), origen="pc")

        self.assertNotIn("El servidor ya está reiniciado", resp.response)
        self.assertIn("No pude realizar esa acción porque no se ejecutó ninguna herramienta", resp.response)

    async def test_no_genera_falsos_positivos_en_conversaciones_normales(self):
        # Preguntas o charlas normales no deben ser interceptadas
        frase = "Estoy aprendiendo Flutter, ¿qué opinas?"
        respuesta_groq = mock.Mock(
            choices=[
                mock.Mock(
                    message=mock.Mock(
                        tool_calls=[],
                        content="¡Qué bien! Flutter es un framework muy potente para desarrollo multiplataforma.",
                    ),
                    finish_reason="stop",
                )
            ]
        )
        with mock.patch.object(j.client.chat.completions, "create", return_value=respuesta_groq):
            resp = await j.procesar_mensaje(j.ChatRequest(message=frase), origen="pc")

        self.assertIn("Flutter es un framework muy potente", resp.response)


class TestSupresionMuletillasEnErrores(unittest.TestCase):
    """Condición B: Ninguna respuesta debe agregar muletillas festivas a mensajes de error."""

    def test_resumir_resultados_locales_con_error_no_agrega_muletilla(self):
        # Simula fallo en conectar_bluetooth
        resultado_fallo = Resultado(
            ok=False,
            mensaje="No pude conectar a alexa: Failed to connect.",
            estado=EstadoResultado.FAILURE,
        )
        res = j.resumir_resultados_locales([("conectar_bluetooth", {"dispositivo": "alexa"}, resultado_fallo)])

        # No debe contener muletillas como "Listo.", "Sin quejarme. Esta vez.", "Facilito.", etc.
        for m in j.MULETILLAS:
            self.assertFalse(
                res.startswith(m),
                f"La respuesta '{res}' comenzó con la muletilla de éxito '{m}' ante un error",
            )
        self.assertEqual(res, "No pude conectar a alexa: Failed to connect.")

    def test_respuesta_local_con_error_no_agrega_muletilla(self):
        res_error = Resultado(ok=False, mensaje="No pude abrir 'desconocida': no está en mi lista.")
        texto = j.respuesta_local("abrir_aplicacion", {"app_name": "desconocida"}, res_error)
        self.assertEqual(texto, "No pude abrir 'desconocida': no está en mi lista.")


class TestRespuestaCapturaVariadaSinGenerico(unittest.IsolatedAsyncioTestCase):
    """Problema B: Evitar respuesta estática 'Listo, hecho.' tras capturas con Groq."""

    async def test_captura_con_groq_devuelve_frase_de_nucleos(self):
        respuesta_groq = mock.Mock(
            choices=[
                mock.Mock(
                    message=mock.Mock(
                        tool_calls=[
                            SimpleNamespace(
                                id="call_mock_captura",
                                function=SimpleNamespace(
                                    name="tomar_captura",
                                    arguments="{}",
                                )
                            )
                        ],
                        content=None,
                    ),
                    finish_reason="stop",
                )
            ]
        )
        res_captura = Resultado(
            ok=True,
            mensaje="CAPTURA_LISTA:test1234",
            evidencia={"captura_id": "test1234"},
        )
        with mock.patch.object(j, "router_local", return_value=[]), \
             mock.patch.object(j.client.chat.completions, "create", return_value=respuesta_groq), \
             mock.patch.dict(j.TOOLS_FUNCIONES, {"tomar_captura": mock.Mock(return_value=res_captura)}), \
             mock.patch.object(j, "ruta_captura_disponible", return_value="/tmp/test.jpg"), \
             mock.patch.object(j, "procesar_salida_de_voz", mock.AsyncMock(return_value=None)):
            resp = await j.procesar_mensaje(j.ChatRequest(message="MADELINE manda una captura"), origen="telefono")

        # No debe ser el fallback estático "Listo, hecho."
        self.assertNotEqual(resp.response, "Listo, hecho.")
        self.assertEqual(resp.image_path, "/captura/test1234")


if __name__ == "__main__":
    unittest.main()
