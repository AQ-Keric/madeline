"""Qué tools recibe Groq cuando el router local no entendió la orden.

Nada de esto llama a Groq: solo revisa qué schemas se mandarían.
"""
import os
import unittest

import entorno  # noqa: F401
import madeline_server as j

MUSICA = {"control_multimedia", "info_cancion", "abrir_radio_musica"}


def nombres(texto):
    return {t["function"]["name"] for t in j.tools_necesarias(texto)}


class TestTemasTools(unittest.TestCase):
    def test_frases_de_musica_reciben_el_grupo_completo(self):
        for frase in (
            "pon esa canción de nuevo", "no me gusta esta canción",
            "qué opinas de la música electrónica", "me encanta cuando pausan la música",
            "quiero escuchar algo tranquilo", "detén eso", "dale play a lo de antes",
            "qué está sonando ahora", "abre spotify y ponle algo",
        ):
            with self.subTest(frase=frase):
                self.assertTrue(MUSICA <= nombres(frase), nombres(frase))

    def test_otros_temas(self):
        casos = {
            "silencio por favor": {"ajustar_volumen"},
            "cierra eso": {"abrir_aplicacion", "cerrar_aplicacion"},
            "¿va a llover mañana?": {"reporte_clima"},
            "cuánta RAM estoy usando": {"estado_sistema"},
            "conecta los audífonos": {"conectar_bluetooth"},
            "¿qué ves en la pantalla?": {"analizar_pantalla"},
            "busca quién ganó el partido": {"buscar_en_web"},
        }
        for frase, esperadas in casos.items():
            with self.subTest(frase=frase):
                self.assertTrue(esperadas <= nombres(frase), nombres(frase))

    def test_palabras_parecidas_no_activan_temas(self):
        # "abril" no es "abrir", "rama" no es "RAM", "tiempo" solo no es clima.
        for frase in (
            "cuánto tiempo falta para abril", "una rama del árbol",
            "para qué sirve una mandarina", "cuéntame un chiste",
        ):
            with self.subTest(frase=frase):
                self.assertEqual(j.temas_del_mensaje(frase), [])

    def test_sin_tema_manda_solo_busqueda_y_queda_registrada(self):
        if os.path.exists(j.LOG_SIN_TEMA):
            os.remove(j.LOG_SIN_TEMA)
        self.assertEqual(nombres("cuéntame un chiste"), {"buscar_en_web"})
        with open(j.LOG_SIN_TEMA, encoding="utf-8") as f:
            self.assertTrue(f.read().rstrip().endswith("\tcuéntame un chiste"))

    def test_con_tema_no_se_registra(self):
        if os.path.exists(j.LOG_SIN_TEMA):
            os.remove(j.LOG_SIN_TEMA)
        nombres("pon esa canción de nuevo")
        self.assertFalse(os.path.exists(j.LOG_SIN_TEMA))

    def test_todos_los_temas_apuntan_a_tools_reales(self):
        reales = {t["function"]["name"] for t in j.tools}
        for tema, (_, tools) in j.TEMAS_TOOLS.items():
            self.assertTrue(set(tools) <= reales, tema)


if __name__ == "__main__":
    unittest.main()
