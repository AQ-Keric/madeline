"""Pruebas unitarias para la ampliación del Router Local de Madeline (Etapa 2).

Cubre:
1. Capturas de pantalla (variaciones naturales, clíticos).
2. Escritorios virtuales (cambio directo y relativo).
3. Movimiento de ventanas (apps nombradas y ventana activa).
4. Diferenciación estricta entre cambiar escritorio y mover ventana.
5. Aplicaciones (1ra persona, imperativo, perífrasis).
6. Música y streaming (pedidos vagos, controles directos, Spotify).
7. Reinicio de servicios (1ra persona, cortesía, imperativo).
8. Bluetooth (on/off, dispositivos específicos, 1ra persona y solicitud de aclaración si es ambiguo).
9. Filtro de seguridad: preguntas explicativas e hipótesis nunca ejecutan acciones.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.dirname(__file__))

import madeline_server as ms
from resultado import EstadoResultado, Resultado


class TestRouterCapturas(unittest.TestCase):
    def test_variaciones_captura(self):
        frases = [
            "Mándame una captura",
            "Manda una captura",
            "Sácame un pantallazo",
            "sacame un pantallazo",
            "Quiero ver cómo está la pantalla",
            "quiero ver la pantalla",
            "Haz una captura de pantalla",
            "Tómame una captura",
            "Envía una captura",
        ]
        for f in frases:
            with self.subTest(frase=f):
                comandos = ms.router_local(f)
                self.assertEqual(comandos, [("captura", {})], f"Falló en: {f}")


class TestRouterEscritorios(unittest.TestCase):
    def test_cambio_escritorio_absoluto_y_relativo(self):
        casos = [
            ("Cambia al escritorio 3", 3),
            ("cambia al workspace 2", 2),
            ("Pásame al escritorio 2", 2),
            ("pasame al escritorio 5", 5),
            ("Muéstrame el escritorio 4", 4),
            ("muestrame el escritorio 1", 1),
            ("Muévete al escritorio 3", 3),
            ("muevete al workspace 6", 6),
            ("Ve al escritorio anterior", "anterior"),
            ("vete al escritorio previo", "previo"),
            ("Ve al escritorio siguiente", "siguiente"),
            ("vete al escritorio proximo", "proximo"),
            ("Pasa al escritorio 2", 2),
            ("paso al escritorio 4", 4),
        ]
        for f, num_esperado in casos:
            with self.subTest(frase=f):
                comandos = ms.router_local(f)
                self.assertEqual(
                    comandos,
                    [("cambiar_escritorio", {"escritorio": num_esperado})],
                    f"Falló en: {f}",
                )

    def test_ejecucion_cambiar_escritorio_relativo(self):
        # Con escritorio activo = 3, 'anterior' debe ir al 2 y 'siguiente' al 4
        with mock.patch.object(ms.hyprland, "obtener_escritorio_activo", return_value=3), \
             mock.patch.object(ms.hyprland, "enviar_comando_socket", return_value="ok"):
            res_ant = ms.cambiar_escritorio("anterior")
            self.assertTrue(res_ant.ok)
            self.assertEqual(res_ant.evidencia["escritorio"], 2)

            res_sig = ms.cambiar_escritorio("siguiente")
            self.assertTrue(res_sig.ok)
            self.assertEqual(res_sig.evidencia["escritorio"], 4)


class TestRouterMovimientoVentanas(unittest.TestCase):
    def test_mover_ventana_especifica(self):
        casos = [
            ("Mueve Steam al escritorio 2", "steam", 2),
            ("Pasa Firefox al escritorio 3", "firefox", 3),
            ("Lleva Discord al escritorio 1", "discord", 1),
            ("Manda Telegram al escritorio 4", "telegram", 4),
        ]
        for f, app_esp, esc_esp in casos:
            with self.subTest(frase=f):
                comandos = ms.router_local(f)
                self.assertEqual(
                    comandos,
                    [("mover_ventana", {"app_name": app_esp, "escritorio": esc_esp})],
                    f"Falló en: {f}",
                )

    def test_mover_esta_ventana_activa(self):
        casos = [
            ("Manda esta ventana al escritorio 4", "esta ventana", 4),
            ("Mueve esta ventana al escritorio 2", "esta ventana", 2),
            ("Pasa esta ventana al escritorio 3", "esta ventana", 3),
            ("Manda la ventana al escritorio anterior", "esta ventana", "anterior"),
        ]
        for f, app_esp, esc_esp in casos:
            with self.subTest(frase=f):
                comandos = ms.router_local(f)
                self.assertEqual(
                    comandos,
                    [("mover_ventana", {"app_name": app_esp, "escritorio": esc_esp})],
                    f"Falló en: {f}",
                )

    def test_diferenciacion_estricta_escritorio_vs_ventana(self):
        # NUNCA deben confundirse cambiar escritorio activo con mover una ventana
        # 1. Cambiar escritorio: NO menciona ventana ni app
        cmds_esc = ms.router_local("Cambia al escritorio 3")
        self.assertEqual(cmds_esc, [("cambiar_escritorio", {"escritorio": 3})])
        self.assertNotIn("mover_ventana", [nombre for nombre, _ in cmds_esc])

        # 2. Mover ventana: menciona app o "esta ventana"
        cmds_win = ms.router_local("Mueve Steam al escritorio 3")
        self.assertEqual(cmds_win, [("mover_ventana", {"app_name": "steam", "escritorio": 3})])
        self.assertNotIn("cambiar_escritorio", [nombre for nombre, _ in cmds_win])

    def test_ejecucion_mover_esta_ventana_activa(self):
        # Mover 'esta ventana' debe resolver la ventana activa vía hyprland
        with mock.patch.object(ms.hyprland, "obtener_ventana_activa", return_value={"class": "kitty", "title": "terminal"}), \
             mock.patch.object(ms.hyprland, "enviar_comando_socket", return_value=True):
            res = ms.mover_ventana("esta ventana", 4)
            self.assertTrue(res.ok)
            self.assertEqual(res.evidencia["clase"], "kitty")
            self.assertEqual(res.evidencia["escritorio"], 4)

    def test_ejecucion_mover_ventana_destino_relativo(self):
        # Mover ventana a destino relativo con escritorio activo = 2
        with mock.patch.object(ms.hyprland, "obtener_escritorio_activo", return_value=2), \
             mock.patch.object(ms.hyprland, "enviar_comando_socket", return_value=True):
            res = ms.mover_ventana("steam", "siguiente")
            self.assertTrue(res.ok)
            self.assertEqual(res.evidencia["escritorio"], 3)


class TestRouterAplicaciones(unittest.TestCase):
    def test_abrir_y_cerrar_variaciones(self):
        casos = [
            ("Abre Steam", [("abrir_aplicacion", {"app_name": "steam"})]),
            ("Abro Steam", [("abrir_aplicacion", {"app_name": "steam"})]),
            ("Quiero abrir Firefox", [("abrir_aplicacion", {"app_name": "firefox"})]),
            ("Voy a abrir Discord", [("abrir_aplicacion", {"app_name": "discord"})]),
            ("Cierra Discord", [("cerrar_aplicacion", {"app_name": "discord"})]),
            ("Cierro Discord", [("cerrar_aplicacion", {"app_name": "discord"})]),
            ("Quiero cerrar Firefox", [("cerrar_aplicacion", {"app_name": "firefox"})]),
            ("Cierra Steam y Discord", [
                ("cerrar_aplicacion", {"app_name": "steam"}),
                ("cerrar_aplicacion", {"app_name": "discord"}),
            ]),
        ]
        for f, esp in casos:
            with self.subTest(frase=f):
                self.assertEqual(ms.router_local(f), esp)


class TestRouterMusica(unittest.TestCase):
    def test_pedidos_musica_y_control(self):
        casos = [
            ("Pon música", [("radio_musica", {})]),
            ("¿Puedes poner música?", [("radio_musica", {})]),
            ("¿Quieres poner algo de música?", [("radio_musica", {})]),
            ("Madeline, con algo de música", [("radio_musica", {})]),
            ("Pausa Spotify", [("multimedia", {"accion": "pausar"})]),
            ("Siguiente canción", [("multimedia", {"accion": "siguiente"})]),
            ("Reanuda la música", [("multimedia", {"accion": "reproducir"})]),
            ("despausa spotify", [("multimedia", {"accion": "reproducir"})]),
        ]
        for f, esp in casos:
            with self.subTest(frase=f):
                self.assertEqual(ms.router_local(f), esp)


class TestRouterReinicioYServicios(unittest.TestCase):
    def test_reinicio_servidor(self):
        frases = [
            "Reinicia el servidor",
            "Reinicie el servidor",
            "Quiero reiniciar Madeline",
            "Reinicio el servidor",
            "voy a reiniciar el servidor",
            "MADELINE, reinicie el servidor.",
        ]
        for f in frases:
            with self.subTest(frase=f):
                self.assertEqual(ms.router_local(f), [("reiniciar_servidor", {})])


class TestRouterBluetooth(unittest.TestCase):
    def test_bluetooth_acciones(self):
        casos = [
            ("Desconéctate de la Alexa", [("desconectar_bluetooth", {"dispositivo": "alexa"})]),
            ("Desconecto de la Alexa", [("desconectar_bluetooth", {"dispositivo": "alexa"})]),
            ("Conecta los audífonos", [("conectar_bluetooth", {"dispositivo": "audífonos"})]),
            ("Conecto los audífonos", [("conectar_bluetooth", {"dispositivo": "audífonos"})]),
            ("Desconecta el Bluetooth", [("bluetooth", {"accion": "apagar"})]),
            ("Prende el Bluetooth", [("bluetooth", {"accion": "encender"})]),
        ]
        for f, esp in casos:
            with self.subTest(frase=f):
                self.assertEqual(ms.router_local(f), esp)

    def test_bluetooth_ambiguo_solicita_aclaracion(self):
        # Si el usuario dice 'Desconéctate' o 'Conéctate' sin dispositivo,
        # Madeline NO debe inventar ni elegir arbitrariamente un dispositivo:
        # debe pedir aclaración.
        frases_desconectar = [
            "Desconéctate",
            "desconéctate por favor",
            "Madeline desconéctate",
        ]
        for f in frases_desconectar:
            with self.subTest(frase=f):
                res = ms.router_local(f)
                self.assertEqual(res, [("charla", {"tipo": "aclaracion_bt_desconectar"})])
                texto_resp = ms.ejecutar_comando_local("charla", {"tipo": "aclaracion_bt_desconectar"})
                self.assertIn("dispositivo", texto_resp.lower())

        frases_conectar = [
            "Conéctate",
            "conéctate por favor",
            "Madeline conéctate",
        ]
        for f in frases_conectar:
            with self.subTest(frase=f):
                res = ms.router_local(f)
                self.assertEqual(res, [("charla", {"tipo": "aclaracion_bt_conectar"})])
                texto_resp = ms.ejecutar_comando_local("charla", {"tipo": "aclaracion_bt_conectar"})
                self.assertIn("dispositivo", texto_resp.lower())


class TestSeguridadPreguntasExplicativasEHipotesis(unittest.TestCase):
    def test_preguntas_explicativas_no_ejecutan_acciones(self):
        # Preguntas sobre cómo hacer algo, conceptuales o hipotéticas NO deben ejecutar nada
        preguntas = [
            # Capturas
            "¿Cómo tomo una captura?",
            "¿Para qué sirve tomar una captura?",
            "Si saco una captura, ¿dónde se guarda?",
            # Escritorios
            "¿Cómo cambio al escritorio 3?",
            "¿Por qué no puedo cambiar al escritorio 3?",
            "¿Cómo se cambia de escritorio?",
            "¿Para qué sirve el escritorio 2?",
            "Si cambio al escritorio 3, ¿se cierran las aplicaciones?",
            # Ventanas
            "¿Cómo muevo Steam al escritorio 2?",
            "¿Cómo puedo mover una ventana?",
            "Si muevo Steam al escritorio 2, ¿qué pasa?",
            "Mueve ventanas entre escritorios.",
            # Apps
            "¿Cómo se abre Steam?",
            "¿Cómo abro Steam?",
            "¿Por qué se cerró Discord?",
            "Si cierro Discord, ¿se pierden los mensajes?",
            # Reinicio
            "¿Cómo reinicio el servidor?",
            "¿Qué pasa si reinicio el servidor?",
            "¿Por qué reiniciarías el servidor?",
            # Bluetooth
            "¿Cómo me desconecto de la Alexa?",
            "¿Por qué se desconectó el Bluetooth?",
            "¿Qué pasa si desconecto el Bluetooth?",
            # Otras explicativas
            "Explícame cómo cambiar de escritorio",
            "Dime cómo puedo abrir Spotify",
        ]
        for p in preguntas:
            with self.subTest(pregunta=p):
                comandos = ms.router_local(p)
                self.assertEqual(comandos, [], f"Se ejecutó una acción incorrectamente para: '{p}' -> {comandos}")

    def test_preguntas_de_clima_legitimas_no_son_bloqueadas(self):
        # Asegurar que 'dime cómo está el clima' NO se confunda con una explicativa
        frases_clima = [
            "Dime cómo está el clima, porfa.",
            "MADELINE, dime cómo está el clima.",
            "¿Cómo está el tiempo?",
            "¿Cómo está el clima?",
        ]
        for f in frases_clima:
            with self.subTest(frase=f):
                cmds = ms.router_local(f)
                self.assertTrue(any(c[0] == "clima" for c in cmds), f"Clima bloqueado erróneamente en: {f}")


class TestSeguridadFlujoGroqPreguntasExplicativas(unittest.IsolatedAsyncioTestCase):
    def test_schemas_enviados_a_groq_para_preguntas_explicativas(self):
        # Comprobar que Groq NUNCA recibe schemas de acción para preguntas explicativas/hipotéticas
        consultas = [
            "¿Cómo cambio al escritorio 3?",
            "¿Cómo abro Steam?",
            "Si cierro Discord, ¿qué pasa?",
            "¿Cómo puedo mover una ventana?",
            "¿Qué pasa si reinicio el servidor?",
            "¿Cómo me desconecto de la Alexa?",
        ]
        tools_prohibidas = {
            "cambiar_escritorio", "mover_ventana", "abrir_aplicacion",
            "cerrar_aplicacion", "reiniciar_servidor", "control_energia",
            "conectar_bluetooth", "desconectar_bluetooth", "cerrar_todo"
        }
        for c in consultas:
            with self.subTest(consulta=c):
                schemas_enviados = {t["function"]["name"] for t in ms.tools_necesarias(c)}
                interseccion = schemas_enviados & tools_prohibidas
                self.assertEqual(
                    interseccion, set(),
                    f"Groq recibió herramientas de acción indebidas para '{c}': {interseccion}"
                )

    async def test_bloqueo_de_ejecucion_si_groq_intenta_tool_call(self):
        # Si por alguna alucinación Groq emitiera un tool_call de acción sobre una pregunta
        # explicativa, el guardián de _procesar_mensaje debe bloquear su ejecución física.
        from types import SimpleNamespace

        tool_call = SimpleNamespace(
            id="call_999",
            function=SimpleNamespace(name="cambiar_escritorio", arguments='{"escritorio": 3}')
        )
        respuesta_groq = mock.Mock(
            choices=[
                mock.Mock(
                    message=mock.Mock(
                        tool_calls=[tool_call],
                        content=None,
                    ),
                    finish_reason="stop",
                )
            ]
        )
        with mock.patch.object(ms.client.chat.completions, "create", return_value=respuesta_groq), \
             mock.patch.dict(ms.TOOLS_FUNCIONES, {"cambiar_escritorio": mock.Mock()}) as tools_dict, \
             mock.patch.object(ms, "procesar_salida_de_voz", mock.AsyncMock(return_value=None)):
            resp = await ms.procesar_mensaje(ms.ChatRequest(message="¿Cómo cambio al escritorio 3?"), origen="pc")
            # La función real de cambiar_escritorio NUNCA debió llamarse
            self.assertEqual(tools_dict["cambiar_escritorio"].call_count, 0)
            self.assertIn("No se ejecutó ninguna acción", resp.response)


if __name__ == "__main__":
    unittest.main()
