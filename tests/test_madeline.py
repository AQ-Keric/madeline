"""Tests del router local y de las protecciones de seguridad.

Correr desde la raíz del proyecto:
    .venv/bin/python -m unittest discover tests

No llaman a Groq ni ejecutan nada en el sistema: subprocess y el
cliente de Groq se reemplazan por mocks.
"""
import os
import shlex
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.setdefault("GROQ_API_KEY", "test")

import madeline_server as j  # noqa: E402


def reiniciar_estado():
    j.historial_chat.clear()
    for clave in j.memoria_contexto:
        j.memoria_contexto[clave] = None


class TestRouterLocal(unittest.TestCase):
    def setUp(self):
        reiniciar_estado()

    def test_volumen(self):
        self.assertEqual(
            j.router_local("sube el volumen 20"),
            [("volumen", {"accion": "subir", "porcentaje": 20})],
        )
        self.assertEqual(
            j.router_local("pon el volumen en 50"),
            [("volumen", {"accion": "setear", "porcentaje": 50})],
        )

    def test_varias_apps_en_una_frase(self):
        self.assertEqual(
            j.router_local("abre discord y steam"),
            [
                ("abrir_aplicacion", {"app_name": "discord"}),
                ("abrir_aplicacion", {"app_name": "steam"}),
            ],
        )

    def test_termina_no_se_corrige_a_terminal(self):
        self.assertEqual(
            j.router_local("termina discord"),
            [("cerrar_aplicacion", {"app_name": "discord"})],
        )

    def test_mover_ventana(self):
        self.assertEqual(
            j.router_local("mueve discord al escritorio 3"),
            [("mover_ventana", {"app_name": "discord", "escritorio": 3})],
        )

    def test_apagar_pide_confirmacion_y_si_confirma(self):
        self.assertEqual(
            j.router_local("apaga el pc"),
            [("confirmar_accion", {"accion": "apagar"})],
        )
        self.assertEqual(
            j.router_local("sí"),
            [("energia", {"accion": "apagar"})],
        )
        self.assertIsNone(j.memoria_contexto["confirmacion_pendiente"])

    def test_apagar_cancelado(self):
        j.router_local("reinicia el computador")
        self.assertEqual(
            j.router_local("no, cancela"),
            [("confirmacion_cancelada", {})],
        )
        self.assertIsNone(j.memoria_contexto["confirmacion_pendiente"])


@mock.patch.object(j.subprocess, "run")
@mock.patch.object(j.subprocess, "Popen")
class TestListaBlancaApps(unittest.TestCase):
    def test_abrir_app_conocida_usa_nombre_tecnico(self, popen, run):
        j.abrir_aplicacion("discord")
        popen.assert_called_once_with(
            ["hyprctl", "dispatch", "exec", "vesktop"]
        )

    def test_abrir_app_desconocida_no_ejecuta_nada(self, popen, run):
        resultado = j.abrir_aplicacion("kitty; rm -rf ~")
        popen.assert_not_called()
        self.assertTrue(resultado.startswith("No pude"))

    def test_abrir_con_escritorio_no_usa_shell(self, popen, run):
        with mock.patch.object(j.threading, "Timer") as timer:
            j.abrir_aplicacion("discord", escritorio=2)

        popen.assert_called_once_with(
            ["hyprctl", "dispatch", "exec", "vesktop"]
        )
        _, kwargs = timer.call_args
        self.assertEqual(
            kwargs["args"][0],
            ["hyprctl", "dispatch", "movetoworkspacesilent",
             "2,class:.*vesktop.*"],
        )

    def test_escritorio_invalido_no_ejecuta_nada(self, popen, run):
        resultado = j.abrir_aplicacion("discord", escritorio="2; rm -rf ~")
        popen.assert_not_called()
        self.assertTrue(resultado.startswith("Error"))

    def test_cerrar_app_desconocida_no_llama_pkill(self, popen, run):
        resultado = j.cerrar_aplicacion(".")
        run.assert_not_called()
        self.assertTrue(resultado.startswith("No pude"))

    def test_mover_app_desconocida_no_ejecuta_nada(self, popen, run):
        resultado = j.mover_ventana("lo que sea", 2)
        run.assert_not_called()
        self.assertTrue(resultado.startswith("No pude"))


@mock.patch.object(j.subprocess, "Popen")
class TestUrlsNavegador(unittest.TestCase):
    def test_url_con_comillas_queda_escapada(self, popen):
        url = 'https://ejemplo.com/"; rm -rf ~ #$(whoami)'
        self.assertTrue(j.abrir_url_en_navegador(url))

        comando = popen.call_args[0][0][3]
        # La shell que usa hyprctl tiene que ver exactamente 3 palabras:
        # navegador, -new-tab y la URL intacta.
        self.assertEqual(
            shlex.split(comando), ["zen-browser", "-new-tab", url]
        )

    def test_solo_http_https(self, popen):
        self.assertFalse(j.abrir_url_en_navegador("file:///etc/passwd"))
        self.assertFalse(j.abrir_url_en_navegador("javascript:alert(1)"))
        popen.assert_not_called()


def respuesta_groq_con_tool(nombre: str, argumentos: str):
    tool_call = SimpleNamespace(
        function=SimpleNamespace(name=nombre, arguments=argumentos)
    )
    mensaje = SimpleNamespace(tool_calls=[tool_call], content=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=mensaje, finish_reason="tool_calls")]
    )


class TestEnergiaPorGroq(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reiniciar_estado()
        parches = [
            mock.patch.object(j, "control_energia", return_value="Apagando."),
            mock.patch.object(
                j, "procesar_salida_de_voz", mock.AsyncMock(return_value=None)
            ),
            mock.patch.object(
                j.client.chat.completions, "create",
                return_value=respuesta_groq_con_tool(
                    "control_energia", '{"accion": "apagar"}'
                ),
            ),
        ]
        self.control_energia = parches[0].start()
        parches[1].start()
        parches[2].start()
        for parche in parches:
            self.addCleanup(parche.stop)

    async def test_tool_de_groq_no_apaga_sin_confirmar(self):
        # Esta frase no la agarra el router local, así que va a Groq.
        self.assertEqual(j.router_local("apaga todo por favor"), [])

        respuesta = await j.procesar_mensaje(
            j.ChatRequest(message="apaga todo por favor"), origen="pc"
        )

        self.control_energia.assert_not_called()
        self.assertIn("Confirma", respuesta.response)
        self.assertEqual(
            j.memoria_contexto["confirmacion_pendiente"], {"accion": "apagar"}
        )

        await j.procesar_mensaje(j.ChatRequest(message="sí"), origen="pc")
        self.control_energia.assert_called_once_with(accion="apagar")


class TestOrdenesCortas(unittest.TestCase):
    """Órdenes que el router debe resolver local (0 tokens) y
    preguntas que antes secuestraba y ahora tienen que ir a Groq."""

    ORDENES = {
        "Madeline, pausa la música.": [("multimedia", {"accion": "pausar"})],
        "para la música": [("multimedia", {"accion": "pausar"})],
        "¿Puedes pausar la música?": [("multimedia", {"accion": "pausar"})],
        "continúa": [("multimedia", {"accion": "reproducir"})],
        "siguiente canción": [("multimedia", {"accion": "siguiente"})],
        "canción anterior": [("multimedia", {"accion": "anterior"})],
        "súbele": [("volumen", {"accion": "subir", "porcentaje": 10})],
        "bájale un poco": [("volumen", {"accion": "bajar", "porcentaje": 10})],
        "sube el volumen un 20%": [("volumen", {"accion": "subir", "porcentaje": 20})],
        "baja el volumen al 30%": [("volumen", {"accion": "setear", "porcentaje": 30})],
        "volumen al 40": [("volumen", {"accion": "setear", "porcentaje": 40})],
        "mutea el audio": [("volumen", {"accion": "mutear"})],
        "toma una captura": [("captura", {})],
        "captura de pantalla": [("captura", {})],
        "¿qué clima hace?": [("clima", {"dia": "ahora"})],
        "¿cómo está el clima mañana?": [("clima", {"dia": "mañana"})],
        "¿va a llover hoy?": [("clima", {"dia": "hoy"})],
        "pronóstico para pasado mañana": [("clima", {"dia": "pasado mañana"})],
        "gracias madeline": [("charla", {"tipo": "gracias"})],
        "Hola.": [("charla", {"tipo": "saludo"})],
        "abre discord y pausa la música": [
            ("multimedia", {"accion": "pausar"}),
            ("abrir_aplicacion", {"app_name": "discord"}),
        ],
    }

    PREGUNTAS = [
        "¿cuál es el siguiente feriado en Chile?",
        "no tengo tiempo para eso",
        "¿qué es una captura de movimiento?",
        "sube el brillo",
        "reduce el texto a un resumen",
        "explícame qué es una pausa activa",
        "¿cuál es el volumen de una esfera de radio 3?",
        "para qué sirve una api",
        "continúa la explicación",
        "¿qué clima hará el lunes?",
        "gracias por la explicación de antes, ¿y lo otro?",
    ]

    def setUp(self):
        reiniciar_estado()

    def test_ordenes(self):
        for frase, esperado in self.ORDENES.items():
            with self.subTest(frase=frase):
                self.assertEqual(j.router_local(frase), esperado)

    def test_preguntas_van_a_groq(self):
        for frase in self.PREGUNTAS:
            with self.subTest(frase=frase):
                self.assertEqual(j.router_local(frase), [])

    def test_sin_doble_punto(self):
        texto = j.resumir_resultados_locales(
            [("clima", {"dia": "ahora"}, "Ahora mismo: despejado, con 20 grados.")]
        )
        self.assertNotIn("..", texto)


WTTR_FALSO = {
    "current_condition": [{"temp_C": "10", "lang_es": [{"value": "Lluvia ligera"}]}],
    "weather": [
        {
            "mintempC": "6", "maxtempC": "15",
            "hourly": [
                {"chanceofrain": str(p), "lang_es": [{"value": "Nublado"}]}
                for p in (0, 10, 74, 30, 5, 0, 0, 0)
            ],
        },
        {
            "mintempC": "8", "maxtempC": "20",
            "hourly": [
                {"chanceofrain": "0", "lang_es": [{"value": "Soleado"}]}
                for _ in range(8)
            ],
        },
    ],
}


class TestClima(unittest.TestCase):
    def setUp(self):
        parche = mock.patch.object(
            j.requests, "get",
            return_value=mock.Mock(status_code=200, json=lambda: WTTR_FALSO),
        )
        parche.start()
        self.addCleanup(parche.stop)

    def test_actual(self):
        self.assertEqual(
            j.reporte_clima(),
            "Ahora mismo en Villa Alegre: lluvia ligera, con 10 grados.",
        )

    def test_pronostico_con_lluvia(self):
        texto = j.reporte_clima("hoy")
        self.assertIn("mínima de 6 y máxima de 15", texto)
        self.assertIn("74%", texto)

    def test_pronostico_sin_lluvia(self):
        self.assertIn("No se espera lluvia", j.reporte_clima("mañana"))


def respuesta_groq_texto(texto: str):
    mensaje = SimpleNamespace(tool_calls=None, content=texto)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=mensaje, finish_reason="stop")]
    )


def error_sin_cuota():
    import httpx
    return j.RateLimitError(
        "rate limit",
        response=httpx.Response(429, request=httpx.Request("POST", "https://x")),
        body=None,
    )


class TestGroq(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reiniciar_estado()
        parche = mock.patch.object(
            j, "procesar_salida_de_voz", mock.AsyncMock(return_value=None)
        )
        parche.start()
        self.addCleanup(parche.stop)

    async def test_busqueda_web_se_resume(self):
        snippets = "Resultados web para 'x':\n1. Argentina ganó en 2022.\n"
        llamadas = [
            respuesta_groq_con_tool("buscar_en_web", '{"consulta": "mundial 2022"}'),
            respuesta_groq_texto("Lo ganó Argentina, Keric."),
        ]

        buscar = mock.Mock(return_value=snippets)
        with mock.patch.dict(j.TOOLS_FUNCIONES, {"buscar_en_web": buscar}), \
             mock.patch.object(j.client.chat.completions, "create",
                               side_effect=llamadas) as create:
            respuesta = await j.procesar_mensaje(
                j.ChatRequest(message="¿quién ganó el mundial 2022?"), origen="pc"
            )

        buscar.assert_called_once_with(consulta="mundial 2022")
        self.assertEqual(respuesta.response, "Lo ganó Argentina, Keric.")
        # Los snippets llegan a la segunda llamada.
        self.assertIn("Argentina ganó en 2022", str(create.call_args_list[1]))
        # La segunda llamada es la mínima: sin tools ni historial.
        segunda = create.call_args_list[1].kwargs
        self.assertNotIn("tools", segunda)
        self.assertEqual(len(segunda["messages"]), 2)

    async def test_busqueda_sin_cuota_lee_primer_resultado(self):
        snippets = "Resultados web para 'x':\n1. Argentina ganó en 2022.\n"
        llamadas = [
            respuesta_groq_con_tool("buscar_en_web", '{"consulta": "x"}'),
            error_sin_cuota(),
        ]

        buscar = mock.Mock(return_value=snippets)
        with mock.patch.dict(j.TOOLS_FUNCIONES, {"buscar_en_web": buscar}), \
             mock.patch.object(j.client.chat.completions, "create",
                               side_effect=llamadas):
            respuesta = await j.procesar_mensaje(
                j.ChatRequest(message="¿quién ganó el mundial?"), origen="pc"
            )

        self.assertIn("Argentina ganó en 2022", respuesta.response)
        self.assertNotIn("Resultados web", respuesta.response)

    async def test_sin_cuota_responde_en_vez_de_error_500(self):
        with mock.patch.object(j.client.chat.completions, "create",
                               side_effect=error_sin_cuota()):
            respuesta = await j.procesar_mensaje(
                j.ChatRequest(message="¿qué es un agujero negro?"), origen="pc"
            )

        self.assertIn("cuota", respuesta.response)
        # La pregunta sin responder no queda colgada en el historial.
        self.assertEqual(j.historial_chat, [])

    async def test_charla_no_llama_a_groq(self):
        with mock.patch.object(j.client.chat.completions, "create") as create:
            await j.procesar_mensaje(j.ChatRequest(message="gracias madeline"), origen="pc")
        create.assert_not_called()

    async def test_historial_guarda_pregunta_anterior(self):
        with mock.patch.object(j.client.chat.completions, "create",
                               return_value=respuesta_groq_texto("Respuesta.")) as create:
            await j.procesar_mensaje(j.ChatRequest(message="¿quién es Tesla?"), origen="pc")
            await j.procesar_mensaje(j.ChatRequest(message="¿y cuándo nació?"), origen="pc")

        mensajes = create.call_args.kwargs["messages"]
        contenidos = [m["content"] for m in mensajes if m["role"] != "system"]
        self.assertEqual(contenidos, ["¿quién es Tesla?", "Respuesta.", "¿y cuándo nació?"])


class TestToken(unittest.TestCase):
    """TestClient se conecta como host "testclient", o sea, como si
    viniera de la red (no del mismo PC)."""

    def setUp(self):
        from fastapi.testclient import TestClient
        parche = mock.patch.object(j, "MADELINE_TOKEN", "secreto")
        parche.start()
        self.addCleanup(parche.stop)
        self.cliente = TestClient(j.app)

    def test_sin_token_rechaza(self):
        self.assertEqual(self.cliente.get("/").status_code, 401)
        self.assertEqual(self.cliente.post("/callar").status_code, 401)

    def test_token_incorrecto_rechaza(self):
        r = self.cliente.get("/", headers={"X-Madeline-Token": "otro"})
        self.assertEqual(r.status_code, 401)

    def test_token_correcto_pasa(self):
        r = self.cliente.get("/", headers={"X-Madeline-Token": "secreto"})
        self.assertEqual(r.status_code, 200)

    def test_capturas_y_audios_no_piden_token(self):
        # 404 (no existe), no 401: pasó el control de token.
        self.assertEqual(self.cliente.get("/captura/noexiste").status_code, 404)
        self.assertEqual(self.cliente.get("/audios/noexiste.mp3").status_code, 404)

    def test_desde_el_mismo_pc_no_pide_token(self):
        from fastapi.testclient import TestClient
        local = TestClient(j.app, client=("127.0.0.1", 50000))
        self.assertEqual(local.get("/").status_code, 200)


class TestCapturaSegunOrigen(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reiniciar_estado()
        parches = [
            mock.patch.object(j, "tomar_captura", return_value="CAPTURA_LISTA:abc"),
            mock.patch.object(j, "ruta_captura_disponible", return_value="/tmp/x.jpg"),
            mock.patch.object(j, "enviar_captura_telegram"),
            mock.patch.object(j, "procesar_salida_de_voz", mock.AsyncMock(return_value=None)),
        ]
        for parche in parches:
            parche.start()
            self.addCleanup(parche.stop)

    async def test_desde_telefono_no_va_a_telegram(self):
        r = await j.procesar_mensaje(j.ChatRequest(message="toma una captura"), origen="telefono")
        self.assertEqual(r.image_path, "/captura/abc")
        j.enviar_captura_telegram.assert_not_called()

    async def test_desde_pc_va_a_telegram(self):
        await j.procesar_mensaje(j.ChatRequest(message="toma una captura"), origen="pc")
        j.enviar_captura_telegram.assert_called_once_with("abc")


class TestEfectoVoz(unittest.IsolatedAsyncioTestCase):
    async def test_si_ffmpeg_falla_queda_la_voz_limpia(self):
        import tempfile

        class VozFalsa:
            def __init__(self, *a, **k):
                pass

            async def save(self, ruta):
                with open(ruta, "wb") as f:
                    f.write(b"voz")

        with tempfile.TemporaryDirectory() as carpeta, \
             mock.patch.object(j.edge_tts, "Communicate", VozFalsa), \
             mock.patch.object(j, "EFECTO_VOZ", "sutil"), \
             mock.patch.dict(j.FILTROS_VOZ, {"sutil": "filtro_que_no_existe"}):
            salida = os.path.join(carpeta, "r.mp3")
            await j.generar_voz("hola", salida)

            with open(salida, "rb") as f:
                self.assertEqual(f.read(), b"voz")
            self.assertEqual(os.listdir(carpeta), ["r.mp3"])


@mock.patch.object(j.subprocess, "run")
class TestCerrarTodo(unittest.TestCase):
    def test_informa_que_cerro(self, run):
        run.return_value = mock.Mock(returncode=0)
        self.assertIn("navegador", j.cerrar_todo())

    def test_nada_abierto(self, run):
        run.return_value = mock.Mock(returncode=1)
        self.assertEqual(j.cerrar_todo(), "No había nada que cerrar.")


if __name__ == "__main__":
    unittest.main()
