"""Tests de la memoria a largo plazo (SQLite temporal, embeddings falsos)."""
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import entorno
import madeline_server as j
import memoria


def respuesta_groq_texto(texto: str):
    mensaje = SimpleNamespace(tool_calls=None, content=texto)
    return SimpleNamespace(choices=[SimpleNamespace(message=mensaje, finish_reason="stop")])


def textos_guardados():
    return [f[0] for f in memoria._db().execute("SELECT texto FROM recuerdos ORDER BY id")]


class TestModuloMemoria(unittest.TestCase):
    def setUp(self):
        entorno.vaciar_memoria()

    def test_busca_lo_relevante_y_nada_mas(self):
        memoria.guardar("mi hermana se llama Ana")
        memoria.guardar("estoy aprendiendo Flutter para la app")
        self.assertIn("hermana", memoria.contexto_recuerdos("¿cómo se llama mi hermana?"))
        self.assertEqual(memoria.contexto_recuerdos("capital de Francia"), "")

    def test_dato_casi_igual_no_se_duplica(self):
        memoria.guardar("mi hermana se llama Ana")
        memoria.guardar("mi hermana se llama Ana")
        self.assertEqual(textos_guardados(), ["mi hermana se llama Ana"])

    def test_olvidar(self):
        memoria.guardar("mi perro se llama Toby")
        self.assertEqual(memoria.olvidar("mi perro"), "mi perro se llama Toby")
        self.assertEqual(textos_guardados(), [])
        self.assertIsNone(memoria.olvidar("la luna"))

    def test_olvidar_borra_charlas_que_repiten_el_dato(self):
        memoria.guardar("mi color favorito es el morado")
        memoria.guardar_conversacion("¿qué fondo me recomiendas?", "Uno morado, tu color favorito.")
        memoria.guardar_conversacion("¿qué es Flutter?", "Un framework de Google.")
        memoria.olvidar("mi color favorito")
        self.assertEqual(len(textos_guardados()), 1)
        self.assertIn("Flutter", textos_guardados()[0])

    def test_conversacion_reciente_no_se_repite(self):
        # Ya va en historial_chat: mandarla como recuerdo gastaría tokens dos veces.
        memoria.guardar_conversacion("¿qué es un transformer?", "Una arquitectura de redes neuronales.")
        self.assertEqual(memoria.buscar("transformer redes neuronales"), [])
        memoria._db().execute("UPDATE recuerdos SET creado = ?", (time.time() - 86400,))
        memoria._cache = None
        self.assertEqual(len(memoria.buscar("transformer redes neuronales")), 1)

    def test_sin_ollama_usa_palabras_clave(self):
        with mock.patch.object(memoria, "_embedding", return_value=None):
            memoria.guardar("mi hermana se llama Ana")
            self.assertIn("hermana", memoria.contexto_recuerdos("¿cómo se llama mi hermana?"))
        self.assertIsNone(memoria._db().execute("SELECT embedding FROM recuerdos").fetchone()[0])
        memoria.rellenar_embeddings()
        self.assertIsNotNone(memoria._db().execute("SELECT embedding FROM recuerdos").fetchone()[0])

    def test_recuerdo_largo_se_recorta(self):
        memoria.guardar("Flutter es " + "x" * 400)
        recuerdo = memoria.buscar("Flutter")[0]
        self.assertLess(len(recuerdo), memoria.MAX_CARACTERES_RECUERDO + 20)

    def test_detecta_datos_personales(self):
        casos = {
            "estoy aprendiendo Flutter": "estoy aprendiendo Flutter",
            "abre firefox, mi perro se llama Toby": "mi perro se llama Toby",
            "no me gusta el reggaetón": "no me gusta el reggaetón",
            "vivo en Talca": "vivo en Talca",
        }
        for texto, esperado in casos.items():
            self.assertEqual(memoria.dato_personal(texto), esperado, texto)
        for texto in ["¿cómo se llama mi perro?", "sube el volumen", "me gusta",
                      "tengo hambre", "pon música"]:
            self.assertIsNone(memoria.dato_personal(texto), texto)


class TestRouterMemoria(unittest.TestCase):
    def test_pedidos(self):
        casos = {
            "recuerda que mi hermana se llama Ana": ("memoria_guardar", {"dato": "mi hermana se llama Ana"}),
            "Madeline, acuérdate de que el lunes tengo dentista": ("memoria_guardar", {"dato": "el lunes tengo dentista"}),
            "olvida lo de el dentista": ("memoria_olvidar", {"tema": "el dentista"}),
            "¿qué recuerdas de mi hermana?": ("memoria_consultar", {"tema": "mi hermana"}),
            "¿qué sabes de mí?": ("memoria_consultar", {"tema": ""}),
        }
        for texto, esperado in casos.items():
            self.assertEqual(j.router_local(texto), [esperado], texto)

    def test_no_choca_con_otras_ordenes(self):
        self.assertEqual(j.router_local("recuérdame en 10 minutos sacar la ropa")[0][0], "recordatorio")
        # "¿qué sabes de X?" es una pregunta general, va a Groq.
        self.assertIsNone(j.pedido_de_memoria("¿qué sabes de la segunda guerra mundial?"))
        self.assertIsNone(j.pedido_de_memoria("¿recuerdas cuándo fue la revolución francesa?"))


class TestMemoriaEnConversacion(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        entorno.vaciar_memoria()
        j.historial_chat.clear()
        parche = mock.patch.object(j, "procesar_salida_de_voz", mock.AsyncMock(return_value=None))
        parche.start()
        self.addCleanup(parche.stop)

    async def test_recuerda_sin_groq_y_luego_lo_usa(self):
        with mock.patch.object(j.client.chat.completions, "create",
                               return_value=respuesta_groq_texto("Se llama Ana.")) as create:
            await j.procesar_mensaje(j.ChatRequest(message="recuerda que mi hermana se llama Ana"))
            create.assert_not_called()

            await j.procesar_mensaje(j.ChatRequest(message="¿cómo se llama mi hermana?"))

        sistema = [m["content"] for m in create.call_args.kwargs["messages"] if m["role"] == "system"]
        self.assertTrue(any("mi hermana se llama Ana" in s for s in sistema))

    async def test_sin_recuerdos_relevantes_no_agrega_nada(self):
        memoria.guardar("mi hermana se llama Ana")
        with mock.patch.object(j.client.chat.completions, "create",
                               return_value=respuesta_groq_texto("París.")) as create:
            await j.procesar_mensaje(j.ChatRequest(message="¿cuál es la capital de Francia?"))
        sistema = [m for m in create.call_args.kwargs["messages"] if m["role"] == "system"]
        self.assertEqual(len(sistema), 1)

    async def test_guarda_charlas_y_datos_pero_no_ordenes(self):
        with mock.patch.object(j.client.chat.completions, "create",
                               return_value=respuesta_groq_texto("¡Qué bien! Es un gran framework.")):
            await j.procesar_mensaje(j.ChatRequest(message="estoy aprendiendo Flutter, ¿qué opinas?"))
        with mock.patch.object(j, "ajustar_volumen", return_value="Volumen al 50%"):
            await j.procesar_mensaje(j.ChatRequest(message="sube el volumen"))

        guardados = textos_guardados()
        self.assertIn("estoy aprendiendo Flutter", guardados)
        self.assertTrue(any(t.startswith("Keric preguntó: estoy aprendiendo Flutter") for t in guardados))
        self.assertFalse(any("volumen" in t for t in guardados))


if __name__ == "__main__":
    unittest.main()
