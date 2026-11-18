"""Pruebas unitarias para el servicio services.audio y su compatibilidad en la fachada."""

import subprocess
import sys
import unittest
from unittest import mock

import madeline_server as ms
from services import audio


class TestServicesAudio(unittest.TestCase):
    """Verifica las 4 funciones de services.audio de forma aislada."""

    def test_ajustar_volumen_comandos_y_args(self):
        """Verifica que ajustar_volumen invoque pamixer con los argumentos exactos."""
        with mock.patch("services.audio.subprocess.run") as mock_run:
            # 1. Mutear general
            res = audio.ajustar_volumen("mutear")
            mock_run.assert_called_with(["pamixer", "-t"], check=True)
            self.assertEqual(res, "Volumen general muteado o desmuteado.")

            # 2. Mutear aplicación
            res = audio.ajustar_volumen("mutear", aplicacion="spotify")
            mock_run.assert_called_with(
                ["pamixer", "--stream", "--toggle-mute", "--sink-input", "spotify"],
                check=True,
            )
            self.assertEqual(res, "Se cambió el mute de la aplicación: spotify.")

            # 3. Setear general
            res = audio.ajustar_volumen("setear", porcentaje=45)
            mock_run.assert_called_with(["pamixer", "--set-volume", "45"], check=True)
            self.assertEqual(res, "Volumen general fijado exactamente al 45%.")

            # 4. Setear aplicación
            res = audio.ajustar_volumen("setear", porcentaje=30, aplicacion="vlc")
            mock_run.assert_called_with(
                ["pamixer", "--stream", "--set-volume", "30", "--sink-input", "vlc"],
                check=True,
            )
            self.assertEqual(res, "Volumen fijado exactamente al 30% en vlc.")

            # 5. Subir general y aplicación
            res = audio.ajustar_volumen("subir", porcentaje=15)
            mock_run.assert_called_with(["pamixer", "-i", "15"], check=True)
            self.assertEqual(res, "Volumen general subido un 15%.")

            res = audio.ajustar_volumen("subir", porcentaje=20, aplicacion="discord")
            mock_run.assert_called_with(
                ["pamixer", "--stream", "-i", "20", "--sink-input", "discord"],
                check=True,
            )
            self.assertEqual(res, "Volumen subido un 20% en discord.")

            # 6. Bajar general y aplicación
            res = audio.ajustar_volumen("bajar", porcentaje=10)
            mock_run.assert_called_with(["pamixer", "-d", "10"], check=True)
            self.assertEqual(res, "Volumen general bajado un 10%.")

            res = audio.ajustar_volumen("bajar", porcentaje=5, aplicacion="zen")
            mock_run.assert_called_with(
                ["pamixer", "--stream", "-d", "5", "--sink-input", "zen"],
                check=True,
            )
            self.assertEqual(res, "Volumen bajado un 5% en zen.")

            # 7. Acción no reconocida
            res = audio.ajustar_volumen("invalida")
            self.assertEqual(res, "Acción no reconocida.")

    def test_ajustar_volumen_manejo_excepciones(self):
        """Verifica que ajustar_volumen capture errores de pamixer sin lanzar excepciones."""
        with mock.patch("services.audio.subprocess.run", side_effect=RuntimeError("pamixer falló")):
            res = audio.ajustar_volumen("subir", 10)
            self.assertEqual(res, "Error ejecutando pamixer: pamixer falló")

    def test_hay_reproductor(self):
        """Verifica la detección de reproductor activo mediante el returncode de playerctl."""
        mock_proc = mock.Mock()
        mock_proc.returncode = 0
        with mock.patch("services.audio.subprocess.run", return_value=mock_proc) as mock_run:
            self.assertTrue(audio.hay_reproductor())
            mock_run.assert_called_with(["playerctl", "status"], capture_output=True, text=True)

        mock_proc.returncode = 1
        with mock.patch("services.audio.subprocess.run", return_value=mock_proc):
            self.assertFalse(audio.hay_reproductor())

    def test_info_cancion(self):
        """Verifica la lectura de metadatos de playerctl y casos límite."""
        # Caso exitoso
        with mock.patch("services.audio.subprocess.check_output", return_value=b"Queen - Bohemian Rhapsody\n"):
            res = audio.info_cancion()
            self.assertEqual(res, "Actualmente está sonando: Queen - Bohemian Rhapsody")

        # Caso sin canción (cadena vacía)
        with mock.patch("services.audio.subprocess.check_output", return_value=b""):
            res = audio.info_cancion()
            self.assertEqual(res, "Al parecer no hay ninguna canción reproduciéndose.")

        # Caso sin canción (guión)
        with mock.patch("services.audio.subprocess.check_output", return_value=b" - \n"):
            res = audio.info_cancion()
            self.assertEqual(res, "Al parecer no hay ninguna canción reproduciéndose.")

        # Caso excepción
        with mock.patch("services.audio.subprocess.check_output", side_effect=Exception("sin reproductor")):
            res = audio.info_cancion()
            self.assertEqual(res, "Pucha, no pude leer la canción: sin reproductor")

    def test_control_multimedia_acciones_estandar(self):
        """Verifica el despacho de comandos de reproducción a playerctl."""
        with mock.patch("services.audio.subprocess.run") as mock_run:
            # Pausar
            res = audio.control_multimedia("pausar")
            mock_run.assert_called_with(["playerctl", "pause"], check=True)
            self.assertEqual(res, "Se ejecutó la acción: pausar.")

            # Siguiente
            res = audio.control_multimedia("siguiente")
            mock_run.assert_called_with(["playerctl", "next"], check=True)
            self.assertEqual(res, "Se ejecutó la acción: siguiente.")

            # Reiniciar
            res = audio.control_multimedia("reiniciar")
            mock_run.assert_called_with(["playerctl", "position", "0"], check=True)
            self.assertEqual(res, "Se ejecutó la acción: reiniciar.")

            # Acción inválida
            res = audio.control_multimedia("desconocida")
            self.assertIn("Error: La acción 'desconocida' no es válida", res)

    def test_control_multimedia_anterior_doble_toque(self):
        """Verifica que la acción anterior ejecute doble llamada con sleep de 0.1s."""
        with mock.patch("services.audio.subprocess.run") as mock_run, \
             mock.patch("services.audio.time.sleep") as mock_sleep:
            res = audio.control_multimedia("anterior")
            self.assertEqual(mock_run.call_args_list, [
                mock.call(["playerctl", "previous"], check=True),
                mock.call(["playerctl", "previous"], check=True),
            ])
            mock_sleep.assert_called_once_with(0.1)
            self.assertEqual(res, "Se saltó a la canción anterior con doble toque.")

    def test_control_multimedia_reproducir_con_y_sin_reproductor(self):
        """Verifica la rama 'reproducir' con callbacks opcionales."""
        # Con reproductor activo
        with mock.patch("services.audio.subprocess.run") as mock_run, \
             mock.patch("services.audio.hay_reproductor", return_value=True):
            res = audio.control_multimedia("reproducir")
            mock_run.assert_called_with(["playerctl", "play"], check=True)
            self.assertEqual(res, "Se ejecutó la acción: reproducir.")

        # Sin reproductor activo y con callback abrir_radio_fn
        mock_radio = mock.Mock(return_value="Abriendo radio test")
        with mock.patch("services.audio.hay_reproductor", return_value=False):
            res = audio.control_multimedia("reproducir", abrir_radio_fn=mock_radio)
            mock_radio.assert_called_once()
            self.assertEqual(res, "Abriendo radio test")

        # Sin reproductor activo y sin callback abrir_radio_fn
        with mock.patch("services.audio.hay_reproductor", return_value=False):
            res = audio.control_multimedia("reproducir")
            self.assertEqual(res, "No hay ningún reproductor activo para reanudar.")

    def test_control_multimedia_excepciones(self):
        """Verifica que fallos de playerctl devuelvan mensaje de error sin propagar excepción."""
        with mock.patch("services.audio.subprocess.run", side_effect=Exception("playerctl error")):
            res = audio.control_multimedia("pausar")
            self.assertEqual(res, "Pucha, no pude controlar la música: playerctl error")


class TestFachadaMadelineServerAudio(unittest.TestCase):
    """Verifica que madeline_server reexporte y delegue con late-binding hacia services.audio."""

    def test_identidad_simbolos_reexportados(self):
        """Las funciones directas deben tener identidad idéntica con el servicio."""
        self.assertIs(ms.ajustar_volumen, audio.ajustar_volumen)
        self.assertIs(ms.hay_reproductor, audio.hay_reproductor)
        self.assertIs(ms.info_cancion, audio.info_cancion)

    def test_firma_publica_control_multimedia_fachada(self):
        """La fachada de control_multimedia debe recibir exactamente un argumento (accion)."""
        import inspect
        sig = inspect.signature(ms.control_multimedia)
        self.assertEqual(list(sig.parameters.keys()), ["accion"])

    def test_late_binding_mocks_en_fachada(self):
        """Mocks sobre ms.hay_reproductor y ms.abrir_radio_musica deben ser respetados por ms.control_multimedia."""
        with mock.patch.object(ms, "hay_reproductor", return_value=False), \
             mock.patch.object(ms, "abrir_radio_musica", return_value="RADIO_DESDE_FACHADA_MOCK") as mock_radio:
            res = ms.control_multimedia("reproducir")
            mock_radio.assert_called_once()
            self.assertEqual(res, "RADIO_DESDE_FACHADA_MOCK")


class TestAislamientoProcesoLimpio(unittest.TestCase):
    """Verifica que services.audio se importe limpiamente en un proceso aislado."""

    def test_import_independiente_sin_madeline_server(self):
        """El subproceso no debe registrar madeline_server ni FastAPI en sys.modules."""
        cmd = [
            sys.executable,
            "-c",
            "import sys, services.audio; "
            "assert 'madeline_server' not in sys.modules; "
            "assert 'fastapi' not in sys.modules; "
            "print('OK_CLEAN_IMPORT')",
        ]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            env={"PYTHONPATH": "server"},
        )
        self.assertEqual(res.returncode, 0, f"Error en import limpio: {res.stderr}")
        self.assertIn("OK_CLEAN_IMPORT", res.stdout)


if __name__ == "__main__":
    unittest.main()
