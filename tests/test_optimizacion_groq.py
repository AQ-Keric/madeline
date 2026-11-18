"""Tests para el Stage 3.1: Optimización de Groq y Selección Inteligente de Herramientas.

Verificaciones obligatorias:
1. Apertura y cierre seleccionan herramientas distintas.
2. Una orden de apertura nunca incluye 'cerrar_aplicacion'.
3. Una orden de cierre nunca incluye 'abrir_aplicacion'.
4. Las preguntas conceptuales y casuales no seleccionan herramientas (set()).
5. Las búsquedas explícitas y consultas de actualidad conservan 'buscar_en_web'.
6. Conservación íntegra de parámetros y enums en los 28 esquemas.
7. Protección contra ejecución accidental de herramientas (Stage 1 y 2).
8. Confirmación de que tool_choice no se envía cuando no existen herramientas.
"""
import json
import unittest
from unittest import mock

import entorno  # noqa: F401
import madeline_server as ms
from resultado import EstadoResultado, Resultado


def nombres_tools(texto: str) -> set[str]:
    return {t["function"]["name"] for t in ms.tools_necesarias(texto)}


def mock_respuesta_groq(texto="Respuesta de prueba.", tool_calls=None):
    choice = mock.MagicMock()
    choice.finish_reason = "stop"
    choice.message = mock.MagicMock()
    choice.message.content = texto
    choice.message.tool_calls = tool_calls or []
    resp = mock.MagicMock()
    resp.choices = [choice]
    return resp


class TestConversacionesCasualesYConceptualesSinHerramientas(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(ms, "procesar_salida_de_voz", mock.AsyncMock(return_value=None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_preguntas_conceptuales_y_casuales_retornan_cero_tools(self):
        frases = [
            # Casuales / Charla / Opinión
            "cuéntame un chiste",
            "hola madeline cómo estás",
            "buenos días, ¿qué tal?",
            "¿qué opinas de flutter?",
            "¿sabes que mi equipo favorito es la u?",
            "escribe un poema corto sobre las estrellas",
            "háblame un poco de ti",
            "qué lindo día hace hoy",
            # Conocimiento general / Conceptuales
            "¿quién fue albert einstein?",
            "¿qué es flutter?",
            "¿qué es el sol?",
            "una rama del árbol",
            "cuánto tiempo falta para abril",
            "¿cuál es el sentido de la vida?",
            "explícame la teoría de la relatividad",
        ]
        for frase in frases:
            with self.subTest(frase=frase):
                self.assertEqual(
                    nombres_tools(frase),
                    set(),
                    f"La frase '{frase}' no debió recibir herramientas."
                )

    async def test_flujo_procesar_mensaje_conversacion_sin_tools(self):
        with mock.patch.object(ms.client.chat.completions, "create") as mock_create:
            mock_create.return_value = mock_respuesta_groq("Hola Keric, todo bien por aquí.")
            req = ms.ChatRequest(message="Hola Madeline, ¿cómo estás hoy?")
            registro = {"ruta": None, "acciones": []}
            res = await ms._procesar_mensaje(req, origen="pc", registro=registro)

            self.assertEqual(registro["ruta"], "groq")
            self.assertEqual(registro["tools_enviadas"], [])
            call_kwargs = mock_create.call_args[1]
            self.assertNotIn("tools", call_kwargs)
            self.assertNotIn("tool_choice", call_kwargs)
            self.assertIn("Hola Keric", res.response)


class TestDiscriminacionAperturaYCierre(unittest.TestCase):
    """Verifica que apertura y cierre seleccionen herramientas distintas y no se mezclen."""

    def test_orden_apertura_selecciona_solo_abrir_y_nunca_cerrar(self):
        ordenes_apertura = [
            "Inicia Discord",
            "inicia discord por favor",
            "arranca el navegador",
            "abre la aplicación firefox",
            "MADELINE, abro Steam.",
            "levanta la aplicación de terminal",
            "ejecuta la calculadora",
            "pon en marcha el editor de código",
            "despacha la aplicación discord",
        ]
        for orden in ordenes_apertura:
            with self.subTest(orden=orden):
                tools = nombres_tools(orden)
                self.assertIn("abrir_aplicacion", tools, f"'{orden}' debió incluir 'abrir_aplicacion'.")
                self.assertNotIn("cerrar_aplicacion", tools, f"'{orden}' NUNCA debe incluir 'cerrar_aplicacion'.")
                self.assertEqual(tools, {"abrir_aplicacion"}, f"'{orden}' debió seleccionar únicamente 'abrir_aplicacion'.")

    def test_orden_cierre_selecciona_solo_cerrar_y_nunca_abrir(self):
        ordenes_cierre = [
            "cierra discord",
            "cierra eso",
            "mata el proceso de steam",
            "termina la aplicación obs",
            "cierra el proceso de discord",
            "cierra la aplicación de firefox",
        ]
        for orden in ordenes_cierre:
            with self.subTest(orden=orden):
                tools = nombres_tools(orden)
                self.assertIn("cerrar_aplicacion", tools, f"'{orden}' debió incluir 'cerrar_aplicacion'.")
                self.assertNotIn("abrir_aplicacion", tools, f"'{orden}' NUNCA debe incluir 'abrir_aplicacion'.")
                self.assertEqual(tools, {"cerrar_aplicacion"}, f"'{orden}' debió seleccionar únicamente 'cerrar_aplicacion'.")

    def test_apertura_y_cierre_son_disjuntos(self):
        tools_abrir = nombres_tools("inicia discord")
        tools_cerrar = nombres_tools("cierra discord")
        self.assertTrue(tools_abrir.isdisjoint(tools_cerrar))


class TestBusquedasExplicitasYActualidad(unittest.TestCase):
    """Verifica que búsquedas explícitas y consultas de actualidad conservan buscar_en_web."""

    def test_busquedas_explicitas_y_de_actualidad_reciben_buscar_en_web(self):
        consultas = [
            # Búsqueda explícita
            "busca información sobre albert einstein",
            "busca información sobre el telescopio james webb",
            "investiga las últimas noticias sobre astronomía",
            "averigua quién ganó el partido de tenis",
            # Indicadores de actualidad ("actual", "hoy", "último", "precio", "noticias")
            "¿quién es el presidente actual de chile?",
            "¿cuál es el precio del dólar hoy?",
            "noticias de tecnología recientes",
            "¿a qué hora juega la selección hoy?",
            "cuál fue el último resultado de la u",
            "cotización del euro en tiempo real",
        ]
        for c in consultas:
            with self.subTest(consulta=c):
                recibidas = nombres_tools(c)
                self.assertIn(
                    "buscar_en_web",
                    recibidas,
                    f"La consulta '{c}' debió recibir 'buscar_en_web'."
                )
                self.assertNotIn(
                    "abrir_aplicacion",
                    recibidas,
                    f"La consulta informativa '{c}' no debe recibir 'abrir_aplicacion'."
                )
                self.assertNotIn(
                    "cerrar_aplicacion",
                    recibidas,
                    f"La consulta informativa '{c}' no debe recibir 'cerrar_aplicacion'."
                )


class TestPreguntasReflexivasSinHerramientasModificadoras(unittest.TestCase):
    HERRAMIENTAS_MODIFICADORAS = {
        "abrir_aplicacion", "cerrar_aplicacion", "mover_ventana",
        "cambiar_escritorio", "control_energia", "cerrar_todo",
        "reiniciar_servidor", "ajustar_volumen", "controlar_bluetooth",
        "conectar_bluetooth", "desconectar_bluetooth", "crear_evento",
        "agregar_recado", "borrar_recado", "mostrar_avatar",
    }

    def test_preguntas_explicativas_e_hipoteticas_sin_modificadoras(self):
        preguntas = [
            "¿por qué el cielo es azul?",
            "¿para qué sirve una mandarina?",
            "¿cómo cambio al escritorio 3?",
            "si cierro discord, ¿qué pasa?",
            "¿cómo funciona un agujero negro?",
        ]
        for p in preguntas:
            with self.subTest(pregunta=p):
                recibidas = nombres_tools(p)
                modificadoras_recibidas = recibidas & self.HERRAMIENTAS_MODIFICADORAS
                self.assertEqual(
                    modificadoras_recibidas,
                    set(),
                    f"La pregunta '{p}' no debió recibir tools modificadoras, pero recibió {modificadoras_recibidas}."
                )


class TestConservacionParametrosYEnumsEsquemas(unittest.TestCase):
    def test_total_herramientas_y_nombres(self):
        self.assertEqual(len(ms.tools), 28)
        nombres = {t["function"]["name"] for t in ms.tools}
        esperados = {
            "ajustar_volumen", "cerrar_aplicacion", "control_multimedia", "info_cancion",
            "control_energia", "reiniciar_servidor", "cerrar_todo", "reporte_clima",
            "estado_sistema", "tomar_captura", "buscar_en_web", "abrir_aplicacion",
            "mover_ventana", "cambiar_escritorio", "controlar_bluetooth", "conectar_bluetooth",
            "desconectar_bluetooth", "actualizar_sistema", "verificar_actualizaciones",
            "abrir_radio_musica", "abrir_pestanas_busqueda", "analizar_pantalla",
            "agregar_recado", "ver_recados", "borrar_recado", "ver_agenda",
            "crear_evento", "mostrar_avatar"
        }
        self.assertEqual(nombres, esperados)

    def test_enums_clave_preservados(self):
        tools_dict = {t["function"]["name"]: t["function"] for t in ms.tools}
        self.assertEqual(tools_dict["ajustar_volumen"]["parameters"]["properties"]["accion"]["enum"], ["subir", "bajar", "mutear", "setear"])
        self.assertEqual(tools_dict["control_multimedia"]["parameters"]["properties"]["accion"]["enum"], ["pausar", "reproducir", "siguiente", "anterior", "reiniciar"])
        self.assertEqual(tools_dict["control_energia"]["parameters"]["properties"]["accion"]["enum"], ["apagar", "reiniciar"])
        self.assertEqual(tools_dict["reporte_clima"]["parameters"]["properties"]["dia"]["enum"], ["ahora", "hoy", "mañana", "pasado mañana"])
        self.assertEqual(tools_dict["controlar_bluetooth"]["parameters"]["properties"]["accion"]["enum"], ["encender", "apagar"])
        self.assertEqual(tools_dict["conectar_bluetooth"]["parameters"]["properties"]["dispositivo"]["enum"], list(ms.DISPOSITIVOS_BLUETOOTH))
        self.assertEqual(tools_dict["abrir_pestanas_busqueda"]["parameters"]["properties"]["tipo"]["enum"], ["general", "papers", "imagenes"])
        self.assertEqual(tools_dict["mostrar_avatar"]["parameters"]["properties"]["accion"]["enum"], ["mostrar", "esconder"])

    def test_parametros_requeridos_preservados(self):
        tools_dict = {t["function"]["name"]: t["function"] for t in ms.tools}
        self.assertEqual(tools_dict["abrir_aplicacion"]["parameters"]["required"], ["app_name"])
        self.assertEqual(tools_dict["mover_ventana"]["parameters"]["required"], ["app_name", "escritorio"])
        self.assertEqual(tools_dict["cambiar_escritorio"]["parameters"]["required"], ["escritorio"])
        self.assertEqual(tools_dict["buscar_en_web"]["parameters"]["required"], ["consulta"])
        self.assertEqual(tools_dict["crear_evento"]["parameters"]["required"], ["titulo", "fecha"])


class TestProteccionEjecucionAccidentalHerramientas(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(ms, "procesar_salida_de_voz", mock.AsyncMock(return_value=None))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher_rl = mock.patch.object(ms, "router_local", return_value=[])
        patcher_rl.start()
        self.addCleanup(patcher_rl.stop)

    async def test_guardian_intercepta_alucinacion_en_groq_sin_tools(self):
        with mock.patch.object(ms.client.chat.completions, "create") as mock_create:
            mock_create.return_value = mock_respuesta_groq("¡Listo, Keric! Steam está abierto y listo.")
            req = ms.ChatRequest(message="MADELINE, abro Steam.")
            registro = {"ruta": None, "acciones": []}
            res = await ms._procesar_mensaje(req, origen="pc", registro=registro)

            self.assertIn("No pude realizar esa acción porque no se ejecutó ninguna herramienta", res.response)

    async def test_pregunta_explicativa_bloquea_tool_call_accidental(self):
        call_mock = mock.MagicMock()
        call_mock.function.name = "cambiar_escritorio"
        call_mock.function.arguments = json.dumps({"escritorio": 3})

        with mock.patch.object(ms.client.chat.completions, "create") as mock_create:
            mock_create.return_value = mock_respuesta_groq("", tool_calls=[call_mock])
            req = ms.ChatRequest(message="¿Cómo cambio al escritorio 3?")
            registro = {"ruta": None, "acciones": []}
            res = await ms._procesar_mensaje(req, origen="pc", registro=registro)

            self.assertIn("No se ejecutó ninguna acción porque tu consulta es una pregunta conceptual o explicativa", res.response)


class TestToolChoiceOmitidoSinHerramientas(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(ms, "procesar_salida_de_voz", mock.AsyncMock(return_value=None))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher_rl = mock.patch.object(ms, "router_local", return_value=[])
        patcher_rl.start()
        self.addCleanup(patcher_rl.stop)

    async def test_tool_choice_no_presente_cuando_tools_vacia(self):
        with mock.patch.object(ms.client.chat.completions, "create") as mock_create:
            mock_create.return_value = mock_respuesta_groq("Respuesta filosófica sobre la existencia.")
            req = ms.ChatRequest(message="¿Qué opinas sobre el existencialismo y Jean-Paul Sartre?")
            registro = {"ruta": None, "acciones": []}
            await ms._procesar_mensaje(req, origen="pc", registro=registro)

            kwargs = mock_create.call_args[1]
            self.assertNotIn("tools", kwargs)
            self.assertNotIn("tool_choice", kwargs)


if __name__ == "__main__":
    unittest.main()
