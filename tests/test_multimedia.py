"""Las órdenes de música se resuelven en local con muchas formas de decirlas."""
import unittest
from unittest import mock  # noqa: F401

import entorno  # noqa: F401
import madeline_server as j
from frases_multimedia import ESPERADO, NO_MUSICA, PREFIJOS

MUSICA = {"multimedia", "info_cancion", "radio_musica"}


def accion(comandos):
    for nombre, args in comandos or []:
        if nombre in MUSICA:
            return nombre, args.get("accion")
    return None


class TestFrasesMultimedia(unittest.TestCase):
    def test_todas_las_frases(self):
        fallos = []
        for esperado, frases in ESPERADO.items():
            for frase in frases:
                for prefijo in PREFIJOS:
                    for texto in (prefijo + frase, (prefijo + frase).capitalize() + "?" if esperado[0] == "info_cancion" else prefijo + frase + "."):
                        j.memoria_contexto["confirmacion_pendiente"] = None
                        obtenido = accion(j.router_local(texto))
                        if obtenido != esperado:
                            fallos.append(f"{texto!r}: {obtenido} (esperado {esperado})")
        self.assertEqual(fallos, [], f"{len(fallos)} fallos:\n" + "\n".join(fallos[:80]))

    def test_no_son_ordenes_de_musica(self):
        fallos = [f for f in NO_MUSICA for t in (f, f.capitalize() + "?") if accion(j.router_local(t))]
        self.assertEqual(fallos, [])


if __name__ == "__main__":
    unittest.main()


class TestControlMultimedia(unittest.TestCase):
    def test_play_y_pausa_no_alternan(self):
        from unittest import mock
        with mock.patch.object(j, "hay_reproductor", return_value=True), \
             mock.patch.object(j.subprocess, "run") as run:
            j.control_multimedia("reproducir")
            j.control_multimedia("pausar")
            j.control_multimedia("reiniciar")
        comandos = [c.args[0] for c in run.call_args_list]
        self.assertEqual(comandos, [["playerctl", "play"], ["playerctl", "pause"], ["playerctl", "position", "0"]])

    def test_play_sin_reproductor_abre_la_radio(self):
        from unittest import mock
        with mock.patch.object(j, "hay_reproductor", return_value=False), \
             mock.patch.object(j, "abrir_radio_musica", return_value="Abriendo tu radio en YouTube Music") as radio:
            resultado = j.control_multimedia("reproducir")
        radio.assert_called_once()
        self.assertNotIn("de nuevo", j.respuesta_local("multimedia", {"accion": "reproducir"}, resultado))
