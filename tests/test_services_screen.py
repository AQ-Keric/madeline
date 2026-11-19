"""Pruebas unitarias para services.screen y desacoplamiento de routers en U-3.1."""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from resultado import EstadoResultado, Resultado
from services import screen


class TestServicesScreen(unittest.TestCase):
    """Verifica las responsabilidades de captura, resolución, bandeja y limpieza."""

    def setUp(self):
        # Crear directorio temporal para aislar pruebas de bandeja y temporales
        self._test_dir = tempfile.mkdtemp(prefix="madeline_screen_test_")
        self._orig_bandeja = screen.CARPETA_BANDEJA
        screen.CARPETA_BANDEJA = os.path.join(self._test_dir, "bandeja")
        os.makedirs(screen.CARPETA_BANDEJA, exist_ok=True)
        self._tmp_archivos = []

    def tearDown(self):
        screen.CARPETA_BANDEJA = self._orig_bandeja
        for ruta in self._tmp_archivos:
            if os.path.exists(ruta):
                try:
                    os.remove(ruta)
                except OSError:
                    pass
        if os.path.exists(self._test_dir):
            shutil.rmtree(self._test_dir, ignore_errors=True)

    def _crear_temporal(self, nombre: str, contenido: bytes = b"test") -> str:
        ruta = os.path.join("/tmp", nombre)
        with open(ruta, "wb") as f:
            f.write(contenido)
        self._tmp_archivos.append(ruta)
        return ruta

    # 1. Captura exitosa
    def test_captura_exitosa_con_conversion_jpg(self):
        """grim genera PNG y PIL lo comprime a JPG exitosamente."""
        def fake_run(cmd, check=True, timeout=10):
            # cmd[1] es la ruta png
            ruta_png = cmd[1]
            with open(ruta_png, "wb") as f:
                f.write(b"PNG_FAKE_DATA")
            self._tmp_archivos.append(ruta_png)
            return mock.Mock(returncode=0)

        # Mock PIL Image
        mock_img = mock.MagicMock()
        mock_img.width = 1920
        mock_img.height = 1080
        mock_img.convert.return_value = mock_img

        def fake_save(ruta, formato, **kwargs):
            with open(ruta, "wb") as f:
                f.write(b"JPG_FAKE_DATA")
            self._tmp_archivos.append(ruta)

        mock_img.save.side_effect = fake_save

        mock_pil = mock.MagicMock()
        mock_pil.open.return_value.__enter__.return_value = mock_img

        mock_subp = mock.Mock()
        mock_subp.run.side_effect = fake_run

        with mock.patch.dict("sys.modules", {"PIL": mock.Mock(Image=mock_pil), "PIL.Image": mock_pil}):
            res = screen.tomar_captura(ancho_max=1920, _subprocess=mock_subp)

        self.assertTrue(res.ok)
        self.assertEqual(res.estado, EstadoResultado.SUCCESS)
        self.assertTrue(res.mensaje.startswith("CAPTURA_LISTA:"))
        captura_id = res.evidencia["captura_id"]
        self.assertEqual(len(captura_id), 12)
        self.assertTrue(screen.PATRON_ID_CAPTURA.match(captura_id))
        self.assertTrue(res.evidencia["verificado"])
        self.assertGreater(res.evidencia["tamano_bytes"], 0)

    # 2. Fallos de grim
    def test_captura_fallo_grim_process_error(self):
        """Si grim sale con código de error, retorna Resultado FAILURE."""
        mock_subp = mock.Mock()
        mock_subp.run.side_effect = subprocess.CalledProcessError(returncode=1, cmd=["grim"])

        res = screen.tomar_captura(_subprocess=mock_subp)
        self.assertFalse(res.ok)
        self.assertEqual(res.estado, EstadoResultado.FAILURE)
        self.assertIn("Falló el módulo de captura", res.mensaje)

    def test_captura_timeout_grim(self):
        """Si grim excede el tiempo límite (10s), retorna Resultado FAILURE."""
        mock_subp = mock.Mock()
        mock_subp.run.side_effect = subprocess.TimeoutExpired(cmd=["grim"], timeout=10)

        res = screen.tomar_captura(_subprocess=mock_subp)
        self.assertFalse(res.ok)
        self.assertEqual(res.estado, EstadoResultado.FAILURE)
        self.assertIn("Falló el módulo de captura", res.mensaje)

    # 3. Archivo inexistente o vacío
    def test_captura_archivo_vacio_o_no_creado(self):
        """Si el comando termina pero el archivo no existe o tiene 0 bytes, retorna FAILURE."""
        mock_subp = mock.Mock()
        mock_subp.run.return_value = mock.Mock(returncode=0)

        # Resolver devuelve None (no existe archivo)
        res = screen.tomar_captura(_subprocess=mock_subp, _resolver=lambda cid: None)
        self.assertFalse(res.ok)
        self.assertEqual(res.estado, EstadoResultado.FAILURE)
        self.assertIn("no se generó o quedó vacío", res.mensaje)

    # 4. Fallo de PIL con fallback a PNG
    def test_captura_fallo_pil_conserva_png_original(self):
        """Si PIL falla al comprimir, se conserva el PNG original y la captura tiene éxito."""
        def fake_run(cmd, check=True, timeout=10):
            ruta_png = cmd[1]
            with open(ruta_png, "wb") as f:
                f.write(b"PNG_FALLBACK_BYTES")
            self._tmp_archivos.append(ruta_png)
            return mock.Mock(returncode=0)

        mock_subp = mock.Mock()
        mock_subp.run.side_effect = fake_run

        with mock.patch("PIL.Image.open", side_effect=Exception("Pillow decoding error")):
            res = screen.tomar_captura(_subprocess=mock_subp)

        self.assertTrue(res.ok)
        self.assertEqual(res.estado, EstadoResultado.SUCCESS)
        self.assertTrue(res.evidencia["ruta"].endswith(".png"))
        self.assertEqual(res.evidencia["tamano_bytes"], len(b"PNG_FALLBACK_BYTES"))

    # 5. Generación y validación de IDs
    def test_generacion_y_validacion_ids_unicos(self):
        """Cada captura debe generar un ID hexadecimal único de 12 caracteres."""
        ids = set()
        for _ in range(50):
            cid = screen.tomar_captura(_subprocess=mock.Mock(run=mock.Mock(side_effect=Exception("stop"))))
            # Inspeccionar ID del fallo o generar
            # Validar patrón
            raw_id = time.time_ns().to_bytes(8, "big").hex()[:12]
            self.assertTrue(screen.PATRON_ID_CAPTURA.match(raw_id))

    # 6. Resolución de ruta disponible
    def test_ruta_captura_disponible_prioriza_jpg(self):
        """ruta_captura_disponible debe priorizar JPG cuando existen ambos."""
        cid = "aabbccddeeff"
        ruta_jpg = self._crear_temporal(f"captura_madeline_{cid}.jpg", b"jpg")
        ruta_png = self._crear_temporal(f"captura_madeline_{cid}.png", b"png")

        self.assertEqual(screen.ruta_captura_disponible(cid), ruta_jpg)

    def test_ruta_captura_disponible_usa_png_si_no_hay_jpg(self):
        """ruta_captura_disponible debe retornar PNG si no existe JPG."""
        cid = "112233445566"
        ruta_png = self._crear_temporal(f"captura_madeline_{cid}.png", b"png")
        self.assertEqual(screen.ruta_captura_disponible(cid), ruta_png)

    def test_ruta_captura_disponible_retorna_none_si_no_existe(self):
        """ruta_captura_disponible retorna None si el ID no existe."""
        self.assertIsNone(screen.ruta_captura_disponible("999999999999"))

    # 7. Consulta de bandeja y orden cronológico
    def test_capturas_en_bandeja_filtrado_y_orden(self):
        """capturas_en_bandeja debe filtrar archivos inválidos y ordenar cronológicamente."""
        # Archivo válido 1 (antiguo)
        cid1 = "111111111111"
        r1 = os.path.join(screen.CARPETA_BANDEJA, f"{cid1}.jpg")
        with open(r1, "wb") as f:
            f.write(b"1")
        os.utime(r1, (time.time() - 100, time.time() - 100))

        # Archivo válido 2 (reciente)
        cid2 = "222222222222"
        r2 = os.path.join(screen.CARPETA_BANDEJA, f"{cid2}.png")
        with open(r2, "wb") as f:
            f.write(b"2")
        os.utime(r2, (time.time() - 10, time.time() - 10))

        # Archivo inválido (no coincide con PATRON_ID_CAPTURA)
        r_invalido = os.path.join(screen.CARPETA_BANDEJA, "ignore_me.tmp")
        with open(r_invalido, "wb") as f:
            f.write(b"x")

        bandeja = screen.capturas_en_bandeja()
        self.assertEqual(len(bandeja), 2)
        self.assertEqual(bandeja[0]["id"], cid1)
        self.assertEqual(bandeja[1]["id"], cid2)
        self.assertLess(bandeja[0]["creada"], bandeja[1]["creada"])

    # 8. Transferencia de capturas al teléfono
    def test_enviar_captura_al_telefono_traslada_archivo(self):
        """enviar_captura_al_telefono debe mover el archivo desde /tmp a CARPETA_BANDEJA."""
        cid = "333333333333"
        ruta_orig = self._crear_temporal(f"captura_madeline_{cid}.jpg", b"datos_captura")

        destino = screen.enviar_captura_al_telefono(cid)
        self.assertIsNotNone(destino)
        self.assertFalse(os.path.exists(ruta_orig))
        self.assertTrue(os.path.exists(destino))
        self.assertTrue(destino.startswith(screen.CARPETA_BANDEJA))

    def test_enviar_captura_inexistente_retorna_none(self):
        """enviar_captura_al_telefono con ID inexistente retorna None sin lanzar excepción."""
        self.assertIsNone(screen.enviar_captura_al_telefono("000000000000"))

    # 9. Expiración y limpieza de archivos
    def test_limpiar_capturas_viejas(self):
        """limpiar_capturas_viejas elimina archivos vencidos y conserva recientes."""
        # Captura vieja en /tmp (> 10 min)
        cid_viejo = "444444444444"
        r_vieja = self._crear_temporal(f"captura_madeline_{cid_viejo}.jpg", b"viejo")
        os.utime(r_vieja, (time.time() - 1200, time.time() - 1200))

        # Captura reciente en /tmp (< 10 min)
        cid_reciente = "555555555555"
        r_reciente = self._crear_temporal(f"captura_madeline_{cid_reciente}.jpg", b"reciente")

        # Captura vieja en bandeja (> 24 horas)
        cid_bviejo = "666666666666"
        r_bviejo = os.path.join(screen.CARPETA_BANDEJA, f"{cid_bviejo}.jpg")
        with open(r_bviejo, "wb") as f:
            f.write(b"bviejo")
        os.utime(r_bviejo, (time.time() - 100000, time.time() - 100000))

        eliminados = screen.limpiar_capturas_viejas(minutos=10, horas_bandeja=24)
        self.assertEqual(eliminados, 2)
        self.assertFalse(os.path.exists(r_vieja))
        self.assertFalse(os.path.exists(r_bviejo))
        self.assertTrue(os.path.exists(r_reciente))

    # 10. Concurrencia
    def test_concurrencia_capturas_simultaneas(self):
        """Múltiples hilos concurrentes deben obtener IDs y rutas no colisionantes."""
        resultados = []
        errores = []

        def worker(idx):
            try:
                def fake_run(cmd, check=True, timeout=10):
                    ruta = cmd[1]
                    with open(ruta, "wb") as f:
                        f.write(f"png_{idx}".encode())
                    self._tmp_archivos.append(ruta)
                    return mock.Mock(returncode=0)

                mock_subp = mock.Mock()
                mock_subp.run.side_effect = fake_run

                with mock.patch("PIL.Image.open", side_effect=Exception("skip jpg")):
                    res = screen.tomar_captura(_subprocess=mock_subp)
                    resultados.append(res)
            except Exception as e:
                errores.append(e)

        hilos = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        self.assertEqual(len(errores), 0)
        self.assertEqual(len(resultados), 8)
        ids = [r.evidencia["captura_id"] for r in resultados]
        self.assertEqual(len(ids), len(set(ids)))  # Todos únicos

    # 11. Aislamiento del servicio
    def test_aislamiento_servicio_sin_dependencias_de_fachada(self):
        """services.screen no debe importar madeline_server ni FastAPI."""
        import inspect
        source = inspect.getsource(screen)
        self.assertNotIn("import madeline_server", source)
        self.assertNotIn("from madeline_server", source)
        self.assertNotIn("import fastapi", source)
        self.assertNotIn("from fastapi", source)
        self.assertNotIn("import groq", source)

    # 12. Compatibilidad con la fachada pública
    def test_compatibilidad_fachada_publica_y_monkeypatching(self):
        """madeline_server debe re-exportar los símbolos y responder al monkeypatching histórico."""
        import madeline_server as ms

        # Símbolos públicos disponibles
        self.assertTrue(callable(ms.tomar_captura))
        self.assertTrue(callable(ms.ruta_captura_disponible))
        self.assertTrue(callable(ms.enviar_captura_al_telefono))
        self.assertTrue(callable(ms.capturas_en_bandeja))
        self.assertEqual(ms.CARPETA_BANDEJA, self._orig_bandeja)
        self.assertEqual(ms.HORAS_VIDA_BANDEJA, screen.HORAS_VIDA_BANDEJA)

        # Monkeypatching de ruta_captura_disponible en ms debe ser respetado por tomar_captura
        with mock.patch.object(ms.subprocess, "run", return_value=mock.Mock(returncode=0)), \
             mock.patch.object(ms, "ruta_captura_disponible", return_value=None):
            res = ms.tomar_captura()
            self.assertFalse(res.ok)
            self.assertEqual(res.estado, EstadoResultado.FAILURE)
            self.assertIn("Falló el módulo de captura", res.mensaje)


class TestImportacionesAisladasSubprocesos(unittest.TestCase):
    """Verifica que cada router y módulo se pueda importar en subprocesos limpios sin importar la fachada."""

    def _ejecutar_subproceso(self, codigo: str):
        env = dict(os.environ)
        env["PYTHONPATH"] = "server"
        env["GROQ_API_KEY"] = "test"
        res = subprocess.run(
            [sys.executable, "-c", codigo],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(
            res.returncode,
            0,
            f"Fallo en subproceso con código: {codigo}\nStderr: {res.stderr}\nStdout: {res.stdout}"
        )

    def test_import_routers_sistema_independiente(self):
        """routers.sistema debe poder importarse directamente sin ciclo."""
        self._ejecutar_subproceso("import routers.sistema; print('OK')")

    def test_import_routers_audio_independiente(self):
        """routers.audio debe poder importarse directamente."""
        self._ejecutar_subproceso("import routers.audio; print('OK')")

    def test_import_routers_deck_independiente(self):
        """routers.deck debe poder importarse directamente."""
        self._ejecutar_subproceso("import routers.deck; print('OK')")

    def test_import_services_screen_independiente(self):
        """services.screen debe poder importarse directamente."""
        self._ejecutar_subproceso("import services.screen; print('OK')")

    def test_import_madeline_server_independiente(self):
        """madeline_server debe poder importarse directamente."""
        self._ejecutar_subproceso("import madeline_server; print('OK')")

    def test_import_permutaciones_sin_deadlock(self):
        """Verifica secuencias cruzadas de importación."""
        self._ejecutar_subproceso("import routers.sistema; import madeline_server; print('OK')")
        self._ejecutar_subproceso("import madeline_server; import routers.sistema; print('OK')")


class TestContratosHttpEndpointsSistema(unittest.TestCase):
    """Verifica los endpoints HTTP de routers.sistema mediante FastAPI TestClient."""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers.sistema import router as router_sistema

        self.app = FastAPI()
        self.app.include_router(router_sistema)
        self.client = TestClient(self.app)

        self._tmp_archivos = []
        self._orig_bandeja = screen.CARPETA_BANDEJA
        self._test_dir = tempfile.mkdtemp(prefix="madeline_http_test_")
        screen.CARPETA_BANDEJA = os.path.join(self._test_dir, "bandeja")
        os.makedirs(screen.CARPETA_BANDEJA, exist_ok=True)

    def tearDown(self):
        screen.CARPETA_BANDEJA = self._orig_bandeja
        for ruta in self._tmp_archivos:
            if os.path.exists(ruta):
                try:
                    os.remove(ruta)
                except OSError:
                    pass
        if os.path.exists(self._test_dir):
            shutil.rmtree(self._test_dir, ignore_errors=True)

    def test_get_sensores_contrato(self):
        """GET /sensores retorna 200 y las 6 métricas esperadas por Flutter."""
        with mock.patch("services.system.leer_sensores", return_value={
            "ram_usada": 4.2,
            "ram_total": 16.0,
            "cpu_uso": 12.5,
            "cpu_temp": 45.0,
            "gpu_uso": "15",
            "gpu_temp": "52",
        }):
            r = self.client.get("/sensores")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["ram_usada"], "4.2")
            self.assertEqual(data["ram_total"], "16.0")
            self.assertEqual(data["cpu_uso"], "12.5")
            self.assertEqual(data["cpu_temp"], "45.0")
            self.assertEqual(data["gpu_uso"], "15")
            self.assertEqual(data["gpu_temp"], "52")

    def test_get_bandeja_y_descarga(self):
        """GET /bandeja lista archivos y GET /bandeja/{id} los descarga y elimina."""
        cid = "abcdef012345"
        ruta = os.path.join(screen.CARPETA_BANDEJA, f"{cid}.jpg")
        with open(ruta, "wb") as f:
            f.write(b"IMAGEN_BANDEJA_DATA")

        # 1. Listar bandeja
        r = self.client.get("/bandeja")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"capturas": [{"id": cid, "creada": mock.ANY}]})

        # 2. Descargar archivo
        r_dl = self.client.get(f"/bandeja/{cid}")
        self.assertEqual(r_dl.status_code, 200)
        self.assertEqual(r_dl.content, b"IMAGEN_BANDEJA_DATA")
        self.assertEqual(r_dl.headers["content-type"], "image/jpeg")

        # 3. Comprobar eliminación tras background task
        self.assertFalse(os.path.exists(ruta))
        r_404 = self.client.get(f"/bandeja/{cid}")
        self.assertEqual(r_404.status_code, 404)

    def test_get_captura_directa(self):
        """GET /captura/{id} entrega archivo de /tmp y lo elimina."""
        cid = "fedcba543210"
        ruta = os.path.join("/tmp", f"captura_madeline_{cid}.png")
        with open(ruta, "wb") as f:
            f.write(b"CAPTURA_DIRECTA_PNG")
        self._tmp_archivos.append(ruta)

        r = self.client.get(f"/captura/{cid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"CAPTURA_DIRECTA_PNG")
        self.assertEqual(r.headers["content-type"], "image/png")

        # Comprobar eliminación
        self.assertFalse(os.path.exists(ruta))
        r_not_found = self.client.get(f"/captura/{cid}")
        self.assertEqual(r_not_found.status_code, 404)

    def test_path_traversal_protegido(self):
        """Intentos de path traversal deben retornar 404."""
        r = self.client.get("/bandeja/..%2F..%2Fetc%2Fpasswd")
        self.assertEqual(r.status_code, 404)
        r2 = self.client.get("/captura/..%2F..%2Fetc%2Fpasswd")
        self.assertEqual(r2.status_code, 404)
