"""Pruebas unitarias para el subsistema de servicios STT agnóstico y adaptador Groq (U-4.2C-2).

Cubre:
1. Implementación del contrato abstracto STTProvider.
2. Validación de audio vacío o inválido.
3. Validación de nombre de archivo y extensiones permitidas.
4. Transcripción correcta y normalización de respuesta.
5. Manejo de respuestas vacías o malformadas.
6. Mapeo de errores de Rate Limit (HTTP 429).
7. Mapeo de errores de Timeout (Groq y HTTPX).
8. Mapeo de errores de autenticación y configuración.
9. Mapeo de errores genéricos del proveedor.
10. Verificación de ejecución asíncrona delegada en asyncio.to_thread.
11. Verificación de encapsulamiento total (sin exposición de objetos o excepciones crudas de Groq).
12. Semántica de cancelación de tareas asyncio.
13. Inicialización perezosa sin llamadas a red ni clientes globales en importación.
"""

import asyncio
import os
import threading
import unittest
from unittest import mock

import groq
import httpx

from server.services.stt.base import (
    EXTENSIONES_AUDIO_PERMITIDAS,
    STTProvider,
    validar_parametros_transcripcion,
)
from server.services.stt.exceptions import (
    STTConfigurationError,
    STTConnectionError,
    STTEmptyTranscriptionError,
    STTError,
    STTMalformedResponseError,
    STTProviderError,
    STTRateLimitError,
    STTTimeoutError,
    STTValidationError,
)
from server.services.stt.providers.groq import GroqSTTProvider


class MockTranscriptionResult:
    """Simula el objeto Transcription devuelto por el SDK de Groq."""

    def __init__(self, text: str) -> None:
        self.text = text


class DummySTTProvider(STTProvider):
    """Implementación dummy para probar el contrato abstracto."""

    @property
    def nombre(self) -> str:
        return "dummy"

    async def transcribir(
        self,
        audio_bytes: bytes,
        filename: str,
        language: str = "es",
        prompt: str | None = None,
        model: str | None = None,
    ) -> str:
        validar_parametros_transcripcion(audio_bytes, filename, language, prompt, model)
        return "dummy transcription"


class TestSTTSubsystem(unittest.IsolatedAsyncioTestCase):
    """Suite de pruebas para el subsistema STT y adaptador Groq."""

    def setUp(self) -> None:
        self.mock_client = mock.MagicMock()
        self.provider = GroqSTTProvider(
            client=self.mock_client,
            default_model="whisper-large-v3-turbo",
            timeout=15.0,
        )
        self.valid_audio = b"\x1a\x45\xdf\xa3dummy_valid_audio_bytes"
        self.valid_filename = "audio_test.wav"

    # -------------------------------------------------------------------------
    # 1. Implementación del contrato abstracto
    # -------------------------------------------------------------------------

    def test_01_contrato_abstracto_no_instanciable_directamente(self) -> None:
        """STTProvider no puede instanciarse directamente sin implementar sus métodos."""
        with self.assertRaises(TypeError):
            STTProvider()  # type: ignore

    async def test_01b_dummy_provider_cumple_contrato(self) -> None:
        """Una subclase que implementa STTProvider es instanciable y operable."""
        dummy = DummySTTProvider()
        self.assertIsInstance(dummy, STTProvider)
        self.assertEqual(dummy.nombre, "dummy")
        resultado = await dummy.transcribir(self.valid_audio, self.valid_filename)
        self.assertEqual(resultado, "dummy transcription")

    def test_01c_groq_stt_provider_es_instancia_de_stt_provider(self) -> None:
        """GroqSTTProvider implementa formalmente la interfaz STTProvider."""
        self.assertIsInstance(self.provider, STTProvider)
        self.assertEqual(self.provider.nombre, "groq")
        self.assertEqual(self.provider.default_model, "whisper-large-v3-turbo")

    def test_01d_jerarquia_excepciones_validation_error_y_malformed_response(self) -> None:
        """STTValidationError hereda directamente de STTError (no de STTProviderError)."""
        self.assertTrue(issubclass(STTValidationError, STTError))
        self.assertFalse(issubclass(STTValidationError, STTProviderError))
        self.assertTrue(issubclass(STTMalformedResponseError, STTProviderError))
        self.assertTrue(issubclass(STTMalformedResponseError, STTError))
        self.assertTrue(issubclass(STTEmptyTranscriptionError, STTMalformedResponseError))
        self.assertTrue(issubclass(STTEmptyTranscriptionError, STTProviderError))
        self.assertTrue(issubclass(STTEmptyTranscriptionError, STTError))

    # -------------------------------------------------------------------------
    # 2. Audio vacío o inválido
    # -------------------------------------------------------------------------

    async def test_02_audio_vacio_lanza_stt_validation_error(self) -> None:
        """El audio con 0 bytes levanta STTValidationError."""
        with self.assertRaises(STTValidationError) as ctx:
            await self.provider.transcribir(b"", self.valid_filename)
        self.assertIn("no puede estar vacío", str(ctx.exception))

    async def test_02b_audio_tipo_invalido_lanza_stt_validation_error(self) -> None:
        """Tipos de audio no binarios (None, str, int, dict) levantan STTValidationError."""
        tipos_invalidos = [None, "audio_en_string", 12345, {"data": 1}, [1, 2, 3]]
        for invalido in tipos_invalidos:
            with self.subTest(tipo=type(invalido).__name__):
                with self.assertRaises(STTValidationError) as ctx:
                    await self.provider.transcribir(invalido, self.valid_filename)  # type: ignore
                self.assertIn("debe ser bytes", str(ctx.exception))

    async def test_02c_audio_bytearray_es_aceptado(self) -> None:
        """El tipo bytearray es aceptado válidamente como entrada de audio."""
        self.mock_client.audio.transcriptions.create.return_value = MockTranscriptionResult(
            "Texto válido desde bytearray"
        )
        audio_ba = bytearray(self.valid_audio)
        resultado = await self.provider.transcribir(audio_ba, self.valid_filename)
        self.assertEqual(resultado, "Texto válido desde bytearray")

    # -------------------------------------------------------------------------
    # 3. Nombre de archivo inválido y extensiones permitidas
    # -------------------------------------------------------------------------

    async def test_03_filename_vacio_o_blanco_lanza_stt_validation_error(self) -> None:
        """Nombres de archivo vacíos, con solo espacios o no strings levantan STTValidationError."""
        nombres_invalidos = ["", "   ", "\t\n", None, 123]
        for nombre in nombres_invalidos:
            with self.subTest(nombre=nombre):
                with self.assertRaises(STTValidationError):
                    await self.provider.transcribir(self.valid_audio, nombre)  # type: ignore

    async def test_03b_filename_sin_extension_o_no_permitida(self) -> None:
        """Archivos sin extensión o con extensiones no soportadas levantan STTValidationError."""
        archivos_no_permitidos = [
            "audio",
            "audio.",
            "documento.pdf",
            "script.py",
            "binario.exe",
            "imagen.png",
            "texto.txt",
        ]
        for nombre in archivos_no_permitidos:
            with self.subTest(nombre=nombre):
                with self.assertRaises(STTValidationError) as ctx:
                    await self.provider.transcribir(self.valid_audio, nombre)
                self.assertIn("no soportada o ausente", str(ctx.exception))

    async def test_03c_filename_con_caracter_nulo_es_rechazado(self) -> None:
        """Nombres de archivo con byte nulo (null byte injection) son rechazados."""
        with self.assertRaises(STTValidationError) as ctx:
            await self.provider.transcribir(self.valid_audio, "audio\0.wav")
        self.assertIn("caracteres nulos", str(ctx.exception))

    async def test_03d_extensiones_audio_permitidas_son_validas(self) -> None:
        """Todas las extensiones de audio permitidas son aceptadas por el validador."""
        self.mock_client.audio.transcriptions.create.return_value = MockTranscriptionResult("OK")
        for ext in EXTENSIONES_AUDIO_PERMITIDAS:
            with self.subTest(ext=ext):
                res = await self.provider.transcribir(self.valid_audio, f"test_audio.{ext.upper()}")
                self.assertEqual(res, "OK")

    async def test_03e_idioma_invalido_es_rechazado(self) -> None:
        """Parámetros de idioma vacíos o no-strings son rechazados."""
        with self.assertRaises(STTValidationError):
            await self.provider.transcribir(self.valid_audio, self.valid_filename, language="")
        with self.assertRaises(STTValidationError):
            await self.provider.transcribir(self.valid_audio, self.valid_filename, language=None)  # type: ignore

    async def test_03f_validacion_parametro_model(self) -> None:
        """Validación de parámetro model: None y modelos válidos pasan; vacíos, con espacios o tipo erróneo fallan."""
        self.mock_client.audio.transcriptions.create.return_value = MockTranscriptionResult("OK")

        # 1. model=None es válido
        res_none = await self.provider.transcribir(self.valid_audio, self.valid_filename, model=None)
        self.assertEqual(res_none, "OK")

        # 2. model válido es aceptado
        res_valid = await self.provider.transcribir(self.valid_audio, self.valid_filename, model="whisper-large-v3")
        self.assertEqual(res_valid, "OK")

        # 3. Modelos inválidos levantan STTValidationError
        modelos_invalidos = ["", "   ", " whisper-large-v3", "whisper-large-v3 ", 123, ["model"]]
        for m_invalido in modelos_invalidos:
            with self.subTest(model=repr(m_invalido)):
                with self.assertRaises(STTValidationError):
                    await self.provider.transcribir(self.valid_audio, self.valid_filename, model=m_invalido)  # type: ignore

    # -------------------------------------------------------------------------
    # 4. Transcripción correcta
    # -------------------------------------------------------------------------

    async def test_04_transcripcion_correcta_objeto_sdk(self) -> None:
        """Transcripción correcta cuando el SDK devuelve un objeto Transcription."""
        self.mock_client.audio.transcriptions.create.return_value = MockTranscriptionResult(
            "  Hola Madeline, transcribe este mensaje por favor.  "
        )

        res = await self.provider.transcribir(
            audio_bytes=self.valid_audio,
            filename=self.valid_filename,
            language="es",
            prompt="Vocabulario técnico Madeline",
        )

        self.assertEqual(res, "Hola Madeline, transcribe este mensaje por favor.")
        self.mock_client.audio.transcriptions.create.assert_called_once_with(
            file=(self.valid_filename, self.valid_audio),
            model="whisper-large-v3-turbo",
            language="es",
            prompt="Vocabulario técnico Madeline",
            timeout=15.0,
        )

    async def test_04b_transcripcion_correcta_desde_dict(self) -> None:
        """Transcripción correcta cuando el SDK devuelve un diccionario serializado."""
        self.mock_client.audio.transcriptions.create.return_value = {
            "text": "Transcripción devuelta como diccionario."
        }

        res = await self.provider.transcribir(self.valid_audio, self.valid_filename)
        self.assertEqual(res, "Transcripción devuelta como diccionario.")

    async def test_04c_transcripcion_correcta_desde_string(self) -> None:
        """Transcripción correcta cuando el SDK devuelve directamente un string."""
        self.mock_client.audio.transcriptions.create.return_value = "Transcripción directa en string."
        res = await self.provider.transcribir(self.valid_audio, self.valid_filename)
        self.assertEqual(res, "Transcripción directa en string.")

    async def test_04d_modelo_especifico_sobrescribe_default(self) -> None:
        """El parámetro model sobrescribe el modelo por defecto en la llamada al SDK."""
        self.mock_client.audio.transcriptions.create.return_value = MockTranscriptionResult("Modelo custom")
        await self.provider.transcribir(self.valid_audio, self.valid_filename, model="whisper-custom-v2")
        _, kwargs = self.mock_client.audio.transcriptions.create.call_args
        self.assertEqual(kwargs["model"], "whisper-custom-v2")

    # -------------------------------------------------------------------------
    # 5. Respuesta vacía o malformada
    # -------------------------------------------------------------------------

    async def test_05_respuesta_nula_lanza_stt_malformed_response_error(self) -> None:
        """Si el SDK retorna None, se levanta STTMalformedResponseError."""
        self.mock_client.audio.transcriptions.create.return_value = None
        with self.assertRaises(STTMalformedResponseError) as ctx:
            await self.provider.transcribir(self.valid_audio, self.valid_filename)
        self.assertIn("respuesta nula", str(ctx.exception))
        self.assertEqual(ctx.exception.provider, "groq")

    async def test_05b_respuesta_sin_campo_text_lanza_stt_malformed_response_error(self) -> None:
        """Si el objeto o diccionario de respuesta carece de campo 'text', levanta error."""
        respuestas_malformadas = [
            {},
            {"otro_campo": "valor"},
            mock.MagicMock(spec=[]),  # Objeto sin atributo text
            12345,
            ["texto"],
        ]
        for malformada in respuestas_malformadas:
            with self.subTest(tipo=type(malformada).__name__):
                self.mock_client.audio.transcriptions.create.return_value = malformada
                with self.assertRaises(STTMalformedResponseError):
                    await self.provider.transcribir(self.valid_audio, self.valid_filename)

    async def test_05c_respuesta_con_texto_vacio_lanza_stt_malformed_response_error(self) -> None:
        """Si el campo text contiene solo espacios o está vacío, levanta STTEmptyTranscriptionError."""
        textos_vacios = ["", "   ", "\n\t  "]
        for txt in textos_vacios:
            with self.subTest(texto=repr(txt)):
                self.mock_client.audio.transcriptions.create.return_value = MockTranscriptionResult(txt)
                with self.assertRaises(STTMalformedResponseError) as ctx:
                    await self.provider.transcribir(self.valid_audio, self.valid_filename)
                self.assertIsInstance(ctx.exception, STTEmptyTranscriptionError)
                self.assertIn("vacía", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 6. Rate Limit
    # -------------------------------------------------------------------------

    async def test_06_mapeo_groq_rate_limit_error(self) -> None:
        """groq.RateLimitError se mapea a STTRateLimitError preservando causa."""
        mock_resp = mock.MagicMock(status_code=429)
        groq_exc = groq.RateLimitError(
            message="Rate limit reached: tokens per minute (TPM) exceeded.",
            response=mock_resp,
            body={"error": {"message": "TPM limit"}},
        )
        self.mock_client.audio.transcriptions.create.side_effect = groq_exc

        with self.assertRaises(STTRateLimitError) as ctx:
            await self.provider.transcribir(self.valid_audio, self.valid_filename)

        self.assertEqual(ctx.exception.provider, "groq")
        self.assertIn("Rate limit reached", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, groq_exc)

    # -------------------------------------------------------------------------
    # 7. Timeout
    # -------------------------------------------------------------------------

    async def test_07_mapeo_groq_api_timeout_error(self) -> None:
        """groq.APITimeoutError se mapea a STTTimeoutError."""
        mock_req = mock.MagicMock()
        groq_exc = groq.APITimeoutError(request=mock_req)
        self.mock_client.audio.transcriptions.create.side_effect = groq_exc

        with self.assertRaises(STTTimeoutError) as ctx:
            await self.provider.transcribir(self.valid_audio, self.valid_filename)

        self.assertEqual(ctx.exception.provider, "groq")
        self.assertIs(ctx.exception.__cause__, groq_exc)

    async def test_07b_mapeo_httpx_timeout_exception(self) -> None:
        """httpx.TimeoutException se mapea a STTTimeoutError."""
        httpx_exc = httpx.ReadTimeout("Read timed out after 15.0s")
        self.mock_client.audio.transcriptions.create.side_effect = httpx_exc

        with self.assertRaises(STTTimeoutError) as ctx:
            await self.provider.transcribir(self.valid_audio, self.valid_filename)

        self.assertEqual(ctx.exception.provider, "groq")
        self.assertIs(ctx.exception.__cause__, httpx_exc)

    async def test_07c_timeout_efectivo_con_cliente_inyectado(self) -> None:
        """El timeout configurado en GroqSTTProvider se envía explícitamente al SDK con cliente inyectado."""
        self.mock_client.audio.transcriptions.create.return_value = MockTranscriptionResult("OK")
        prov_custom_timeout = GroqSTTProvider(client=self.mock_client, timeout=42.5)

        await prov_custom_timeout.transcribir(self.valid_audio, self.valid_filename)

        _, kwargs = self.mock_client.audio.transcriptions.create.call_args
        self.assertEqual(kwargs.get("timeout"), 42.5)

    async def test_07d_timeout_efectivo_con_cliente_creado_internamente(self) -> None:
        """El timeout configurado se propaga tanto al constructor de Groq como a create() con cliente interno."""
        mock_internal_client = mock.MagicMock()
        mock_internal_client.audio.transcriptions.create.return_value = MockTranscriptionResult("OK")

        with mock.patch("groq.Groq", return_value=mock_internal_client) as mock_groq_ctor:
            prov_interno = GroqSTTProvider(client=None, api_key="gsk_dummy_timeout_test", timeout=33.0)
            await prov_interno.transcribir(self.valid_audio, self.valid_filename)

            # Verificación en el constructor
            mock_groq_ctor.assert_called_once()
            _, ctor_kwargs = mock_groq_ctor.call_args
            self.assertEqual(ctor_kwargs.get("timeout"), 33.0)

            # Verificación en el método create
            mock_internal_client.audio.transcriptions.create.assert_called_once()
            _, create_kwargs = mock_internal_client.audio.transcriptions.create.call_args
            self.assertEqual(create_kwargs.get("timeout"), 33.0)

    # -------------------------------------------------------------------------
    # 8. Error de autenticación y configuración
    # -------------------------------------------------------------------------

    async def test_08_mapeo_groq_authentication_error(self) -> None:
        """groq.AuthenticationError se mapea a STTConfigurationError."""
        mock_resp = mock.MagicMock(status_code=401)
        groq_exc = groq.AuthenticationError(
            message="Invalid API Key provided.",
            response=mock_resp,
            body={"error": {"message": "Invalid API Key"}},
        )
        self.mock_client.audio.transcriptions.create.side_effect = groq_exc

        with self.assertRaises(STTConfigurationError) as ctx:
            await self.provider.transcribir(self.valid_audio, self.valid_filename)

        self.assertEqual(ctx.exception.provider, "groq")
        self.assertIn("Error de autenticación", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, groq_exc)

    async def test_08b_falta_de_api_key_sin_cliente_lanza_stt_configuration_error(self) -> None:
        """Si no hay cliente inyectado ni GROQ_API_KEY en entorno, lanza STTConfigurationError."""
        provider_sin_key = GroqSTTProvider(client=None, api_key=None)
        with mock.patch.dict(os.environ, {}, clear=True):
            # Asegurar que GROQ_API_KEY no exista
            os.environ.pop("GROQ_API_KEY", None)
            with self.assertRaises(STTConfigurationError) as ctx:
                await provider_sin_key.transcribir(self.valid_audio, self.valid_filename)
            self.assertIn("GROQ_API_KEY no está configurada", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 9. Error genérico del proveedor y conexión
    # -------------------------------------------------------------------------

    async def test_09_mapeo_groq_bad_request_error(self) -> None:
        """groq.BadRequestError (HTTP 400) se mapea a STTProviderError."""
        mock_resp = mock.MagicMock(status_code=400)
        groq_exc = groq.BadRequestError(
            message="Audio format corrupt",
            response=mock_resp,
            body={"error": {"message": "Corrupt audio"}},
        )
        self.mock_client.audio.transcriptions.create.side_effect = groq_exc

        with self.assertRaises(STTProviderError) as ctx:
            await self.provider.transcribir(self.valid_audio, self.valid_filename)

        self.assertEqual(ctx.exception.provider, "groq")
        self.assertIs(ctx.exception.__cause__, groq_exc)

    async def test_09b_mapeo_groq_api_connection_error(self) -> None:
        """groq.APIConnectionError se mapea a STTConnectionError."""
        mock_req = mock.MagicMock()
        groq_exc = groq.APIConnectionError(request=mock_req)
        self.mock_client.audio.transcriptions.create.side_effect = groq_exc

        with self.assertRaises(STTConnectionError) as ctx:
            await self.provider.transcribir(self.valid_audio, self.valid_filename)

        self.assertEqual(ctx.exception.provider, "groq")
        self.assertIs(ctx.exception.__cause__, groq_exc)

    async def test_09c_excepcion_inesperada_se_mapea_a_stt_provider_error(self) -> None:
        """Cualquier excepción inesperada no contemplada se mapea a STTProviderError."""
        exc_rara = RuntimeError("Fallo interno en biblioteca nativa de red")
        self.mock_client.audio.transcriptions.create.side_effect = exc_rara

        with self.assertRaises(STTProviderError) as ctx:
            await self.provider.transcribir(self.valid_audio, self.valid_filename)

        self.assertEqual(ctx.exception.provider, "groq")
        self.assertIn("Fallo interno", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, exc_rara)

    # -------------------------------------------------------------------------
    # 10. Uso de asyncio.to_thread y concurrencia no bloqueante
    # -------------------------------------------------------------------------

    async def test_10_ejecucion_en_hilo_worker_con_to_thread(self) -> None:
        """Verifica que la llamada al SDK corre en un hilo worker diferente al loop principal."""
        id_hilo_principal = threading.get_ident()
        id_hilo_worker = [None]

        def _capturar_hilo_y_retornar(**kwargs):
            id_hilo_worker[0] = threading.get_ident()
            return MockTranscriptionResult("Transcrito en worker thread")

        self.mock_client.audio.transcriptions.create.side_effect = _capturar_hilo_y_retornar

        with mock.patch("asyncio.to_thread", wraps=asyncio.to_thread) as spy_to_thread:
            res = await self.provider.transcribir(self.valid_audio, self.valid_filename)

            self.assertEqual(res, "Transcrito en worker thread")
            spy_to_thread.assert_called_once()
            self.assertIsNotNone(id_hilo_worker[0])
            self.assertNotEqual(
                id_hilo_principal,
                id_hilo_worker[0],
                "La llamada al SDK síncrono debe ejecutarse fuera del hilo del event loop.",
            )

    # -------------------------------------------------------------------------
    # 11. No se exponen excepciones u objetos crudos de Groq
    # -------------------------------------------------------------------------

    async def test_11_ninguna_excepcion_cruda_de_groq_escapa(self) -> None:
        """Todas las excepciones levantadas son instancias de STTError y no de GroqError directamente."""
        mock_resp = mock.MagicMock(status_code=500)
        casos = [
            groq.RateLimitError("rate limit", response=mock_resp, body={}),
            groq.APITimeoutError(request=mock.MagicMock()),
            groq.APIConnectionError(request=mock.MagicMock()),
            groq.AuthenticationError("auth error", response=mock_resp, body={}),
            groq.BadRequestError("bad request", response=mock_resp, body={}),
            groq.InternalServerError("500 error", response=mock_resp, body={}),
        ]

        for exc_groq in casos:
            with self.subTest(exc=type(exc_groq).__name__):
                self.mock_client.audio.transcriptions.create.side_effect = exc_groq
                try:
                    await self.provider.transcribir(self.valid_audio, self.valid_filename)
                    self.fail(f"Debió levantarse excepción para {type(exc_groq).__name__}")
                except Exception as exc_atrapada:
                    self.assertIsInstance(
                        exc_atrapada,
                        STTError,
                        f"La excepción {type(exc_atrapada).__name__} debe heredar de STTError.",
                    )
                    # Verifica que no sea la clase cruda del SDK de Groq sin envolver
                    self.assertFalse(
                        type(exc_atrapada).__module__.startswith("groq"),
                        f"Se expuso una excepción cruda del módulo groq: {type(exc_atrapada)}",
                    )

    async def test_11b_retorno_exitoso_es_exclusivamente_str_plano(self) -> None:
        """La transcripción exitosa retorna siempre str puro y no el objeto crudo del SDK."""
        objeto_sdk_crudo = MockTranscriptionResult("Mensaje de voz limpio")
        self.mock_client.audio.transcriptions.create.return_value = objeto_sdk_crudo

        resultado = await self.provider.transcribir(self.valid_audio, self.valid_filename)

        self.assertIsInstance(resultado, str)
        self.assertEqual(resultado, "Mensaje de voz limpio")
        self.assertNotIsInstance(resultado, MockTranscriptionResult)

    # -------------------------------------------------------------------------
    # 12. Semántica de cancelación de tareas asyncio
    # -------------------------------------------------------------------------

    async def test_12_cancelacion_de_tarea_asyncio(self) -> None:
        """Cancelar la tarea de transcripción en asyncio levanta CancelledError limpiamente."""
        bloqueo_worker = threading.Event()

        def _bloquear_hasta_cancelar(**kwargs):
            bloqueo_worker.wait(timeout=2.0)
            return MockTranscriptionResult("tardío")

        self.mock_client.audio.transcriptions.create.side_effect = _bloquear_hasta_cancelar

        task = asyncio.create_task(self.provider.transcribir(self.valid_audio, self.valid_filename))
        # Permitir que la tarea inicie el dispatch a to_thread
        await asyncio.sleep(0.01)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        # Desbloquear el hilo worker para que no quede pendiente
        bloqueo_worker.set()

    # -------------------------------------------------------------------------
    # 13. Inicialización perezosa e imports libres de efectos secundarios
    # -------------------------------------------------------------------------

    def test_13_inicializacion_perezosa_sin_conexion_al_instanciar(self) -> None:
        """Instanciar GroqSTTProvider no realiza llamadas de red ni inicializa cliente prematuramente."""
        with mock.patch("groq.Groq") as mock_groq_class:
            prov = GroqSTTProvider(api_key="gsk_dummy_123")
            mock_groq_class.assert_not_called()
            self.assertIsNone(prov._client)


if __name__ == "__main__":
    unittest.main()
