"""Pruebas unitarias y de integración para el router de audio de Madeline (U-4.2C-3).

Cubre:
1. Transcripción exitosa y continuidad hacia procesar_mensaje.
2. Formato y compatibilidad de respuestas ChatResponse.
3. Validación de audio ausente, vacío o extensión no soportada.
4. Mapeo de STTRateLimitError hacia responder_sin_cuota().
5. Mapeo de STTTimeoutError hacia HTTP 504 Gateway Timeout sin éxito falso.
6. Mapeo de STTConfigurationError y STTConnectionError.
7. Mapeo de STTProviderError.
8. Descarte de alucinaciones conocidas de Whisper (ALUCINACIONES_WHISPER).
9. Manejo de audio sin voz detectada (HTTP 400).
10. Comportamiento según origen ('pc' interrumpe voz; 'telefono' no interrumpe).
11. Validación del parámetro origen (rechazo de orígenes inválidos).
12. Inyección desacoplada del proveedor STT mediante fijar_stt_provider / get_stt_provider.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import HTTPException
from fastapi.testclient import TestClient

import madeline_server as ms
from core.models import ChatResponse
from routers.audio import get_stt_provider
from services.stt import (
    STTConfigurationError,
    STTConnectionError,
    STTEmptyTranscriptionError,
    STTError,
    STTMalformedResponseError,
    STTProvider,
    STTProviderError,
    STTRateLimitError,
    STTTimeoutError,
    STTValidationError,
)


class MockSTTProvider(STTProvider):
    """Proveedor STT mock para pruebas del router de audio."""

    def __init__(self, respuesta_o_error="Hola Madeline"):
        self.respuesta_o_error = respuesta_o_error
        self.llamadas = []

    @property
    def nombre(self) -> str:
        return "mock_stt"

    async def transcribir(
        self,
        audio_bytes: bytes,
        filename: str,
        language: str = "es",
        prompt: str | None = None,
        model: str | None = None,
    ) -> str:
        self.llamadas.append({
            "audio_bytes": audio_bytes,
            "filename": filename,
            "language": language,
            "prompt": prompt,
            "model": model,
        })
        if isinstance(self.respuesta_o_error, Exception):
            raise self.respuesta_o_error
        return self.respuesta_o_error


class TestRouterAudio(unittest.IsolatedAsyncioTestCase):
    """Pruebas del endpoint /comando_audio con inyección del proveedor STT."""

    def setUp(self):
        # Desactivar requerimiento de token para llamadas locales de TestClient
        self.patch_token = mock.patch.object(ms, "MADELINE_TOKEN", None)
        self.patch_token.start()
        self.addCleanup(self.patch_token.stop)

        # Interrumpir voz mock
        self.patch_interrumpir = mock.patch.object(ms, "interrumpir_voz_pc")
        self.mock_interrumpir = self.patch_interrumpir.start()
        self.addCleanup(self.patch_interrumpir.stop)

        # Guardar proveedor original para restaurar al finalizar
        self.stt_original = ms.obtener_stt_provider()
        self.addCleanup(lambda: ms.fijar_stt_provider(self.stt_original))

        # Guardar overrides originales de FastAPI para restaurar sin eliminar overrides ajenos
        overrides_originales = dict(ms.app.dependency_overrides)

        def _restaurar_overrides():
            ms.app.dependency_overrides.clear()
            ms.app.dependency_overrides.update(overrides_originales)

        self.addCleanup(_restaurar_overrides)

        self.client = TestClient(ms.app, client=("127.0.0.1", 50000))
        self.dummy_audio = b"\x1a\x45\xdf\xa3dummy_pcm_bytes"

    # -------------------------------------------------------------------------
    # 1. Transcripción exitosa y continuidad hacia procesar_mensaje
    # -------------------------------------------------------------------------

    async def test_01_comando_audio_exitoso_origen_pc(self):
        """Transcribe audio exitosamente, llama a procesar_mensaje y retorna ChatResponse con transcripcion."""
        prov = MockSTTProvider("reproduce musica en spotify")
        ms.fijar_stt_provider(prov)

        with mock.patch.object(
            ms,
            "procesar_mensaje",
            mock.AsyncMock(return_value=ChatResponse(response="Reproduciendo música.")),
        ) as mock_proc:
            r = self.client.post(
                "/comando_audio?origen=pc",
                files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
            )

        self.assertEqual(r.status_code, 200)
        datos = r.json()
        self.assertEqual(datos["response"], "Reproduciendo música.")
        self.assertEqual(datos["transcripcion"], "reproduce musica en spotify")

        # Verifica llamada al proveedor
        self.assertEqual(len(prov.llamadas), 1)
        self.assertEqual(prov.llamadas[0]["filename"], "orden.wav")
        self.assertEqual(prov.llamadas[0]["language"], "es")
        self.assertEqual(prov.llamadas[0]["prompt"], ms.PISTA_VOCABULARIO)

        # Verifica que se interrumpió la voz previa en PC
        self.mock_interrumpir.assert_called_once()

        # Verifica continuidad hacia procesar_mensaje
        mock_proc.assert_called_once()
        req_arg = mock_proc.call_args[0][0]
        self.assertEqual(req_arg.message, "reproduce musica en spotify")
        self.assertEqual(mock_proc.call_args[1].get("origen"), "pc")

    async def test_01b_comando_audio_exitoso_origen_telefono(self):
        """Origen teléfono no interrumpe la voz del PC y procesa con origen='telefono'."""
        prov = MockSTTProvider("cuál es el clima")
        ms.fijar_stt_provider(prov)

        with mock.patch.object(
            ms,
            "procesar_mensaje",
            mock.AsyncMock(return_value=ChatResponse(response="Hace 22 grados.")),
        ) as mock_proc:
            r = self.client.post(
                "/comando_audio?origen=telefono",
                files={"audio": ("celular.m4a", self.dummy_audio, "audio/mp4")},
            )

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["transcripcion"], "cuál es el clima")
        self.mock_interrumpir.assert_not_called()
        self.assertEqual(mock_proc.call_args[1].get("origen"), "telefono")

    # -------------------------------------------------------------------------
    # 2. Validación de entradas
    # -------------------------------------------------------------------------

    async def test_02_audio_ausente_retorna_422(self):
        """Petición sin campo 'audio' es rechazada con HTTP 422 por FastAPI."""
        r = self.client.post("/comando_audio")
        self.assertEqual(r.status_code, 422)

    async def test_02b_origen_invalido_retorna_400(self):
        """Origen distinto de 'pc' o 'telefono' retorna HTTP 400."""
        r = self.client.post(
            "/comando_audio?origen=tablet",
            files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("El origen debe ser 'pc' o 'telefono'", r.json()["detail"])

    async def test_02c_audio_vacio_retorna_400(self):
        """Audio vacío (0 bytes) levanta STTValidationError y retorna HTTP 400."""
        prov = MockSTTProvider(STTValidationError("El contenido de audio no puede estar vacío (0 bytes)."))
        ms.fijar_stt_provider(prov)

        r = self.client.post(
            "/comando_audio?origen=pc",
            files={"audio": ("orden.wav", b"", "audio/wav")},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("Audio inválido", r.json()["detail"])

    # -------------------------------------------------------------------------
    # 3. Mapeo de Rate Limit
    # -------------------------------------------------------------------------

    async def test_03_rate_limit_delega_en_responder_sin_cuota(self):
        """STTRateLimitError delega en responder_sin_cuota() preservando contrato."""
        prov = MockSTTProvider(STTRateLimitError("TPM exceeded", provider="groq"))
        ms.fijar_stt_provider(prov)

        r = self.client.post(
            "/comando_audio?origen=pc",
            files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
        )

        self.assertEqual(r.status_code, 200)
        datos = r.json()
        self.assertIn("me quedé sin cuota de Groq por ahora", datos["response"])

    # -------------------------------------------------------------------------
    # 4. Mapeo de Timeout
    # -------------------------------------------------------------------------

    async def test_04_timeout_retorna_504_sin_exito_falso(self):
        """STTTimeoutError retorna HTTP 504 sin generar un ChatResponse de falso éxito."""
        prov = MockSTTProvider(STTTimeoutError("Read timed out", provider="groq"))
        ms.fijar_stt_provider(prov)

        r = self.client.post(
            "/comando_audio?origen=pc",
            files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
        )

        self.assertEqual(r.status_code, 504)
        self.assertIn("tiempo de espera agotado", r.json()["detail"])

    # -------------------------------------------------------------------------
    # 5. Mapeo de errores de configuración y conexión
    # -------------------------------------------------------------------------

    async def test_05_error_configuracion_retorna_500(self):
        """STTConfigurationError retorna HTTP 500 con detalle descriptivo."""
        prov = MockSTTProvider(STTConfigurationError("Falta GROQ_API_KEY", provider="groq"))
        ms.fijar_stt_provider(prov)

        r = self.client.post(
            "/comando_audio?origen=pc",
            files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
        )

        self.assertEqual(r.status_code, 500)
        self.assertIn("Error de configuración", r.json()["detail"])

    async def test_05b_error_conexion_retorna_502(self):
        """STTConnectionError retorna HTTP 502 Bad Gateway."""
        prov = MockSTTProvider(STTConnectionError("DNS lookup failed", provider="groq"))
        ms.fijar_stt_provider(prov)

        r = self.client.post(
            "/comando_audio?origen=pc",
            files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
        )

        self.assertEqual(r.status_code, 502)
        self.assertIn("Error de conexión", r.json()["detail"])

    # -------------------------------------------------------------------------
    # 6. Mapeo de errores del proveedor
    # -------------------------------------------------------------------------

    async def test_06_error_proveedor_retorna_500(self):
        """STTProviderError retorna HTTP 500."""
        prov = MockSTTProvider(STTProviderError("Error 500 interno de Groq", provider="groq"))
        ms.fijar_stt_provider(prov)

        r = self.client.post(
            "/comando_audio?origen=pc",
            files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
        )

        self.assertEqual(r.status_code, 500)
        self.assertIn("Error procesando el audio", r.json()["detail"])

    # -------------------------------------------------------------------------
    # 7. Audio sin voz y descarte de alucinaciones
    # -------------------------------------------------------------------------

    async def test_07_transcripcion_vacia_retorna_400_no_se_detecto_voz(self):
        """Transcripción vacía lanza STTEmptyTranscriptionError y retorna HTTP 400 'No se detectó voz.'."""
        prov = MockSTTProvider(STTEmptyTranscriptionError("La transcripción retornada por el proveedor está vacía.", provider="groq"))
        ms.fijar_stt_provider(prov)

        r = self.client.post(
            "/comando_audio?origen=pc",
            files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
        )

        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "No se detectó voz.")

    async def test_07b_alucinacion_whisper_es_descartada_y_retorna_400(self):
        """Alucinaciones conocidas de Whisper son filtradas retornando HTTP 400 'No se detectó voz.'."""
        alucinaciones = [
            "Subtítulos por la comunidad de Amara.org",
            "¡Gracias por ver el video!",
            "Suscríbete al canal y dale like",
        ]
        for alu in alucinaciones:
            with self.subTest(alucinacion=alu):
                prov = MockSTTProvider(alu)
                ms.fijar_stt_provider(prov)

                r = self.client.post(
                    "/comando_audio?origen=pc",
                    files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
                )

                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["detail"], "No se detectó voz.")

    async def test_07c_respuesta_malformada_real_retorna_500(self):
        """Respuesta estructuralmente malformada del proveedor retorna HTTP 500."""
        prov = MockSTTProvider(STTMalformedResponseError("Tipo de respuesta no reconocido (int).", provider="groq"))
        ms.fijar_stt_provider(prov)

        r = self.client.post(
            "/comando_audio?origen=pc",
            files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
        )

        self.assertEqual(r.status_code, 500)
        self.assertIn("Error procesando el audio: Tipo de respuesta no reconocido", r.json()["detail"])

    async def test_07d_transcripcion_espacios_en_blanco_retorna_400_y_no_procesa_mensaje(self):
        """Transcripción compuesta únicamente por espacios en blanco no llega a procesar_mensaje y retorna HTTP 400."""
        prov = MockSTTProvider("   \n\t   ")
        ms.fijar_stt_provider(prov)

        with mock.patch.object(ms, "procesar_mensaje", mock.AsyncMock()) as mock_proc:
            r = self.client.post(
                "/comando_audio?origen=pc",
                files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
            )

        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "No se detectó voz.")
        mock_proc.assert_not_called()

    async def test_07e_http_exception_en_bloque_stt_se_propaga_sin_convertirse_en_500(self):
        """HTTPException levantada dentro del bloque STT se propaga conservando su status code."""
        prov = MockSTTProvider(HTTPException(status_code=403, detail="Acceso denegado al servicio STT"))
        ms.fijar_stt_provider(prov)

        r = self.client.post(
            "/comando_audio?origen=pc",
            files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
        )

        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "Acceso denegado al servicio STT")

    # -------------------------------------------------------------------------
    # 8. Inyección de dependencias FastAPI (app.dependency_overrides)
    # -------------------------------------------------------------------------

    async def test_08_inyeccion_dependencias_fastapi_dependency_overrides(self):
        """app.dependency_overrides[get_stt_provider] reemplaza al proveedor global y se aísla correctamente."""
        prov_global = MockSTTProvider("comando global no deseado")
        prov_override = MockSTTProvider("comando inyectado por override")

        # Fijamos un proveedor global que NO debe ser llamado
        ms.fijar_stt_provider(prov_global)

        # Inyectamos el override en FastAPI
        ms.app.dependency_overrides[get_stt_provider] = lambda: prov_override

        with mock.patch.object(
            ms,
            "procesar_mensaje",
            mock.AsyncMock(return_value=ChatResponse(response="OK desde override")),
        ) as mock_proc:
            r = self.client.post(
                "/comando_audio?origen=pc",
                files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
            )

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["transcripcion"], "comando inyectado por override")
        self.assertEqual(len(prov_override.llamadas), 1)
        self.assertEqual(len(prov_global.llamadas), 0)

        # Retiramos el override específico y comprobamos que vuelve a tomar el global
        ms.app.dependency_overrides.pop(get_stt_provider, None)

        with mock.patch.object(
            ms,
            "procesar_mensaje",
            mock.AsyncMock(return_value=ChatResponse(response="OK desde global")),
        ):
            r2 = self.client.post(
                "/comando_audio?origen=pc",
                files={"audio": ("orden.wav", self.dummy_audio, "audio/wav")},
            )

        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["transcripcion"], "comando global no deseado")
        self.assertEqual(len(prov_global.llamadas), 1)


if __name__ == "__main__":
    unittest.main()
