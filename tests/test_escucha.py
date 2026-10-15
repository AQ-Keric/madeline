"""Tests de la palabra de activación (sin micrófono ni modelo de Vosk)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import madeline_escucha as e  # noqa: E402


class TestHayOrden(unittest.TestCase):
    def test_frase_de_activacion(self):
        self.assertTrue(e.hay_orden("madeline sube el volumen", True))
        self.assertTrue(e.hay_orden("oye madeleine qué hora es", True))
        self.assertFalse(e.hay_orden("madeline", True))
        self.assertFalse(e.hay_orden("oye madeline", True))
        # Lo que se dijo ANTES de "Madeline" no es la orden.
        self.assertFalse(e.hay_orden("ayer vi a madeline", True))
        # Vosk corrigió "madeline" por "madrid" al cerrar la frase.
        self.assertFalse(e.hay_orden("madrid", True))

    def test_frase_siguiente(self):
        self.assertTrue(e.hay_orden("pon música", False))
        self.assertFalse(e.hay_orden("", False))

    def test_palabra_activacion(self):
        self.assertTrue(e.tiene_palabra_activacion("oye madeline pon música"))
        self.assertFalse(e.tiene_palabra_activacion("magdalena fue al mercado"))
        self.assertFalse(e.tiene_palabra_activacion("mandela línea de código"))


if __name__ == "__main__":
    unittest.main()
