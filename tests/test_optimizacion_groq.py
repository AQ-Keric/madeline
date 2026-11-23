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
import asyncio
import json
import time
import unittest
from unittest import mock

from fastapi import HTTPException
from groq import APITimeoutError, RateLimitError
import httpx
import entorno  # noqa: F401
import madeline_server as ms
from resultado import EstadoResultado, Resultado


def error_sin_cuota():
    return RateLimitError(
        "rate limit",
        response=httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com")),
        body=None,
    )


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
        call_mock.id = "call_mock_escritorio"
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


class TestHistorialConversacionalStage32(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(ms, "procesar_salida_de_voz", mock.AsyncMock(return_value=None))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._limpiar_estado_global)
        self._limpiar_estado_global()

    def _limpiar_estado_global(self):
        ms.historial_chat.clear()
        ms.historiales_por_origen["pc"].clear()
        ms.historiales_por_origen["telefono"].clear()
        ms.acciones_locales_recientes["pc"] = None
        ms.acciones_locales_recientes["telefono"] = None

    async def test_payload_groq_orden_estricto_cronologico(self):
        """El array messages enviado a Groq debe ser estrictamente U_ant -> A_ant -> U_act."""
        with mock.patch.object(ms.client.chat.completions, "create") as mock_create:
            mock_create.return_value = mock_respuesta_groq("Fue una matemática pionera.")
            await ms.procesar_mensaje(ms.ChatRequest(message="¿Quién fue Ada Lovelace?"), origen="pc")

            mock_create.return_value = mock_respuesta_groq("Nació en 1815.")
            await ms.procesar_mensaje(ms.ChatRequest(message="¿En qué año nació?"), origen="pc")

            mensajes = mock_create.call_args[1]["messages"]
            sin_sistema = [m for m in mensajes if m["role"] != "system"]
            roles = [m["role"] for m in sin_sistema]
            contenidos = [m["content"] for m in sin_sistema]

            self.assertEqual(roles, ["user", "assistant", "user"])
            self.assertEqual(contenidos, ["¿Quién fue Ada Lovelace?", "Fue una matemática pionera.", "¿En qué año nació?"])
            # El mensaje actual SIEMPRE debe ser el último:
            self.assertEqual(mensajes[-1]["role"], "user")
            self.assertEqual(mensajes[-1]["content"], "¿En qué año nació?")

    async def test_aislamiento_historial_pc_y_telefono(self):
        """Los mensajes de PC y teléfono no deben mezclarse."""
        with mock.patch.object(ms.client.chat.completions, "create") as mock_create:
            mock_create.return_value = mock_respuesta_groq("Respuesta PC")
            await ms.procesar_mensaje(ms.ChatRequest(message="Consulta desde PC"), origen="pc")

            mock_create.return_value = mock_respuesta_groq("Respuesta Teléfono")
            await ms.procesar_mensaje(ms.ChatRequest(message="Consulta desde Teléfono"), origen="telefono")

            self.assertEqual(len(ms.historiales_por_origen["pc"]), 1)
            self.assertEqual(ms.historiales_por_origen["pc"][0].usuario, "Consulta desde PC")
            self.assertEqual(ms.historiales_por_origen["pc"][0].asistente, "Respuesta PC")

            self.assertEqual(len(ms.historiales_por_origen["telefono"]), 1)
            self.assertEqual(ms.historiales_por_origen["telefono"][0].usuario, "Consulta desde Teléfono")
            self.assertEqual(ms.historiales_por_origen["telefono"][0].asistente, "Respuesta Teléfono")

            # historial_chat solo proyecta PC
            self.assertEqual(len(ms.historial_chat), 2)
            self.assertEqual(ms.historial_chat[0]["content"], "Consulta desde PC")

    async def test_historial_chat_compat_clear_solo_afecta_pc(self):
        """clear() en historial_chat debe vaciar PC sin alterar el historial del teléfono."""
        ms.historiales_por_origen["pc"].append(ms.TurnoConversacion("U_pc", "A_pc", time.time()))
        ms.historiales_por_origen["telefono"].append(ms.TurnoConversacion("U_tel", "A_tel", time.time()))
        ms.historial_chat.sincronizar_desde_turnos(ms.historiales_por_origen["pc"])

        self.assertEqual(len(ms.historial_chat), 2)
        self.assertEqual(len(ms.historiales_por_origen["telefono"]), 1)

        ms.historial_chat.clear()

        self.assertEqual(ms.historial_chat, [])
        self.assertEqual(ms.historiales_por_origen["pc"], [])
        self.assertEqual(len(ms.historiales_por_origen["telefono"]), 1)
        self.assertEqual(ms.historiales_por_origen["telefono"][0].usuario, "U_tel")

    async def test_accion_fallida_sobrescribe_inmediatamente_evidencia_exitosa(self):
        """Una acción con FAILURE debe sobrescribir una acción previa con SUCCESS."""
        # Acción 1 exitosa:
        ms.ejecutar_comandos_locales([("volumen", {"accion": "fijar", "porcentaje": 50})], origen="pc")
        self.assertIsNotNone(ms.acciones_locales_recientes["pc"])
        self.assertTrue(ms.acciones_locales_recientes["pc"].resultado.ok)

        # Acción 2 fallida:
        fallo = Resultado(ok=False, mensaje="No se encontró la aplicación blender", estado=EstadoResultado.FAILURE)
        with mock.patch.object(ms, "ejecutar_comando_local", return_value=fallo):
            ms.ejecutar_comandos_locales([("abrir_aplicacion", {"app": "blender"})], origen="pc")

        self.assertIsNotNone(ms.acciones_locales_recientes["pc"])
        self.assertEqual(ms.acciones_locales_recientes["pc"].comando, "abrir_aplicacion")
        self.assertEqual(ms.acciones_locales_recientes["pc"].resultado.estado, EstadoResultado.FAILURE)

        ctx = ms.contexto_para_pregunta("¿se abrió?", origen="pc")
        self.assertIn("FAILURE", ctx)
        self.assertIn("blender", ctx)
        self.assertNotIn("volumen", ctx)

    async def test_accion_local_seguimiento_estados_success_pending_failure(self):
        """Verifica el formato del contexto para cada estado de EstadoResultado."""
        # SUCCESS
        res_ok = Resultado(ok=True, mensaje="Volumen fijado al 50%", estado=EstadoResultado.SUCCESS)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("volumen", {"porcentaje": 50}, res_ok, time.time(), "pc")
        ctx_ok = ms.contexto_para_pregunta("¿qué volumen quedó?", origen="pc")
        self.assertIn("SUCCESS", ctx_ok)
        self.assertIn("Volumen fijado al 50%", ctx_ok)

        # PENDING
        res_pending = Resultado(ok=True, mensaje="Abriendo Spotify", evidencia={"app": "spotify"}, estado=EstadoResultado.PENDING)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("abrir_aplicacion", {"app": "spotify"}, res_pending, time.time(), "pc")
        ctx_pending = ms.contexto_para_pregunta("¿se abrió la ventana?", origen="pc")
        self.assertIn("PENDING", ctx_pending)
        self.assertIn("Abriendo Spotify", ctx_pending)

        # FAILURE
        res_fail = Resultado(ok=False, mensaje="Comando no reconocido", estado=EstadoResultado.FAILURE)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("desconocido", {}, res_fail, time.time(), "pc")
        ctx_fail = ms.contexto_para_pregunta("¿funcionó?", origen="pc")
        self.assertIn("FAILURE", ctx_fail)

    async def test_accion_local_expirada_no_se_inyecta(self):
        """Acciones de más de 120 segundos no deben inyectar contexto."""
        res_ok = Resultado(ok=True, mensaje="Volumen al 50%", estado=EstadoResultado.SUCCESS)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente(
            "volumen", {"porcentaje": 50}, res_ok, time.time() - 150.0, "pc"
        )
        ctx = ms.contexto_para_pregunta("¿qué volumen quedó?", origen="pc")
        self.assertEqual(ctx, "")

    async def test_aislamiento_acciones_locales_pc_y_telefono(self):
        """Las acciones ejecutadas en PC no deben inyectar contexto al teléfono."""
        res_ok = Resultado(ok=True, mensaje="Abriendo Steam", estado=EstadoResultado.SUCCESS)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("abrir_aplicacion", {"app": "steam"}, res_ok, time.time(), "pc")

        ctx_tel = ms.contexto_para_pregunta("¿se abrió?", origen="telefono")
        self.assertEqual(ctx_tel, "")

    async def test_groq_timeout_nativo_libera_lock_y_no_corrompe_historial(self):
        """Si Groq lanza APITimeoutError, se levanta HTTP 504, el lock se libera y el historial queda limpio."""
        req_mock = mock.MagicMock()
        with mock.patch.object(ms.client.chat.completions, "create", side_effect=APITimeoutError(request=req_mock)):
            with self.assertRaises(HTTPException) as ctx:
                await ms.procesar_mensaje(ms.ChatRequest(message="¿cuál es la masa de Júpiter?"), origen="pc")

            self.assertEqual(ctx.exception.status_code, 504)
            self.assertIn("Tiempo de espera agotado", ctx.exception.detail)

        # Historial permanece limpio (sin turno incompleto):
        self.assertEqual(ms.historiales_por_origen["pc"], [])
        self.assertEqual(ms.historial_chat, [])

        # El lock debe estar liberado:
        self.assertFalse(ms.obtener_lock_origen("pc").locked())

        # Una petición subsiguiente debe funcionar con normalidad:
        with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("Respuesta OK")):
            resp = await ms.procesar_mensaje(ms.ChatRequest(message="Hola de nuevo"), origen="pc")
            self.assertEqual(resp.response, "Respuesta OK")
            self.assertEqual(len(ms.historiales_por_origen["pc"]), 1)

    async def test_rollback_atomico_ante_error_500_o_excepcion(self):
        """Si Groq falla con 500, no se agregan preguntas huérfanas al historial."""
        with mock.patch.object(ms.client.chat.completions, "create", side_effect=Exception("Falla interna Groq 500")):
            with self.assertRaises(HTTPException) as ctx:
                await ms.procesar_mensaje(ms.ChatRequest(message="Pregunta fallida"), origen="pc")
            self.assertEqual(ctx.exception.status_code, 500)

        # No hay preguntas huérfanas en historial
        self.assertEqual(ms.historiales_por_origen["pc"], [])
        self.assertEqual(ms.historial_chat, [])

        # Siguiente petición no tiene dos usuarios consecutivos
        with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("Respuesta normal")) as mock_create:
            await ms.procesar_mensaje(ms.ChatRequest(message="Nueva pregunta"), origen="pc")
            mensajes = mock_create.call_args[1]["messages"]
            usuarios = [m["content"] for m in mensajes if m["role"] == "user"]
            self.assertEqual(usuarios, ["Nueva pregunta"])

    async def test_cancelacion_asyncio_libera_lock_y_no_corrompe(self):
        """Verifica que si la corrutina recibe CancelledError (simulado con side_effect en el mock),
        el lock se libera al salir del contexto y no se guarda turno en el historial.

        Nota: Este test evalúa la propagación de CancelledError en asyncio y la liberación del lock;
        no prueba una cancelación real a nivel de socket HTTP ni la interrupción del hilo en el SO."""
        with mock.patch.object(ms.client.chat.completions, "create", side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await ms.procesar_mensaje(ms.ChatRequest(message="Cancelación"), origen="pc")

        self.assertEqual(ms.historiales_por_origen["pc"], [])
        self.assertFalse(ms.obtener_lock_origen("pc").locked())

    async def test_concurrencia_serializacion_mismo_origen(self):
        """Dos solicitudes del mismo origen se serializan ordenadamente bajo el Lock."""
        orden = []

        def side_effect_lento(*args, **kwargs):
            orden.append("llamada_groq")
            return mock_respuesta_groq("Respuesta")

        with mock.patch.object(ms.client.chat.completions, "create", side_effect=side_effect_lento):
            tarea1 = asyncio.create_task(ms.procesar_mensaje(ms.ChatRequest(message="Pregunta 1"), origen="pc"))
            tarea2 = asyncio.create_task(ms.procesar_mensaje(ms.ChatRequest(message="Pregunta 2"), origen="pc"))
            await asyncio.gather(tarea1, tarea2)

        self.assertEqual(orden, ["llamada_groq", "llamada_groq"])
        self.assertEqual(len(ms.historiales_por_origen["pc"]), 1)
        # La última pregunta fue la 2 (poda a MAX_TURNOS=1)
        self.assertEqual(ms.historiales_por_origen["pc"][0].usuario, "Pregunta 2")

    async def test_groq_tools_no_contaminan_acciones_locales_recientes(self):
        """Las herramientas ejecutadas por Groq no deben alterar acciones_locales_recientes."""
        # Se establece una acción local previa:
        res_ok = Resultado(ok=True, mensaje="Volumen al 40%", estado=EstadoResultado.SUCCESS)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("volumen", {"porcentaje": 40}, res_ok, time.time(), "pc")

        # Llamada a Groq con tool call (bypaseando router_local):
        call_mock = mock.MagicMock()
        call_mock.id = "call_mock_cancion"
        call_mock.function.name = "info_cancion"
        call_mock.function.arguments = "{}"

        with mock.patch.object(ms, "router_local", return_value=[]), \
             mock.patch.object(ms.client.chat.completions, "create") as mock_create, \
             mock.patch.dict(ms.TOOLS_FUNCIONES, {"info_cancion": mock.Mock(return_value="Actualmente está sonando: Bohemian Rhapsody")}):
            mock_create.return_value = mock_respuesta_groq("", tool_calls=[call_mock])
            await ms.procesar_mensaje(ms.ChatRequest(message="¿Qué canción está sonando?"), origen="pc")

        # acciones_locales_recientes sigue siendo la acción local original:
        self.assertEqual(ms.acciones_locales_recientes["pc"].comando, "volumen")

    async def test_cuota_agotada_pc_con_turno_previo_conserva_historial(self):
        """Si ocurre falta de cuota en PC, el turno anterior completado debe preservarse íntegro en ambos historiales."""
        with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("Fue un filósofo griego.")):
            await ms.procesar_mensaje(ms.ChatRequest(message="¿Quién fue Pitágoras?"), origen="pc")

        self.assertEqual(len(ms.historiales_por_origen["pc"]), 1)
        self.assertEqual(len(ms.historial_chat), 2)

        with mock.patch.object(ms.client.chat.completions, "create", side_effect=error_sin_cuota()):
            resp = await ms.procesar_mensaje(ms.ChatRequest(message="¿Qué es la física cuántica?"), origen="pc")

        self.assertIn("cuota", resp.response)
        # El turno previo debe seguir intacto en ambos:
        self.assertEqual(len(ms.historiales_por_origen["pc"]), 1)
        self.assertEqual(ms.historiales_por_origen["pc"][0].usuario, "¿Quién fue Pitágoras?")
        self.assertEqual(ms.historiales_por_origen["pc"][0].asistente, "Fue un filósofo griego.")

        self.assertEqual(len(ms.historial_chat), 2)
        self.assertEqual(ms.historial_chat[0], {"role": "user", "content": "¿Quién fue Pitágoras?"})
        self.assertEqual(ms.historial_chat[1], {"role": "assistant", "content": "Fue un filósofo griego."})

    async def test_cuota_agotada_telefono_con_turno_previo_conserva_historial(self):
        """Si ocurre falta de cuota en teléfono, el turno anterior en el teléfono debe permanecer intacto."""
        with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("Respuesta móvil")):
            await ms.procesar_mensaje(ms.ChatRequest(message="Consulta móvil"), origen="telefono")

        self.assertEqual(len(ms.historiales_por_origen["telefono"]), 1)

        with mock.patch.object(ms.client.chat.completions, "create", side_effect=error_sin_cuota()):
            resp = await ms.procesar_mensaje(ms.ChatRequest(message="Otra consulta móvil"), origen="telefono")

        self.assertIn("cuota", resp.response)
        self.assertEqual(len(ms.historiales_por_origen["telefono"]), 1)
        self.assertEqual(ms.historiales_por_origen["telefono"][0].usuario, "Consulta móvil")
        self.assertEqual(ms.historiales_por_origen["telefono"][0].asistente, "Respuesta móvil")

    async def test_cancelacion_durante_salida_voz_mantiene_turno_confirmado(self):
        """Si la tarea se cancela durante la síntesis/reproducción de voz tras una respuesta exitosa de Groq, el turno permanece confirmado."""
        with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("Respuesta confirmada")), \
             mock.patch.object(ms, "procesar_salida_de_voz", side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await ms.procesar_mensaje(ms.ChatRequest(message="Pregunta con cancelación en voz"), origen="pc")

        # El turno fue confirmado antes de procesar_salida_de_voz y permanece guardado:
        self.assertEqual(len(ms.historiales_por_origen["pc"]), 1)
        self.assertEqual(ms.historiales_por_origen["pc"][0].usuario, "Pregunta con cancelación en voz")
        self.assertEqual(ms.historiales_por_origen["pc"][0].asistente, "Respuesta confirmada")

        self.assertEqual(len(ms.historial_chat), 2)
        self.assertEqual(ms.historial_chat[1]["content"], "Respuesta confirmada")

        # El lock debe liberarse limpiamente:
        self.assertFalse(ms.obtener_lock_origen("pc").locked())


class TestStage33ComportamientoConversacional(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ms.historiales_por_origen["pc"].clear()
        ms.historiales_por_origen["telefono"].clear()
        ms.historial_chat.clear()
        ms.acciones_locales_recientes["pc"] = None
        ms.acciones_locales_recientes["telefono"] = None
        ms.memoria_contexto.clear()

        patch_voz = mock.patch.object(ms, "procesar_salida_de_voz", mock.AsyncMock(return_value=None))
        patch_voz.start()
        self.addCleanup(patch_voz.stop)

        def limpiar():
            ms.historiales_por_origen["pc"].clear()
            ms.historiales_por_origen["telefono"].clear()
            ms.historial_chat.clear()
            ms.acciones_locales_recientes["pc"] = None
            ms.acciones_locales_recientes["telefono"] = None
            ms.memoria_contexto.clear()
        self.addCleanup(limpiar)

    # ------------------------------------------------------------
    # BLOQUE A: Integración y Multi-Turno
    # ------------------------------------------------------------
    async def test_T33_INT_01_accion_causa_repeticion(self):
        """Secuencia completa: Acción local -> Causa -> Repetición segura."""
        with mock.patch.object(ms, "ajustar_volumen", return_value=Resultado(ok=True, mensaje="Volumen al 50%", estado=EstadoResultado.SUCCESS)) as mock_vol, \
             mock.patch.object(ms.client.chat.completions, "create") as mock_groq:

            # Turno 1: Orden local directa
            res1 = await ms.procesar_mensaje(ms.ChatRequest(message="Sube el volumen un 10%"), origen="pc")
            self.assertEqual(mock_vol.call_count, 1)
            self.assertIsNotNone(ms.acciones_locales_recientes["pc"])
            self.assertEqual(ms.acciones_locales_recientes["pc"].resultado.estado, EstadoResultado.SUCCESS)

            # Turno 2: Pregunta sobre causa a Groq
            mock_groq.return_value = mock_respuesta_groq("Fue porque me pediste subir el volumen.")
            res2 = await ms.procesar_mensaje(ms.ChatRequest(message="¿Por qué hiciste eso?"), origen="pc")
            mensajes_enviados = mock_groq.call_args.kwargs["messages"]
            system_contexts = [m["content"] for m in mensajes_enviados if m["role"] == "system"]
            self.assertTrue(any("volumen" in c and "SUCCESS" in c for c in system_contexts))

            # Turno 3: Repetición segura (no llama a Groq)
            mock_groq.reset_mock()
            res3 = await ms.procesar_mensaje(ms.ChatRequest(message="Hazlo de nuevo"), origen="pc")
            mock_groq.assert_not_called()
            self.assertEqual(mock_vol.call_count, 2)
            self.assertTrue(any(w in res3.response.lower() for w in ("volumen", "ruido", "10%")))

    async def test_T33_INT_02_preservacion_n1_dialogo_tras_orden_local_n2(self):
        """Un comando local no destruye la conversación de Nivel 1."""
        with mock.patch.object(ms.client.chat.completions, "create") as mock_groq, \
             mock.patch.object(ms, "control_multimedia", return_value=Resultado(ok=True, mensaje="Música pausada", estado=EstadoResultado.SUCCESS)):

            # Turno 1: Diálogo conceptual
            mock_groq.return_value = mock_respuesta_groq("La teoría de la relatividad especial describe el espacio-tiempo.")
            await ms.procesar_mensaje(ms.ChatRequest(message="¿Qué es la teoría de la relatividad especial?"), origen="pc")
            self.assertEqual(len(ms.historiales_por_origen["pc"]), 1)

            # Turno 2: Orden local de música
            res_local = await ms.procesar_mensaje(ms.ChatRequest(message="Pausa la música"), origen="pc")
            self.assertIsNotNone(ms.acciones_locales_recientes["pc"])
            self.assertEqual(ms.acciones_locales_recientes["pc"].comando, "multimedia")
            self.assertEqual(len(ms.historiales_por_origen["pc"]), 1)
            self.assertEqual(ms.historiales_por_origen["pc"][0].usuario, "¿Qué es la teoría de la relatividad especial?")

            # Turno 3: Seguimiento conceptual
            mock_groq.reset_mock()
            mock_groq.return_value = mock_respuesta_groq("El experimento de Michelson-Morley la confirmó.")
            await ms.procesar_mensaje(ms.ChatRequest(message="¿Y qué experimentos la confirmaron?"), origen="pc")

            mensajes_enviados = mock_groq.call_args.kwargs["messages"]
            usuarios = [m["content"] for m in mensajes_enviados if m["role"] == "user"]
            self.assertIn("¿Qué es la teoría de la relatividad especial?", usuarios)
            self.assertNotIn("Pausa la música", usuarios)
            self.assertFalse(any("multimedia" in m["content"] for m in mensajes_enviados))

    async def test_T33_INT_03_fallo_local_explica_evidencia_sin_exito_falso(self):
        """Fallo local transmite evidencia técnica sin afirmar falso éxito."""
        with mock.patch.object(ms, "control_multimedia", return_value=Resultado(
            ok=False, mensaje="No se encontró un reproductor activo", evidencia={"exit_code": 1}, estado=EstadoResultado.FAILURE
        )), mock.patch.object(ms.client.chat.completions, "create") as mock_groq:

            # Turno 1: Fallo local
            res1 = await ms.procesar_mensaje(ms.ChatRequest(message="Pausa la música"), origen="pc")
            self.assertEqual(ms.acciones_locales_recientes["pc"].resultado.estado, EstadoResultado.FAILURE)

            # Turno 2: Pregunta por qué falló
            mock_groq.return_value = mock_respuesta_groq("Falló porque playerctl reportó código 1: no había ningún reproductor activo.")
            res2 = await ms.procesar_mensaje(ms.ChatRequest(message="¿Por qué falló?"), origen="pc")

            sys_contexts = [m["content"] for m in mock_groq.call_args.kwargs["messages"] if m["role"] == "system"]
            self.assertTrue(any("FAILURE" in c and "exit_code" in c for c in sys_contexts))
            self.assertNotIn("Música pausada con éxito", res2.response)

    async def test_T33_INT_04_seguimiento_factico_tras_tool_groq_sin_reinvocacion(self):
        """Consulta sobre una herramienta previa se responde conversacionalmente sin re-invocar tools."""
        call_tool = mock.MagicMock()
        call_tool.id = "call_mock_cancion_2"
        call_tool.function.name = "info_cancion"
        call_tool.function.arguments = json.dumps({})

        with mock.patch.object(ms.client.chat.completions, "create") as mock_groq, \
             mock.patch.object(ms, "info_cancion", return_value="Actualmente está sonando: Bohemian Rhapsody de Queen"):

            # Turno 1: Tool call
            mock_groq.return_value = mock_respuesta_groq("", tool_calls=[call_tool])
            await ms.procesar_mensaje(ms.ChatRequest(message="¿Qué canción está sonando?"), origen="pc")

            # Turno 2: Seguimiento fáctico
            tools_t2 = ms.tools_necesarias("¿En qué año se lanzó?")
            self.assertNotIn("control_multimedia", [t["function"]["name"] for t in tools_t2])
            self.assertNotIn("abrir_radio_musica", [t["function"]["name"] for t in tools_t2])

            mock_groq.reset_mock()
            mock_groq.return_value = mock_respuesta_groq("Se lanzó en 1975 en el álbum A Night at the Opera.")
            res2 = await ms.procesar_mensaje(ms.ChatRequest(message="¿En qué año se lanzó?"), origen="pc")
            self.assertIn("1975", res2.response)

    # ------------------------------------------------------------
    # BLOQUE B: Repetición Segura y Manejo de Estados
    # ------------------------------------------------------------
    async def test_T33_REP_01_volumen_relativo_incrementa(self):
        """Repetir una orden relativa incrementa progresivamente."""
        with mock.patch.object(ms, "ajustar_volumen", side_effect=[
            Resultado(ok=True, mensaje="Volumen al 45%", estado=EstadoResultado.SUCCESS),
            Resultado(ok=True, mensaje="Volumen al 60%", estado=EstadoResultado.SUCCESS),
        ]) as mock_vol:
            await ms.procesar_mensaje(ms.ChatRequest(message="Sube el volumen 15%"), origen="pc")
            self.assertEqual(mock_vol.call_args.kwargs, {"accion": "subir", "porcentaje": 15})

            res2 = await ms.procesar_mensaje(ms.ChatRequest(message="Hazlo de nuevo"), origen="pc")
            self.assertEqual(mock_vol.call_count, 2)
            self.assertEqual(mock_vol.call_args.kwargs, {"accion": "subir", "porcentaje": 15})

    async def test_T33_REP_02_volumen_absoluto_idempotente(self):
        """Repetir una orden absoluta es idempotente e informativo."""
        with mock.patch.object(ms, "ajustar_volumen", return_value=Resultado(ok=True, mensaje="Volumen fijado al 40%", estado=EstadoResultado.SUCCESS)) as mock_vol:
            await ms.procesar_mensaje(ms.ChatRequest(message="Pon el volumen al 40%"), origen="pc")
            res2 = await ms.procesar_mensaje(ms.ChatRequest(message="Repítelo"), origen="pc")
            self.assertIn("40%", res2.response)

    async def test_T33_REP_03_mute_toggle_bloqueado(self):
        """El toggle de mute está estrictamente bloqueado contra repetición automática."""
        with mock.patch.object(ms, "ajustar_volumen", return_value=Resultado(ok=True, mensaje="Audio silenciado", estado=EstadoResultado.SUCCESS)) as mock_vol:
            await ms.procesar_mensaje(ms.ChatRequest(message="Mutea el audio"), origen="pc")
            self.assertEqual(mock_vol.call_count, 1)

            res2 = await ms.procesar_mensaje(ms.ChatRequest(message="Hazlo de nuevo"), origen="pc")
            self.assertEqual(mock_vol.call_count, 1)
            self.assertIn("alternancia", res2.response)
            self.assertIn("silenciar o reactivar", res2.response)

    async def test_T33_REP_04_multimedia_siguiente_y_pausa(self):
        """Repetición de control multimedia seguro."""
        with mock.patch.object(ms, "control_multimedia", return_value=Resultado(ok=True, mensaje="Pista siguiente", estado=EstadoResultado.SUCCESS)) as mock_media:
            await ms.procesar_mensaje(ms.ChatRequest(message="Siguiente canción"), origen="pc")
            self.assertEqual(mock_media.call_count, 1)

            res2 = await ms.procesar_mensaje(ms.ChatRequest(message="Otra vez"), origen="pc")
            self.assertEqual(mock_media.call_count, 2)

    async def test_T33_REP_05_mover_ventana_bloqueada_si_no_existe_o_sin_address(self):
        """Repetición bloqueada si la ventana original no existe o no tiene address."""
        # 1. Sin address
        res_sin_addr = Resultado(ok=True, mensaje="Ventana movida", evidencia={"escritorio": 2}, estado=EstadoResultado.SUCCESS)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("mover_ventana", {"app_name": "esta ventana", "escritorio": 2}, res_sin_addr, time.monotonic(), "pc")
        res1 = await ms.procesar_mensaje(ms.ChatRequest(message="Hazlo de nuevo"), origen="pc")
        self.assertIn("No se pudo identificar la ventana original", res1.response)

        # 2. Con address pero ventana ya no existe en listar_ventanas
        res_ok = Resultado(ok=True, mensaje="Ventana movida", evidencia={"address": "0x1234", "escritorio": 2}, estado=EstadoResultado.SUCCESS)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("mover_ventana", {"app_name": "esta ventana", "escritorio": 2}, res_ok, time.monotonic(), "pc")
        with mock.patch.object(ms.hyprland, "listar_ventanas", return_value=[{"address": "0x9999", "class": "terminal"}]), \
             mock.patch.object(ms.hyprland, "enviar_comando_socket") as mock_sock:
            res2 = await ms.procesar_mensaje(ms.ChatRequest(message="Hazlo de nuevo"), origen="pc")
            self.assertIn("ya no se encuentra abierta", res2.response)
            mock_sock.assert_not_called()

    async def test_T33_REP_06_mover_ventana_permitida_mismo_address(self):
        """Repetición de mover_ventana permitida si conserva identidad del address."""
        ventanas_abiertas = [{"address": "0x1234", "class": "firefox", "title": "Firefox"}]
        with mock.patch.object(ms.hyprland, "obtener_ventana_activa", return_value={
            "address": "0x1234", "class": "firefox", "title": "Firefox"
        }), mock.patch.object(ms.hyprland, "listar_ventanas", return_value=ventanas_abiertas), \
           mock.patch.object(ms.hyprland, "enviar_comando_socket", return_value=True) as mock_socket:

            await ms.procesar_mensaje(ms.ChatRequest(message="Mueve esta ventana al escritorio 2"), origen="pc")
            res2 = await ms.procesar_mensaje(ms.ChatRequest(message="Hazlo de nuevo"), origen="pc")
            self.assertIn("escritorio 2", res2.response)
            self.assertIn("address:0x1234", mock_socket.call_args[0][0])

    async def test_T33_REP_06b_perdio_foco_pero_sigue_abierta_mueve_address_original(self):
        """La ventana original sigue existiendo pero perdió el foco: se mueve sobre su address original."""
        res_ok = Resultado(ok=True, mensaje="Ventana movida", evidencia={"address": "0x1234", "class": "firefox", "escritorio": 2}, estado=EstadoResultado.SUCCESS)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("mover_ventana", {"app_name": "esta ventana", "escritorio": 2}, res_ok, time.monotonic(), "pc")

        # Foco activo es OTRA ventana ("0x9999", terminal)
        ventana_enfocada = {"address": "0x9999", "class": "alacritty", "title": "Terminal"}
        ventanas_abiertas = [
            {"address": "0x1234", "class": "firefox", "title": "Firefox"},
            ventana_enfocada,
        ]

        with mock.patch.object(ms.hyprland, "obtener_ventana_activa", return_value=ventana_enfocada), \
             mock.patch.object(ms.hyprland, "listar_ventanas", return_value=ventanas_abiertas), \
             mock.patch.object(ms.hyprland, "enviar_comando_socket", return_value=True) as mock_socket:

            res = await ms.procesar_mensaje(ms.ChatRequest(message="Hazlo de nuevo"), origen="pc")
            self.assertIn("escritorio 2", res.response)
            # Verifica que el comando contiene explícitamente el address original y NO el de la enfocada
            self.assertEqual(mock_socket.call_count, 1)
            payload_enviado = mock_socket.call_args[0][0]
            self.assertIn("address:0x1234", payload_enviado)
            self.assertNotIn("0x9999", payload_enviado)

    async def test_T33_REP_06c_consulta_ventanas_falla_bloquea_repeticion(self):
        """Fallo al consultar ventanas abiertas bloquea la repetición."""
        res_ok = Resultado(ok=True, mensaje="Ventana movida", evidencia={"address": "0x1234", "class": "firefox", "escritorio": 2}, estado=EstadoResultado.SUCCESS)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("mover_ventana", {"app_name": "esta ventana", "escritorio": 2}, res_ok, time.monotonic(), "pc")

        with mock.patch.object(ms.hyprland, "listar_ventanas", side_effect=RuntimeError("Error socket")), \
             mock.patch.object(ms.hyprland, "enviar_comando_socket") as mock_socket:

            res = await ms.procesar_mensaje(ms.ChatRequest(message="Hazlo de nuevo"), origen="pc")
            self.assertIn("No se pudo consultar la lista de ventanas", res.response)
            mock_socket.assert_not_called()

    def test_T33_REP_06d_despacho_hyprland_contiene_address_explicito(self):
        """Comando de despacho a Hyprland contiene explícitamente address:<address>."""
        payload_lua = ms.hyprland.payload_dispatch_move_address("0xABCD", 4)
        self.assertIn("address:0xABCD", payload_lua)
        cmd_lua = ms.hyprland.comando_hypr_movetoworkspacesilent_address("0xABCD", 4)
        self.assertTrue(any("address:0xABCD" in arg for arg in cmd_lua))

        with mock.patch.object(ms.hyprland, "usa_hyprland_lua", return_value=False):
            payload_legacy = ms.hyprland.payload_dispatch_move_address("0xABCD", 4)
            self.assertEqual(payload_legacy, "dispatch movetoworkspacesilent 4,address:0xABCD")
            cmd_legacy = ms.hyprland.comando_hypr_movetoworkspacesilent_address("0xABCD", 4)
            self.assertEqual(cmd_legacy, ["hyprctl", "dispatch", "movetoworkspacesilent", "4,address:0xABCD"])

    async def test_T33_REP_07_acciones_destructivas_exigen_confirmacion_en_repeticion(self):
        """Acciones destructivas exigen confirmación en repetición."""
        res_ok = Resultado(ok=True, mensaje="Sistema apagándose", estado=EstadoResultado.SUCCESS)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("energia", {"accion": "apagar"}, res_ok, time.monotonic(), "pc")

        with mock.patch.object(ms, "ejecutar_comando_local") as mock_cmd:
            res = await ms.procesar_mensaje(ms.ChatRequest(message="Hazlo de nuevo"), origen="pc")
            self.assertIn("Confirmas que quieres apagar el sistema", res.response)
            self.assertIsNotNone(ms.memoria_contexto.obtener_confirmacion("pc"))
            mock_cmd.assert_not_called()

    def test_T33_REP_07b_confirmacion_valida_exactamente_30s(self):
        """Confirmación destructiva es válida exactamente a los 30.0 segundos."""
        t0 = 1000.0
        with mock.patch.object(time, "monotonic", return_value=t0):
            ms.memoria_contexto.fijar_confirmacion({"accion": "apagar"}, origen="pc")

        with mock.patch.object(time, "monotonic", return_value=t0 + 30.0):
            cmd = ms.router_local("sí", origen="pc")
            self.assertEqual(cmd, [("energia", {"accion": "apagar"})])
            self.assertIsNone(ms.memoria_contexto.obtener_confirmacion("pc"))

    def test_T33_REP_07c_confirmacion_expirada_tras_30s(self):
        """Confirmación destructiva expira después de los 30.0 segundos."""
        t0 = 1000.0
        with mock.patch.object(time, "monotonic", return_value=t0):
            ms.memoria_contexto.fijar_confirmacion({"accion": "apagar"}, origen="pc")

        with mock.patch.object(time, "monotonic", return_value=t0 + 30.01):
            cmd = ms.router_local("sí", origen="pc")
            self.assertEqual(cmd, [])
            self.assertIsNone(ms.memoria_contexto.obtener_confirmacion("pc"))

    def test_T33_REP_07d_telefono_no_consume_confirmacion_pc(self):
        """Confirmación pendiente en PC no puede ser consumida desde teléfono."""
        ms.router_local("apaga el pc", origen="pc")
        self.assertEqual(ms.memoria_contexto.obtener_confirmacion("pc"), {"accion": "apagar"})
        self.assertIsNone(ms.memoria_contexto.obtener_confirmacion("telefono"))

        # Teléfono dice "sí"
        cmd_tel = ms.router_local("sí", origen="telefono")
        self.assertEqual(cmd_tel, [])
        self.assertEqual(ms.memoria_contexto.obtener_confirmacion("pc"), {"accion": "apagar"})

        # PC dice "sí" y sí consume
        cmd_pc = ms.router_local("sí", origen="pc")
        self.assertEqual(cmd_pc, [("energia", {"accion": "apagar"})])
        self.assertIsNone(ms.memoria_contexto.obtener_confirmacion("pc"))

    def test_T33_REP_07e_dos_confirmaciones_consecutivas_no_duplican_accion(self):
        """Dos confirmaciones afirmativas consecutivas no duplican la acción destructiva."""
        ms.router_local("apaga el pc", origen="pc")
        cmd1 = ms.router_local("sí", origen="pc")
        self.assertEqual(cmd1, [("energia", {"accion": "apagar"})])

        # Segundo "sí" consecutivo
        cmd2 = ms.router_local("sí", origen="pc")
        self.assertEqual(cmd2, [])

    async def test_T33_REP_07f_accion_destructiva_ejecutada_exige_nueva_confirmacion_al_repetir(self):
        """Acción destructiva ejecutada exige un nuevo ciclo completo de confirmación al repetirse."""
        res_ok = Resultado(ok=True, mensaje="Apagando", estado=EstadoResultado.SUCCESS)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("energia", {"accion": "apagar"}, res_ok, time.monotonic(), "pc")

        with mock.patch.object(ms, "ejecutar_comando_local", return_value="Apagando") as mock_exec:
            # Repetición exige confirmación
            res1 = await ms.procesar_mensaje(ms.ChatRequest(message="Hazlo de nuevo"), origen="pc")
            self.assertIn("Confirmas que quieres apagar el sistema", res1.response)
            mock_exec.assert_not_called()
            self.assertEqual(ms.memoria_contexto.obtener_confirmacion("pc"), {"accion": "apagar"})

            # Usuario confirma con "sí"
            res2 = await ms.procesar_mensaje(ms.ChatRequest(message="sí"), origen="pc")
            self.assertIn("Apagando", res2.response)
            mock_exec.assert_called_once_with("energia", {"accion": "apagar"})
            self.assertIsNone(ms.memoria_contexto.obtener_confirmacion("pc"))

    # ------------------------------------------------------------
    # BLOQUE C: Semántica de Estados PENDING y FAILURE
    # ------------------------------------------------------------
    async def test_T33_STA_01_estado_pending_bloquea_segundo_despacho(self):
        """Estado PENDING bloquea segundo despacho en segundo plano."""
        res_pending = Resultado(ok=True, mensaje="Abriendo Steam...", evidencia={"app": "steam"}, estado=EstadoResultado.PENDING)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("abrir_aplicacion", {"app_name": "steam"}, res_pending, time.monotonic(), "pc")

        with mock.patch.object(ms, "ejecutar_comando_local") as mock_cmd:
            res = await ms.procesar_mensaje(ms.ChatRequest(message="Repítelo"), origen="pc")
            self.assertIn("todavía se está iniciando", res.response)
            mock_cmd.assert_not_called()

    async def test_T33_STA_02_consulta_estado_pending_informa_carga(self):
        """Consulta sobre estado PENDING informa que se encuentra cargando."""
        res_pending = Resultado(ok=True, mensaje="Abriendo Steam...", evidencia={"app": "steam"}, estado=EstadoResultado.PENDING)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("abrir_aplicacion", {"app_name": "steam"}, res_pending, time.monotonic(), "pc")

        res = await ms.procesar_mensaje(ms.ChatRequest(message="¿Qué pasó?"), origen="pc")
        self.assertIn("cargando en segundo plano", res.response)

    # ------------------------------------------------------------
    # BLOQUE D: Robustez Adversarial y Negaciones
    # ------------------------------------------------------------
    async def test_T33_ADV_01_pregunta_hipotetica_destructiva_no_ejecuta(self):
        """Preguntas hipotéticas destructivas no ejecutan acciones."""
        cmd = ms.router_local("Si apago el PC con comandos, ¿se guardan mis pestañas?")
        self.assertEqual(cmd, [])

        tools = ms.tools_necesarias("Si apago el PC con comandos, ¿se guardan mis pestañas?")
        self.assertEqual([t["function"]["name"] for t in tools], ["buscar_en_web"])
        self.assertIsNone(ms.memoria_contexto.obtener_confirmacion("pc"))

    async def test_T33_ADV_02_orden_negada_bloqueo_triple_capa(self):
        """Orden negada bloqueada por router local, tools_necesarias y guardián de ejecución."""
        self.assertEqual(ms.router_local("No abras Steam"), [])

        tools = ms.tools_necesarias("No abras Steam")
        self.assertEqual([t["function"]["name"] for t in tools], ["buscar_en_web"])

        call_mock = mock.MagicMock()
        call_mock.id = "call_mock_steam"
        call_mock.function.name = "abrir_aplicacion"
        call_mock.function.arguments = json.dumps({"app_name": "steam"})

        with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("", tool_calls=[call_mock])):
            res = await ms.procesar_mensaje(ms.ChatRequest(message="No abras Steam"), origen="pc")
            self.assertIn("No se ejecutó ninguna acción porque tu consulta es una orden negada", res.response)

    async def test_T33_ADV_03_anafora_sin_referente_en_frio_pide_contexto(self):
        """Anáfora sin referente en frío pide contexto sin alucinar."""
        res = await ms.procesar_mensaje(ms.ChatRequest(message="¿Por qué hiciste eso?"), origen="pc")
        self.assertIn("No he hecho ninguna acción reciente, Keric. ¿A qué te refieres?", res.response)

    async def test_T33_ADV_04_inmunidad_falsos_positivos_conceptuales_n2_no_inyectado(self):
        """Sustantivos técnicos en preguntas conceptuales no inyectan contexto N2."""
        res_vol = Resultado(ok=True, mensaje="Volumen al 40%", estado=EstadoResultado.SUCCESS)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("volumen", {"porcentaje": 40}, res_vol, time.monotonic(), "pc")

        self.assertEqual(ms.contexto_para_pregunta("¿Qué es el volumen de una esfera?", "pc"), "")
        self.assertEqual(ms.contexto_para_pregunta("¿Cómo se calcula el volumen de un cubo?", "pc"), "")
        self.assertEqual(ms.contexto_para_pregunta("¿Qué es una ventana de Johari?", "pc"), "")
        self.assertEqual(ms.contexto_para_pregunta("¿Cómo funciona la pantalla táctil?", "pc"), "")

    # ------------------------------------------------------------
    # BLOQUE E: Concurrencia y Aislamiento por Dispositivo
    # ------------------------------------------------------------
    async def test_T33_CONC_01_solicitudes_simultaneas_pc_telefono_aisladas(self):
        """Peticiones simultáneas PC y Teléfono no interfieren sus historiales ni estados."""
        with mock.patch.object(ms, "abrir_aplicacion", return_value=Resultado(ok=True, mensaje="Firefox abierto", estado=EstadoResultado.SUCCESS)), \
             mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("La distancia es 384,400 km.")):

            t1 = asyncio.create_task(ms.procesar_mensaje(ms.ChatRequest(message="Abre Firefox"), origen="pc"))
            t2 = asyncio.create_task(ms.procesar_mensaje(ms.ChatRequest(message="¿Cuál es la distancia a la Luna?"), origen="telefono"))
            await asyncio.gather(t1, t2)

            self.assertEqual(len(ms.historiales_por_origen["telefono"]), 1)
            self.assertEqual(ms.historiales_por_origen["telefono"][0].usuario, "¿Cuál es la distancia a la Luna?")
            self.assertEqual(len(ms.historiales_por_origen["pc"]), 0)
            self.assertIsNotNone(ms.acciones_locales_recientes["pc"])
            self.assertIsNone(ms.acciones_locales_recientes["telefono"])

    async def test_T33_CONC_02_repeticion_telefono_no_accede_accion_pc(self):
        """Repetición en Teléfono no accede a acciones locales de PC."""
        res_vol = Resultado(ok=True, mensaje="Volumen al 70%", estado=EstadoResultado.SUCCESS)
        ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("volumen", {"accion": "subir", "porcentaje": 10}, res_vol, time.monotonic(), "pc")

        res_tel = await ms.procesar_mensaje(ms.ChatRequest(message="Hazlo de nuevo"), origen="telefono")
        self.assertIn("No tengo ninguna acción reciente para repetir", res_tel.response)

    async def test_T33_CONC_03_cancelacion_pc_libera_lock_y_hilo_tardio_no_corrompe(self):
        """Cancelación en PC libera lock y teléfono sigue funcionando sin demora."""
        import threading
        evento = threading.Event()

        def side_effect_bloqueado(*args, **kwargs):
            evento.wait(timeout=1.0)
            return mock_respuesta_groq("Respuesta tardía")

        self.addCleanup(evento.set)

        with mock.patch.object(ms.client.chat.completions, "create", side_effect=side_effect_bloqueado):
            task_pc = asyncio.create_task(ms.procesar_mensaje(ms.ChatRequest(message="Consulta larga PC"), origen="pc"))
            await asyncio.sleep(0.01)
            task_pc.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task_pc

        self.assertFalse(ms.obtener_lock_origen("pc").locked())

        with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("Respuesta teléfono OK")):
            res_tel = await ms.procesar_mensaje(ms.ChatRequest(message="Consulta teléfono"), origen="telefono")
            self.assertIn("Respuesta teléfono OK", res_tel.response)

    async def test_T33_CONC_04_ultimo_resultado_aislado_entre_pc_y_telefono(self):
        """ultimo_resultado está estrictamente aislado por origen sin contaminación cruzada."""
        # 1. Acción en PC fija ultimo_resultado en PC
        ms.memoria_contexto.fijar_ultimo_resultado("Resultado PC", origen="pc")
        self.assertEqual(ms.memoria_contexto.obtener_ultimo_resultado("pc"), "Resultado PC")
        self.assertIsNone(ms.memoria_contexto.obtener_ultimo_resultado("telefono"))

        # 2. Teléfono no tiene fallback ni contaminación desde PC
        ctx_tel = ms.contexto_para_pregunta("¿por qué dijiste esa respuesta?", origen="telefono")
        self.assertEqual(ctx_tel, "")

        # 3. PC sí accede a su propio resultado
        ctx_pc = ms.contexto_para_pregunta("¿por qué dijiste esa respuesta?", origen="pc")
        self.assertIn("Resultado PC", ctx_pc)

        # 4. Acción en teléfono fija su propio resultado sin contaminar PC
        ms.memoria_contexto.fijar_ultimo_resultado("Resultado Teléfono", origen="telefono")
        self.assertEqual(ms.memoria_contexto.obtener_ultimo_resultado("telefono"), "Resultado Teléfono")
        ctx_tel2 = ms.contexto_para_pregunta("¿por qué dijiste esa respuesta?", origen="telefono")
        self.assertIn("Resultado Teléfono", ctx_tel2)

        # 5. PC conserva su propio resultado inalterado
        ctx_pc2 = ms.contexto_para_pregunta("¿por qué dijiste esa respuesta?", origen="pc")
        self.assertIn("Resultado PC", ctx_pc2)
        self.assertNotIn("Resultado Teléfono", ctx_pc2)

    # ------------------------------------------------------------
    # BLOQUE F: Límites Temporales Exactos (Reloj Monotónico)
    # ------------------------------------------------------------
    async def test_T33_TIME_01_fronteras_ventana_caliente_tibia_59_60_61(self):
        """Límites exactos de ventana HOT (<= 60s) vs WARM (> 60s)."""
        t0 = 1000.0
        with mock.patch.object(time, "monotonic", return_value=t0):
            # 59s: Pregunta independiente en ventana caliente incluye turno previo
            ms.historiales_por_origen["pc"] = [
                ms.TurnoConversacion(usuario="Pregunta inicial", asistente="Respuesta inicial", timestamp=t0 - 59.0)
            ]
            with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("4")) as mock_groq:
                await ms.procesar_mensaje(ms.ChatRequest(message="¿Cuánto es 2 + 2?"), origen="pc")
                msgs = mock_groq.call_args.kwargs["messages"]
                self.assertTrue(any(m.get("content") == "Pregunta inicial" for m in msgs))

            # 60s exacto: Frontera inclusiva HOT
            ms.historiales_por_origen["pc"] = [
                ms.TurnoConversacion(usuario="Pregunta inicial", asistente="Respuesta inicial", timestamp=t0 - 60.0)
            ]
            with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("4")) as mock_groq:
                await ms.procesar_mensaje(ms.ChatRequest(message="¿Cuánto es 2 + 2?"), origen="pc")
                msgs = mock_groq.call_args.kwargs["messages"]
                self.assertTrue(any(m.get("content") == "Pregunta inicial" for m in msgs))

            # 61s: Ventana tibia excluye pregunta independiente sin anáfora
            ms.historiales_por_origen["pc"] = [
                ms.TurnoConversacion(usuario="Pregunta inicial", asistente="Respuesta inicial", timestamp=t0 - 61.0)
            ]
            with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("4")) as mock_groq:
                await ms.procesar_mensaje(ms.ChatRequest(message="¿Cuánto es 2 + 2?"), origen="pc")
                msgs = mock_groq.call_args.kwargs["messages"]
                self.assertFalse(any(m.get("content") == "Pregunta inicial" for m in msgs))

            # 61s: Ventana tibia incluye pregunta con anáfora
            ms.historiales_por_origen["pc"] = [
                ms.TurnoConversacion(usuario="Pregunta inicial", asistente="Respuesta inicial", timestamp=t0 - 61.0)
            ]
            with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("Explicación")) as mock_groq:
                await ms.procesar_mensaje(ms.ChatRequest(message="¿Y por qué dijiste eso?"), origen="pc")
                msgs = mock_groq.call_args.kwargs["messages"]
                self.assertTrue(any(m.get("content") == "Pregunta inicial" for m in msgs))

    async def test_T33_TIME_02_fronteras_vigencia_accion_local_119_120_121(self):
        """Límites exactos de vigencia de acción local (<= 120s)."""
        t0 = 1000.0
        res_ok = Resultado(ok=True, mensaje="Volumen al 50%", estado=EstadoResultado.SUCCESS)
        with mock.patch.object(time, "monotonic", return_value=t0):
            # 119s: Activo
            ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("volumen", {"porcentaje": 50}, res_ok, t0 - 119.0, "pc")
            self.assertIn("SUCCESS", ms.contexto_para_pregunta("¿Por qué hiciste eso?", "pc"))

            # 120s exacto: Frontera inclusiva activo
            ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("volumen", {"porcentaje": 50}, res_ok, t0 - 120.0, "pc")
            self.assertIn("SUCCESS", ms.contexto_para_pregunta("¿Por qué hiciste eso?", "pc"))

            # 121s: Expirado
            ms.acciones_locales_recientes["pc"] = ms.AccionLocalReciente("volumen", {"porcentaje": 50}, res_ok, t0 - 121.0, "pc")
            self.assertEqual(ms.contexto_para_pregunta("¿Por qué hiciste eso?", "pc"), "")

    async def test_T33_TIME_03_fronteras_ventana_fria_299_300_301(self):
        """Límites exactos de ventana fría (> 300s)."""
        t0 = 1000.0
        with mock.patch.object(time, "monotonic", return_value=t0):
            # 299s: WARM elegible para anáfora
            ms.historiales_por_origen["pc"] = [
                ms.TurnoConversacion(usuario="Pregunta previa", asistente="Respuesta previa", timestamp=t0 - 299.0)
            ]
            with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("Explicación")) as mock_groq:
                await ms.procesar_mensaje(ms.ChatRequest(message="¿Por qué?"), origen="pc")
                msgs = mock_groq.call_args.kwargs["messages"]
                self.assertTrue(any(m.get("content") == "Pregunta previa" for m in msgs))

            # 300s exacto: WARM elegible
            ms.historiales_por_origen["pc"] = [
                ms.TurnoConversacion(usuario="Pregunta previa", asistente="Respuesta previa", timestamp=t0 - 300.0)
            ]
            with mock.patch.object(ms.client.chat.completions, "create", return_value=mock_respuesta_groq("Explicación")) as mock_groq:
                await ms.procesar_mensaje(ms.ChatRequest(message="¿Por qué?"), origen="pc")
                msgs = mock_groq.call_args.kwargs["messages"]
                self.assertTrue(any(m.get("content") == "Pregunta previa" for m in msgs))

            # 301s: COLD -> purgado de RAM
            ms.historiales_por_origen["pc"] = [
                ms.TurnoConversacion(usuario="Pregunta previa", asistente="Respuesta previa", timestamp=t0 - 301.0)
            ]
            res = await ms.procesar_mensaje(ms.ChatRequest(message="¿Por qué?"), origen="pc")
            self.assertIn("No he hecho ninguna acción reciente, Keric. ¿A qué te refieres?", res.response)

    # ------------------------------------------------------------
    # BLOQUE G: Memoria Persistente y Scoring Léxico
    # ------------------------------------------------------------
    def test_T33_MEM_01_abstencion_ante_stopwords_cortesia(self):
        """Consultas compuestas solo por stopwords se abstienen inmediatamente."""
        import memoria
        self.assertEqual(memoria.buscar("Madeline, por favor, dime qué hora es"), [])

    async def test_T33_MEM_02_inhibicion_memoria_en_ordenes_operativas(self):
        """Órdenes operativas no consultan memoria a largo plazo."""
        with mock.patch.object(ms.memoria, "contexto_recuerdos") as mock_ctx_mem, \
             mock.patch.object(ms, "ejecutar_comando_local", return_value="ok"):
            await ms.procesar_mensaje(ms.ChatRequest(message="Abre la terminal"), origen="pc")
            mock_ctx_mem.assert_not_called()

    def test_T33_MEM_03_termino_unico_excluye_conversacion_y_recupera_dato(self):
        """Consulta con término único excluye recuerdos conversacionales y recupera dato conciso."""
        import memoria
        memoria.guardar("Estoy aprendiendo Flutter", "dato")
        memoria.guardar("Keric preguntó: Usuario habló largo rato — Madeline respondió: Conversación extensa sobre Flutter y otros temas de desarrollo móvil", "conversacion")

        candidatos = memoria._por_palabras_clave("Flutter")
        self.assertTrue(len(candidatos) >= 1)
        self.assertTrue(all(c[2] == "dato" for c in candidatos))

        resultados = memoria.buscar("Flutter")
        self.assertTrue(len(resultados) >= 1)
        self.assertTrue(any("Estoy aprendiendo Flutter" in r for r in resultados))

    def test_T33_MEM_04_resolucion_empates_prioriza_dato(self):
        """Resolución de empates léxicos prioriza tipo 'dato' sobre 'conversacion'."""
        import memoria
        memoria.guardar("Kotlin y Android", "dato")
        memoria.guardar("Keric preguntó: Charla Android — Madeline respondió: Kotlin y Android", "conversacion")

        resultados = memoria._por_palabras_clave("Kotlin Android")
        self.assertTrue(len(resultados) >= 2)
        self.assertEqual(resultados[0][2], "dato")

    # ------------------------------------------------------------
    # BLOQUE H: Manejo de Correcciones del Usuario
    # ------------------------------------------------------------
    async def test_T33_CORR_01_correccion_accion_ejecutada_no_finge_cancelacion(self):
        """Corrección sobre acción ejecutada abre nuevo objetivo sin fingir deshacer."""
        with mock.patch.object(ms, "abrir_aplicacion", side_effect=[
            Resultado(ok=True, mensaje="Steam abierto", estado=EstadoResultado.SUCCESS),
            Resultado(ok=True, mensaje="Firefox abierto", estado=EstadoResultado.SUCCESS),
        ]):
            await ms.procesar_mensaje(ms.ChatRequest(message="Abre Steam"), origen="pc")
            res_corr = await ms.procesar_mensaje(ms.ChatRequest(message="No, quise decir Firefox"), origen="pc")

            self.assertIn("Firefox", res_corr.response)
            self.assertNotIn("Steam fue cancelado", res_corr.response)
            self.assertEqual(ms.acciones_locales_recientes["pc"].args["app_name"], "Firefox")

    async def test_T33_CORR_02_correccion_ambigua_pide_clarificacion(self):
        """Corrección ambigua sin objetivo pide aclaración sin ejecutar herramientas."""
        with mock.patch.object(ms, "ejecutar_comando_local") as mock_cmd:
            res1 = await ms.procesar_mensaje(ms.ChatRequest(message="No, eso no"), origen="pc")
            self.assertIn("¿qué prefieres que haga?", res1.response)
            mock_cmd.assert_not_called()

            res2 = await ms.procesar_mensaje(ms.ChatRequest(message="No, me refería a otra cosa"), origen="pc")
            self.assertIn("¿qué prefieres que haga?", res2.response)
            mock_cmd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
