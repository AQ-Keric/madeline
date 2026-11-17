"""Tests para el servicio de comunicación nativa con Hyprland vía UNIX Sockets."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import entorno  # noqa: F401
from services import hyprland


class TestHyprlandIPC(unittest.TestCase):
    def test_usa_hyprland_lua(self):
        # Verifica que la detección no falle
        self.assertIsInstance(hyprland.usa_hyprland_lua(), bool)

    def test_payloads_lua(self):
        with mock.patch.object(hyprland, "usa_hyprland_lua", return_value=True):
            self.assertEqual(
                hyprland.payload_dispatch_exec("kitty"),
                'dispatch hl.dsp.exec_cmd("kitty")',
            )
            self.assertEqual(
                hyprland.payload_dispatch_workspace(2),
                "dispatch hl.dsp.focus({ workspace = '2' })",
            )
            self.assertEqual(
                hyprland.payload_dispatch_move("vesktop", 3),
                "dispatch hl.dsp.window.move({ window = 'class:.*vesktop.*', workspace = 3, follow = false })",
            )

    def test_payloads_legacy(self):
        with mock.patch.object(hyprland, "usa_hyprland_lua", return_value=False):
            self.assertEqual(
                hyprland.payload_dispatch_exec("kitty"),
                "dispatch exec kitty",
            )
            self.assertEqual(
                hyprland.payload_dispatch_workspace(2),
                "dispatch workspace 2",
            )
            self.assertEqual(
                hyprland.payload_dispatch_move("vesktop", 3),
                "dispatch movetoworkspacesilent 3,class:.*vesktop.*",
            )

    @mock.patch.object(hyprland, "obtener_ruta_socket", return_value="/tmp/test.sock")
    @mock.patch("socket.socket")
    def test_enviar_comando_socket_exitoso(self, mock_socket_cls, mock_ruta):
        mock_sock = mock.MagicMock()
        mock_sock.recv.side_effect = [b"ok", b""]
        mock_socket_cls.return_value.__enter__.return_value = mock_sock

        resp = hyprland.enviar_comando_socket("dispatch test")
        self.assertEqual(resp, "ok")
        mock_sock.sendall.assert_called_once_with(b"dispatch test")

    @mock.patch.object(hyprland, "obtener_ruta_socket", return_value="/tmp/test.sock")
    @mock.patch("socket.socket")
    def test_leer_escritorios_socket(self, mock_socket_cls, mock_ruta):
        mock_sock1 = mock.MagicMock()
        mock_sock1.recv.side_effect = [json.dumps([{"id": 1}, {"id": 2}]).encode("utf-8"), b""]

        mock_sock2 = mock.MagicMock()
        mock_sock2.recv.side_effect = [json.dumps({"id": 2}).encode("utf-8"), b""]

        mock_socket_cls.return_value.__enter__.side_effect = [mock_sock1, mock_sock2]

        escritorios, activo = hyprland.leer_escritorios()
        self.assertEqual(escritorios, [1, 2])
        self.assertEqual(activo, 2)


if __name__ == "__main__":
    unittest.main()
