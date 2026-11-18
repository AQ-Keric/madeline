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


if __name__ == "__main__":
    unittest.main()
