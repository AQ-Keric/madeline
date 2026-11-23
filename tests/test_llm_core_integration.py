"""Pruebas agnósticas de integración del proveedor LLM con el núcleo conversacional (U-4.2B).

Verifica:
1. Solicitud de texto sin herramientas.
2. Solicitud con herramientas y tool_choice AUTO.
3. Tool call válido con ID y nombre.
4. Múltiples tool calls con IDs diferentes y correspondencia exacta de cada llamada y resultado.
5. Ejecución secuencial de herramientas y preservación de IDs de llamada.
6. Barrera de seguridad: preguntas explicativas/hipotéticas bloquean ejecución de herramientas.
7. Barrera de seguridad: órdenes negadas bloquean ejecución de herramientas.
8. Conservación de confirmaciones ante acciones críticas del sistema (control_energia).
9. Tool choice modos neutrales (AUTO, NONE, REQUIRED, SPECIFIC).
10. Respuesta sin contenido textual pero con tool calls válidos.
11. Rechazo de tool call sin ID (LLMMalformedResponseError).
12. Rechazo de tool call sin nombre (LLMMalformedResponseError).
13. Rechazo de tool call con argumentos malformados (LLMMalformedResponseError, sin ejecución).
14. Rechazo de contenido de respuesta malformado (no str, no None) y finish_reason inválido.
15. Errores de transporte (RateLimit -> sin_cuota, Timeout -> 504) nunca activan fallback de tools.
16. GroqProvider prioriza RateLimitError sobre mensajes con 'tool_use_failed'.
17. Flujos migrados (_procesar_mensaje, resumir_busqueda) operan sin fugas de SinCuotaGroq.
18. Política de recuperación neutral ante LLMToolCallRejectionError: ocurre exactamente una vez.
19. Ausencia de reintentos espurios ante errores generales del proveedor (HTTP 500 directo).
20. Fallo del segundo intento no provoca un tercer intento (HTTP 500 directo).
21. Fallo del segundo intento con otro LLMToolCallRejectionError no entra en bucle infinito.
22. Retrocompatibilidad de fachada llamar_groq() y SinCuotaGroq.
23. Compatibilidad inalterada de server.client para Whisper/STT.
24. Aislamiento estricto de historiales conversacionales y turnos por origen.
25. Resumen de búsqueda (resumir_busqueda) opera exclusivamente vía LLMRequest/LLMResponse neutral.
26. El flujo conversacional del núcleo opera exclusivamente vía LLMProvider sin acceder al SDK de Groq.
"""
import asyncio
import inspect
import json
import os
import sys
import time
import types
import unittest
from typing import Any, Optional
from unittest import mock

from fastapi import HTTPException
import groq
import httpx

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import entorno  # noqa: F401
import madeline_server as ms
from services.llm import (
    LLMConfigurationError,
    LLMError,
    LLMMalformedResponseError,
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMRole,
    LLMTimeoutError,
    LLMToolCall,
    LLMToolCallRejectionError,
    LLMToolChoice,
    LLMToolChoiceMode,
    LLMToolDefinition,
    LLMValidationError,
    GroqProvider,
)


class MockAgnosticLLMProvider(LLMProvider):
    """Proveedor LLM simulado que no depende de Groq ni de red externa."""

    def __init__(self, respuestas: Optional[list[Any]] = None):
        self._nombre = "mock_agnostico"
        self.solicitudes: list[LLMRequest] = []
        self._respuestas_cola: list[Any] = list(respuestas or [])
        self.vision_calls: list[dict[str, Any]] = []
        self._vision_respuestas_cola: list[Any] = []

    @property
    def nombre(self) -> str:
        return self._nombre

    def encolar_respuesta(self, resp_o_exc: Any) -> None:
        self._respuestas_cola.append(resp_o_exc)

    def encolar_respuesta_vision(self, resp_o_exc: Any) -> None:
        self._vision_respuestas_cola.append(resp_o_exc)

    async def generar_respuesta(self, solicitud: LLMRequest) -> LLMResponse:
        self.solicitudes.append(solicitud)
        if not self._respuestas_cola:
            return LLMResponse(
                texto="Respuesta mock por defecto",
                modelo="mock-model",
                proveedor=self.nombre,
            )
        siguiente = self._respuestas_cola.pop(0)
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente

    async def analizar_vision(
        self,
        imagen_bytes: bytes,
        extension: str,
        pregunta: str,
        model: Optional[str] = None,
    ) -> str:
        self.vision_calls.append({
            "imagen_bytes": imagen_bytes,
            "extension": extension,
            "pregunta": pregunta,
            "model": model,
        })
        if self._vision_respuestas_cola:
            siguiente = self._vision_respuestas_cola.pop(0)
            if isinstance(siguiente, Exception):
                raise siguiente
            return siguiente
        return "Visión mock"


class TestLLMCoreIntegration(unittest.IsolatedAsyncioTestCase):
    """Suite de integración neutral para validar el desacoplamiento del núcleo."""

    def setUp(self):
        entorno.vaciar_memoria()
        ms.historial_chat.clear()
        for k in ms.historiales_por_origen:
            ms.historiales_por_origen[k].clear()
        for clave in ms.memoria_contexto:
            ms.memoria_contexto[clave] = None

        self.original_provider = ms.obtener_llm_provider()
        self.mock_provider = MockAgnosticLLMProvider()
        ms.fijar_llm_provider(self.mock_provider)

        # Evitar reproducción de audio real
        self.patch_audio = mock.patch.object(ms, "procesar_salida_de_voz", mock.AsyncMock(return_value=None))
        self.patch_audio.start()

    def tearDown(self):
        self.patch_audio.stop()
        ms.fijar_llm_provider(self.original_provider)

    async def test_solicitud_texto_sin_herramientas(self):
        """1. Consulta sin herramientas envía LLMRequest neutral sin tools ni tool_choice."""
        self.mock_provider.encolar_respuesta(
            LLMResponse(
                texto="Hola Keric, todo en orden por aquí.",
                finish_reason="stop",
                modelo="mock-model",
                proveedor="mock_agnostico",
            )
        )

        registro = {"ruta": None, "acciones": []}
        resp = await ms._procesar_mensaje(
            ms.ChatRequest(message="Hola Madeline, ¿cómo estás hoy?"),
            origen="pc",
            registro=registro,
        )

        self.assertEqual(len(self.mock_provider.solicitudes), 1)
        solicitud = self.mock_provider.solicitudes[0]

        # Validación del contrato neutral recibido por el proveedor
        self.assertIsInstance(solicitud, LLMRequest)
        self.assertIsNone(solicitud.tools)
        self.assertIsNone(solicitud.tool_choice)
        self.assertGreater(len(solicitud.mensajes), 0)
        self.assertTrue(all(isinstance(m, LLMMessage) for m in solicitud.mensajes))
        self.assertEqual(solicitud.mensajes[-1].rol, LLMRole.USER)
        self.assertEqual(solicitud.mensajes[-1].contenido, "Hola Madeline, ¿cómo estás hoy?")

        # Verificación de respuesta procesada y commit de turno
        self.assertIn("Hola Keric", resp.response)
        self.assertEqual(registro["ruta"], "groq")
        self.assertEqual(len(ms.historiales_por_origen["pc"]), 1)
        self.assertEqual(ms.historiales_por_origen["pc"][0].usuario, "Hola Madeline, ¿cómo estás hoy?")

    async def test_solicitud_con_herramientas(self):
        """2. Consulta con intención de acción incluye LLMToolDefinition y tool_choice AUTO."""
        self.mock_provider.encolar_respuesta(
            LLMResponse(
                texto="El volumen está al 50%.",
                finish_reason="stop",
                modelo="mock-model",
                proveedor="mock_agnostico",
            )
        )

        registro = {"ruta": None, "acciones": []}
        await ms._procesar_mensaje(
            ms.ChatRequest(message="Ajusta el volumen por favor"),
            origen="pc",
            registro=registro,
        )

        self.assertEqual(len(self.mock_provider.solicitudes), 1)
        solicitud = self.mock_provider.solicitudes[0]

        self.assertIsNotNone(solicitud.tools)
        self.assertIsInstance(solicitud.tools, list)
        self.assertTrue(all(isinstance(t, LLMToolDefinition) for t in solicitud.tools))
        nombres = [t.nombre for t in solicitud.tools]
        self.assertIn("ajustar_volumen", nombres)

        self.assertIsNotNone(solicitud.tool_choice)
        self.assertIsInstance(solicitud.tool_choice, LLMToolChoice)
        self.assertEqual(solicitud.tool_choice.modo, LLMToolChoiceMode.AUTO)

    async def test_tool_call_valido_con_id_y_nombre(self):
        """3. Tool call válido con ID y nombre se ejecuta y se registra con correspondencia exacta."""
        tc = LLMToolCall(id="call_media_unic_01", nombre="control_multimedia", argumentos={"accion": "pausar"})
        self.mock_provider.encolar_respuesta(
            LLMResponse(
                texto=None,
                tool_calls=[tc],
                finish_reason="tool_calls",
                modelo="mock-model",
                proveedor="mock_agnostico",
            )
        )

        mock_media = mock.Mock(return_value="Música pausada con éxito.")
        with mock.patch.object(ms, "router_local", return_value=[]), mock.patch.dict(ms.TOOLS_FUNCIONES, {
            "control_multimedia": mock_media,
        }):
            registro = {"ruta": None, "acciones": []}
            resp = await ms._procesar_mensaje(
                ms.ChatRequest(message="Por favor ponle pausa al reproductor"),
                origen="pc",
                registro=registro,
            )

        self.assertEqual(len(registro["acciones"]), 1)
        self.assertEqual(registro["acciones"][0], ["control_multimedia", {"accion": "pausar"}])
        mock_media.assert_called_once_with(accion="pausar")
        self.assertTrue(len(resp.response) > 0)

    async def test_multiples_tool_calls_con_ids_diferentes_y_correspondencia_exacta(self):
        """4. El núcleo procesa múltiples LLMToolCall con IDs diferentes y asocia exactamente cada resultado."""
        tc1 = LLMToolCall(id="call_media_101", nombre="control_multimedia", argumentos={"accion": "pausar"})
        tc2 = LLMToolCall(id="call_vol_202", nombre="ajustar_volumen", argumentos={"accion": "mutear"})

        self.mock_provider.encolar_respuesta(
            LLMResponse(
                texto=None,
                tool_calls=[tc1, tc2],
                finish_reason="tool_calls",
                modelo="mock-model",
                proveedor="mock_agnostico",
            )
        )

        mock_media = mock.Mock(return_value="Música pausada.")
        mock_vol = mock.Mock(return_value="Audio silenciado.")
        with mock.patch.object(ms, "router_local", return_value=[]), mock.patch.dict(ms.TOOLS_FUNCIONES, {
            "control_multimedia": mock_media,
            "ajustar_volumen": mock_vol,
        }):
            registro = {"ruta": None, "acciones": []}
            resp = await ms._procesar_mensaje(
                ms.ChatRequest(message="Pausa la música y silencia el audio"),
                origen="pc",
                registro=registro,
            )

        self.assertEqual(len(registro["acciones"]), 2)
        # Correspondencia exacta entre llamada 1 y resultado 1
        self.assertEqual(registro["acciones"][0], ["control_multimedia", {"accion": "pausar"}])
        mock_media.assert_called_once_with(accion="pausar")

        # Correspondencia exacta entre llamada 2 y resultado 2
        self.assertEqual(registro["acciones"][1], ["ajustar_volumen", {"accion": "mutear"}])
        mock_vol.assert_called_once_with(accion="mutear")

        self.assertTrue(len(resp.response) > 0)

    async def test_ejecucion_secuencial_herramientas_y_preservacion_ids(self):
        """5. Múltiples tool calls preservan sus IDs originales y se ejecutan en orden secuencial estricto."""
        orden_ejecucion = []

        def mock_media(**kwargs):
            orden_ejecucion.append(("control_multimedia", kwargs))
            return "Pausado"

        def mock_vol(**kwargs):
            orden_ejecucion.append(("ajustar_volumen", kwargs))
            return "Volumen fijado"

        tc1 = LLMToolCall(id="call_seq_001", nombre="control_multimedia", argumentos={"accion": "pausar"})
        tc2 = LLMToolCall(id="call_seq_002", nombre="ajustar_volumen", argumentos={"accion": "fijar", "porcentaje": 30})

        resp_llm = LLMResponse(
            texto=None,
            tool_calls=[tc1, tc2],
            finish_reason="tool_calls",
            modelo="mock-model",
            proveedor="mock_agnostico",
        )
        self.assertEqual(resp_llm.tool_calls[0].id, "call_seq_001")
        self.assertEqual(resp_llm.tool_calls[1].id, "call_seq_002")

        self.mock_provider.encolar_respuesta(resp_llm)

        with mock.patch.object(ms, "router_local", return_value=[]), mock.patch.dict(ms.TOOLS_FUNCIONES, {
            "control_multimedia": mock_media,
            "ajustar_volumen": mock_vol,
        }):
            registro = {"ruta": None, "acciones": []}
            await ms._procesar_mensaje(
                ms.ChatRequest(message="Pausa y fija volumen al 30%"),
                origen="pc",
                registro=registro,
            )

        # Verificación de orden secuencial
        self.assertEqual(orden_ejecucion, [
            ("control_multimedia", {"accion": "pausar"}),
            ("ajustar_volumen", {"accion": "fijar", "porcentaje": 30}),
        ])
        self.assertEqual(registro["acciones"], [
            ["control_multimedia", {"accion": "pausar"}],
            ["ajustar_volumen", {"accion": "fijar", "porcentaje": 30}],
        ])

    async def test_barrera_seguridad_pregunta_explicativa_bloquea_tool_call(self):
        """6. Barrera de seguridad: una pregunta explicativa/hipotética bloquea la ejecución de herramientas."""
        mock_tool = mock.Mock(return_value="Escritorio cambiado")
        tc = LLMToolCall(id="call_sec_esc", nombre="cambiar_escritorio", argumentos={"escritorio": 3})
        self.mock_provider.encolar_respuesta(
            LLMResponse(
                texto=None,
                tool_calls=[tc],
                finish_reason="tool_calls",
                modelo="mock-model",
                proveedor="mock_agnostico",
            )
        )

        with mock.patch.object(ms, "router_local", return_value=[]), mock.patch.dict(ms.TOOLS_FUNCIONES, {
            "cambiar_escritorio": mock_tool,
        }):
            registro = {"ruta": None, "acciones": []}
            resp = await ms._procesar_mensaje(
                ms.ChatRequest(message="¿Cómo cambio al escritorio 3?"),
                origen="pc",
                registro=registro,
            )

        # La herramienta no debe haberse ejecutado
        mock_tool.assert_not_called()
        self.assertIn("pregunta conceptual o explicativa", resp.response)

    async def test_barrera_seguridad_orden_negada_bloquea_tool_call(self):
        """7. Barrera de seguridad: una orden explícitamente negada bloquea la ejecución de herramientas."""
        mock_tool = mock.Mock(return_value="App abierta")
        tc = LLMToolCall(id="call_sec_neg", nombre="abrir_aplicacion", argumentos={"app_name": "steam"})
        self.mock_provider.encolar_respuesta(
            LLMResponse(
                texto=None,
                tool_calls=[tc],
                finish_reason="tool_calls",
                modelo="mock-model",
                proveedor="mock_agnostico",
            )
        )

        with mock.patch.object(ms, "router_local", return_value=[]), mock.patch.dict(ms.TOOLS_FUNCIONES, {
            "abrir_aplicacion": mock_tool,
        }):
            registro = {"ruta": None, "acciones": []}
            resp = await ms._procesar_mensaje(
                ms.ChatRequest(message="No abras steam bajo ninguna circunstancia"),
                origen="pc",
                registro=registro,
            )

        mock_tool.assert_not_called()
        self.assertIn("orden negada", resp.response)

    async def test_conservacion_confirmaciones_accion_critica_control_energia(self):
        """8. Conservación de barrera de confirmación: apagar/reiniciar requiere confirmación del usuario."""
        mock_energia = mock.Mock(return_value="Apagando sistema...")
        tc = LLMToolCall(id="call_pwr_crit", nombre="control_energia", argumentos={"accion": "apagar"})
        self.mock_provider.encolar_respuesta(
            LLMResponse(
                texto=None,
                tool_calls=[tc],
                finish_reason="tool_calls",
                modelo="mock-model",
                proveedor="mock_agnostico",
            )
        )

        with mock.patch.object(ms, "router_local", return_value=[]), mock.patch.dict(ms.TOOLS_FUNCIONES, {
            "control_energia": mock_energia,
        }):
            registro = {"ruta": None, "acciones": []}
            resp = await ms._procesar_mensaje(
                ms.ChatRequest(message="Apaga la computadora"),
                origen="pc",
                registro=registro,
            )

        # La herramienta directa no se ejecuta sin confirmación previa
        mock_energia.assert_not_called()
        self.assertIn("confirm", resp.response.lower())
        # Se fija la confirmación pendiente en memoria de contexto
        self.assertIsNotNone(ms.memoria_contexto.obtener_confirmacion(origen="pc"))

    async def test_tool_choice_modos_neutrales(self):
        """9. Verificación de los contratos neutrales de LLMToolChoice (AUTO, NONE, REQUIRED, SPECIFIC)."""
        c_auto = LLMToolChoice.auto()
        self.assertEqual(c_auto.modo, LLMToolChoiceMode.AUTO)

        c_none = LLMToolChoice.none()
        self.assertEqual(c_none.modo, LLMToolChoiceMode.NONE)

        c_req = LLMToolChoice.required()
        self.assertEqual(c_req.modo, LLMToolChoiceMode.REQUIRED)

        tool_calc = LLMToolDefinition(nombre="calcular", descripcion="d")
        solicitud_spec = LLMRequest(
            mensajes=[LLMMessage.user("calcula")],
            tools=[tool_calc],
            tool_choice=LLMToolChoice.specific("calcular"),
        )
        self.assertEqual(solicitud_spec.tool_choice.modo, LLMToolChoiceMode.SPECIFIC)
        self.assertEqual(solicitud_spec.tool_choice.nombre_herramienta, "calcular")

    async def test_respuesta_sin_contenido_textual_con_tool_call(self):
        """10. Respuesta con texto=None pero tool_calls válidos no falla y genera respuesta de acción."""
        tc = LLMToolCall(id="call_x", nombre="control_multimedia", argumentos={"accion": "siguiente"})
        self.mock_provider.encolar_respuesta(
            LLMResponse(
                texto=None,
                tool_calls=[tc],
                finish_reason="tool_calls",
                modelo="mock-model",
            )
        )

        with mock.patch.object(ms, "router_local", return_value=[]), mock.patch.dict(ms.TOOLS_FUNCIONES, {
            "control_multimedia": mock.Mock(return_value="Pista siguiente."),
        }):
            registro = {"ruta": None, "acciones": []}
            resp = await ms._procesar_mensaje(
                ms.ChatRequest(message="Siguiente pista"),
                origen="pc",
                registro=registro,
            )

        self.assertTrue(len(resp.response) > 0)
        self.assertNotIn("None", resp.response)

    async def test_groq_provider_rechaza_tool_call_sin_id(self):
        """11. GroqProvider lanza LLMMalformedResponseError ante tool call sin ID (no inventa call_legacy)."""
        provider = GroqProvider(api_key="gsk_test")
        tc_sin_id = types.SimpleNamespace(
            id=None,
            function=types.SimpleNamespace(name="abrir_app", arguments='{"app": "calc"}'),
        )
        fake_response = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=None, tool_calls=[tc_sin_id]),
                    finish_reason="tool_calls",
                )
            ],
            model="openai/gpt-oss-120b",
        )

        with self.assertRaises(LLMMalformedResponseError) as ctx:
            provider._procesar_respuesta(fake_response)
        self.assertIn("sin identificador ('id') válido", str(ctx.exception))

    async def test_groq_provider_rechaza_tool_call_sin_nombre(self):
        """12. GroqProvider lanza LLMMalformedResponseError ante tool call sin nombre de función (no inventa tool_mock)."""
        provider = GroqProvider(api_key="gsk_test")
        tc_sin_nombre = types.SimpleNamespace(
            id="call_123",
            function=types.SimpleNamespace(name="", arguments='{"app": "calc"}'),
        )
        fake_response = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=None, tool_calls=[tc_sin_nombre]),
                    finish_reason="tool_calls",
                )
            ],
            model="openai/gpt-oss-120b",
        )

        with self.assertRaises(LLMMalformedResponseError) as ctx:
            provider._procesar_respuesta(fake_response)
        self.assertIn("sin nombre de herramienta válido", str(ctx.exception))

    async def test_groq_provider_rechaza_tool_call_con_argumentos_malformados(self):
        """13. GroqProvider lanza LLMMalformedResponseError si los argumentos no son JSON válido y no ejecuta nada."""
        provider = GroqProvider(api_key="gsk_test")
        tc_corrupto = types.SimpleNamespace(
            id="call_123",
            function=types.SimpleNamespace(name="abrir_app", arguments='{"json_incompleto": '),
        )
        fake_response = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=None, tool_calls=[tc_corrupto]),
                    finish_reason="tool_calls",
                )
            ],
            model="openai/gpt-oss-120b",
        )

        with self.assertRaises(LLMMalformedResponseError) as ctx:
            provider._procesar_respuesta(fake_response)
        self.assertIn("Argumentos malformados", str(ctx.exception))

    async def test_groq_provider_rechaza_contenido_malformado_y_finish_reason_invalido(self):
        """14. GroqProvider lanza LLMMalformedResponseError ante content no-str y no-None, o finish_reason inválido."""
        provider = GroqProvider(api_key="gsk_test")

        # 1. Content con tipo int
        fake_resp_int = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=12345, tool_calls=None),
                    finish_reason="stop",
                )
            ],
            model="openai/gpt-oss-120b",
        )
        with self.assertRaises(LLMMalformedResponseError) as ctx:
            provider._procesar_respuesta(fake_resp_int)
        self.assertIn("debe ser str o None, recibido: int", str(ctx.exception))

        # 2. Content None sin tool calls
        fake_resp_none_empty = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=None, tool_calls=None),
                    finish_reason="stop",
                )
            ],
            model="openai/gpt-oss-120b",
        )
        with self.assertRaises(LLMMalformedResponseError) as ctx:
            provider._procesar_respuesta(fake_resp_none_empty)
        self.assertIn("no contiene contenido textual", str(ctx.exception))

        # 3. finish_reason tipo int
        fake_resp_fr_int = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content="Hola", tool_calls=None),
                    finish_reason=123,
                )
            ],
            model="openai/gpt-oss-120b",
        )
        with self.assertRaises(LLMMalformedResponseError) as ctx:
            provider._procesar_respuesta(fake_resp_fr_int)
        self.assertIn("finish_reason", str(ctx.exception))

        # 4. finish_reason con espacios
        fake_resp_fr_spaces = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content="Hola", tool_calls=None),
                    finish_reason=" stop ",
                )
            ],
            model="openai/gpt-oss-120b",
        )
        with self.assertRaises(LLMMalformedResponseError) as ctx:
            provider._procesar_respuesta(fake_resp_fr_spaces)
        self.assertIn("no debe contener espacios", str(ctx.exception))

    async def test_errores_transporte_rate_limit_y_timeout_nunca_activan_fallback(self):
        """15. Errores de transporte (RateLimit -> sin_cuota, Timeout -> 504) se traducen canónicamente y nunca activan fallback."""
        # 1. Rate limit
        self.mock_provider.encolar_respuesta(LLMRateLimitError("Cuota agotada en proveedor"))
        registro = {"ruta": None, "acciones": []}
        resp = await ms._procesar_mensaje(
            ms.ChatRequest(message="Pregunta con rate limit"),
            origen="pc",
            registro=registro,
        )
        self.assertEqual(registro["ruta"], "sin_cuota")
        self.assertIn("cuota", resp.response.lower())
        self.assertEqual(len(ms.historiales_por_origen["pc"]), 0)
        # Verificación crítica: exactamente 1 llamada, sin reintento de fallback
        self.assertEqual(len(self.mock_provider.solicitudes), 1)

        # 2. Timeout
        self.mock_provider.encolar_respuesta(LLMTimeoutError("Timeout de conexión al modelo"))
        registro_timeout = {"ruta": None, "acciones": []}
        with self.assertRaises(HTTPException) as ctx:
            await ms._procesar_mensaje(
                ms.ChatRequest(message="Pregunta que da timeout"),
                origen="pc",
                registro=registro_timeout,
            )
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertIn("Tiempo de espera agotado", ctx.exception.detail)
        self.assertEqual(len(ms.historiales_por_origen["pc"]), 0)
        # Verificación crítica: exactamente 2 llamadas totales (1 de rate limit + 1 de timeout)
        self.assertEqual(len(self.mock_provider.solicitudes), 2)

    async def test_groq_provider_prioriza_rate_limit_sobre_mensaje_tool_use_failed(self):
        """16. GroqProvider prioriza la clasificación tipada de RateLimitError aunque el mensaje contenga 'tool_use_failed'."""
        provider = GroqProvider(api_key="gsk_test")
        req_http = httpx.Request("POST", "https://api.groq.com")
        resp_http = httpx.Response(429, request=req_http)

        rate_limit_con_tool_msg = groq.RateLimitError(
            "tool_use_failed: Rate limit exceeded (HTTP 429)",
            response=resp_http,
            body={"error": {"message": "tool_use_failed: Rate limit", "code": "rate_limit_exceeded"}},
        )

        from services.llm.providers.groq import _mapear_error_groq
        mapped = _mapear_error_groq(rate_limit_con_tool_msg)
        self.assertIsInstance(mapped, LLMRateLimitError)
        self.assertNotIsInstance(mapped, LLMToolCallRejectionError)

    async def test_groq_provider_prioriza_autenticacion_sobre_mensaje_tool_use_failed(self):
        """16b. GroqProvider prioriza AuthenticationError a LLMConfigurationError aunque el mensaje contenga 'tool_use_failed'."""
        req_http = httpx.Request("POST", "https://api.groq.com")
        resp_http = httpx.Response(401, request=req_http)

        auth_con_tool_msg = groq.AuthenticationError(
            "tool_use_failed: Invalid API Key (HTTP 401)",
            response=resp_http,
            body={"error": {"message": "tool_use_failed: Invalid API Key", "type": "invalid_request_error"}},
        )

        from services.llm.providers.groq import _mapear_error_groq
        mapped = _mapear_error_groq(auth_con_tool_msg)
        self.assertIsInstance(mapped, LLMConfigurationError)
        self.assertNotIsInstance(mapped, LLMToolCallRejectionError)

    async def test_groq_provider_bad_request_con_tool_use_failed_es_rejection(self):
        """16c. GroqProvider mapea BadRequestError (HTTP 400) con 'tool_use_failed' a LLMToolCallRejectionError."""
        req_http = httpx.Request("POST", "https://api.groq.com")
        resp_http = httpx.Response(400, request=req_http)

        bad_req = groq.BadRequestError(
            "tool_use_failed: El modelo invocó una tool no declarada",
            response=resp_http,
            body={"error": {"message": "tool_use_failed", "code": "tool_use_failed"}},
        )

        from services.llm.providers.groq import _mapear_error_groq
        mapped = _mapear_error_groq(bad_req)
        self.assertIsInstance(mapped, LLMToolCallRejectionError)

    async def test_groq_provider_error_no_400_con_tool_use_failed_no_es_rejection(self):
        """16d. GroqProvider mapea InternalServerError (HTTP 500) con 'tool_use_failed' a LLMProviderError, nunca a LLMToolCallRejectionError."""
        req_http = httpx.Request("POST", "https://api.groq.com")
        resp_http = httpx.Response(500, request=req_http)

        server_err = groq.InternalServerError(
            "tool_use_failed: Internal server error (500)",
            response=resp_http,
            body={"error": {"message": "tool_use_failed", "code": "server_error"}},
        )

        from services.llm.providers.groq import _mapear_error_groq
        mapped = _mapear_error_groq(server_err)
        self.assertIsInstance(mapped, LLMProviderError)
        self.assertNotIsInstance(mapped, LLMToolCallRejectionError)

    async def test_flujos_migrados_sin_fugas_sincuotagroq(self):
        """17. _procesar_mensaje y resumir_busqueda no dependen de SinCuotaGroq y capturan LLMRateLimitError."""
        # 1. _procesar_mensaje con LLMRateLimitError
        self.mock_provider.encolar_respuesta(LLMRateLimitError("Sin cuota neutral"))
        registro = {"ruta": None, "acciones": []}
        resp = await ms._procesar_mensaje(
            ms.ChatRequest(message="Explica la física cuántica y la teoría de cuerdas"),
            origen="pc",
            registro=registro,
        )
        self.assertEqual(registro["ruta"], "sin_cuota")
        self.assertIn("cuota", resp.response.lower())

        # 2. resumir_busqueda con LLMRateLimitError
        self.mock_provider.encolar_respuesta(LLMRateLimitError("Sin cuota en resumen"))
        resumen = await ms.resumir_busqueda("¿quién ganó?", "1. Noticia")
        # Ante fallo por cuota en resumen, devuelve fallback sin lanzar excepción
        self.assertTrue(len(resumen) > 0)

        # 3. resumir_busqueda con LLMTimeoutError
        self.mock_provider.encolar_respuesta(LLMTimeoutError("Timeout en resumen"))
        resumen_to = await ms.resumir_busqueda("¿quién ganó?", "Resultados:\n1. Noticia importante")
        self.assertIn("Noticia importante", resumen_to)

        # 4. resumir_busqueda con LLMProviderError
        self.mock_provider.encolar_respuesta(LLMProviderError("Error 500 en resumen"))
        resumen_err = await ms.resumir_busqueda("¿quién ganó?", "Resultados:\n1. Noticia fallback")
        self.assertIn("Noticia fallback", resumen_err)

    async def test_politica_recuperacion_neutral_tool_call_rejection_ocurre_exactamente_una_vez(self):
        """18. LLMToolCallRejectionError activa exactamente un reintento neutral sin herramientas."""
        error_alucinacion = LLMToolCallRejectionError("tool_use_failed: El modelo invocó una tool no declarada")
        respuesta_recuperada = LLMResponse(
            texto="Explicación sin ejecutar herramientas.",
            finish_reason="stop",
            modelo="mock-model",
        )

        self.mock_provider.encolar_respuesta(error_alucinacion)
        self.mock_provider.encolar_respuesta(respuesta_recuperada)

        registro = {"ruta": None, "acciones": []}
        with mock.patch.object(ms, "router_local", return_value=[]):
            resp = await ms._procesar_mensaje(
                ms.ChatRequest(message="Abre algo raro"),
                origen="pc",
                registro=registro,
            )

        # Debe haber hecho exactamente 2 intentos: con tools y sin tools
        self.assertEqual(len(self.mock_provider.solicitudes), 2)
        primera = self.mock_provider.solicitudes[0]
        segunda = self.mock_provider.solicitudes[1]

        self.assertIsNotNone(primera.tools)
        self.assertIsNone(segunda.tools)
        self.assertIsNone(segunda.tool_choice)
        self.assertEqual(resp.response, "Explicación sin ejecutar herramientas.")

    async def test_ausencia_reintentos_duplicados_ante_error_general(self):
        """19. Errores generales del proveedor (LLMProviderError) no gatillan fallback ni reintentos espurios."""
        error_general = LLMProviderError("Falla interna 500 no relacionada con tools")
        self.mock_provider.encolar_respuesta(error_general)

        registro = {"ruta": None, "acciones": []}
        with self.assertRaises(HTTPException) as ctx:
            await ms._procesar_mensaje(
                ms.ChatRequest(message="Pregunta con error general"),
                origen="pc",
                registro=registro,
            )

        self.assertEqual(ctx.exception.status_code, 500)
        # Verificación crítica: exactamente 1 solicitud (sin fallback)
        self.assertEqual(len(self.mock_provider.solicitudes), 1)

    async def test_ausencia_reintentos_duplicados_si_fallback_falla(self):
        """20. Si el reintento de recuperación falla con error general, no entra en bucle infinito (HTTP 500 directo)."""
        error_alucinacion = LLMToolCallRejectionError("tool_use_failed")
        error_segundo = LLMProviderError("Fallo también en reintento")

        self.mock_provider.encolar_respuesta(error_alucinacion)
        self.mock_provider.encolar_respuesta(error_segundo)

        registro = {"ruta": None, "acciones": []}
        with mock.patch.object(ms, "router_local", return_value=[]):
            with self.assertRaises(HTTPException) as ctx:
                await ms._procesar_mensaje(
                    ms.ChatRequest(message="Falla persistente"),
                    origen="pc",
                    registro=registro,
                )

        self.assertEqual(ctx.exception.status_code, 500)
        # Exactamente 2 solicitudes: intento 1 con tools y reintento 2 sin tools. NO hay intento 3.
        self.assertEqual(len(self.mock_provider.solicitudes), 2)

    async def test_ausencia_reintentos_duplicados_si_fallback_tambien_rechaza_tools(self):
        """21. Si el reintento falla con otro LLMToolCallRejectionError, no entra en bucle infinito."""
        error_1 = LLMToolCallRejectionError("tool_use_failed intento 1")
        error_2 = LLMToolCallRejectionError("tool_use_failed intento 2")

        self.mock_provider.encolar_respuesta(error_1)
        self.mock_provider.encolar_respuesta(error_2)

        registro = {"ruta": None, "acciones": []}
        with mock.patch.object(ms, "router_local", return_value=[]):
            with self.assertRaises(HTTPException) as ctx:
                await ms._procesar_mensaje(
                    ms.ChatRequest(message="Falla doble rechazo"),
                    origen="pc",
                    registro=registro,
                )

        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(len(self.mock_provider.solicitudes), 2)

    async def test_compatibilidad_llamar_groq_fachada(self):
        """22. La fachada llamar_groq() y la excepción SinCuotaGroq siguen disponibles para compatibilidad legacy."""
        self.assertTrue(callable(ms.llamar_groq))
        self.assertTrue(hasattr(ms, "SinCuotaGroq"))
        self.assertTrue(issubclass(ms.SinCuotaGroq, Exception))

    async def test_compatibilidad_server_client_whisper(self):
        """23. server.client y audio.transcriptions permanecen accesibles para Whisper/STT."""
        self.assertTrue(hasattr(ms, "client"))
        self.assertTrue(hasattr(ms.client, "audio"))
        self.assertTrue(hasattr(ms.client.audio, "transcriptions"))

    async def test_aislamiento_conversaciones_y_concurrencia(self):
        """24. Las solicitudes con el proveedor neutral respetan el aislamiento de turnos por origen."""
        self.mock_provider.encolar_respuesta(LLMResponse(texto="Respuesta para PC"))
        self.mock_provider.encolar_respuesta(LLMResponse(texto="Respuesta para Teléfono"))

        resp_pc = await ms.procesar_mensaje(ms.ChatRequest(message="Pregunta PC"), origen="pc")
        resp_tel = await ms.procesar_mensaje(ms.ChatRequest(message="Pregunta Teléfono"), origen="telefono")

        self.assertEqual(resp_pc.response, "Respuesta para PC")
        self.assertEqual(resp_tel.response, "Respuesta para Teléfono")

        self.assertEqual(len(ms.historiales_por_origen["pc"]), 1)
        self.assertEqual(len(ms.historiales_por_origen["telefono"]), 1)
        self.assertEqual(ms.historiales_por_origen["pc"][0].usuario, "Pregunta PC")
        self.assertEqual(ms.historiales_por_origen["telefono"][0].usuario, "Pregunta Teléfono")

    async def test_resumir_busqueda_usa_contrato_neutral(self):
        """25. resumir_busqueda construye un LLMRequest neutral y consume LLMResponse."""
        self.mock_provider.encolar_respuesta(
            LLMResponse(texto="Argentina ganó en Qatar 2022.")
        )

        resumen = await ms.resumir_busqueda("¿quién ganó el mundial?", "Resultados: 1. Argentina")
        self.assertEqual(resumen, "Argentina ganó en Qatar 2022.")

        self.assertEqual(len(self.mock_provider.solicitudes), 1)
        req = self.mock_provider.solicitudes[0]
        self.assertIsInstance(req, LLMRequest)
        self.assertIsNone(req.tools)
        self.assertEqual(req.temperature, 0.2)
        self.assertEqual(req.max_tokens, 250)
        self.assertEqual(req.reasoning_effort, "low")

    async def test_flujo_conversacional_del_nucleo_opera_via_llm_provider_sin_acceder_al_sdk_groq(self):
        """26. El flujo conversacional del núcleo (_procesar_mensaje) opera exclusivamente vía LLMProvider sin invocar chat completions del SDK de Groq."""
        proveedor_externo = MockAgnosticLLMProvider([
            LLMResponse(texto="Respuesta completamente independiente de Groq.")
        ])
        ms.fijar_llm_provider(proveedor_externo)

        with mock.patch.object(ms.client.chat.completions, "create") as mock_groq_create:
            resp = await ms.procesar_mensaje(ms.ChatRequest(message="Hola desde proveedor independiente"), origen="pc")
            mock_groq_create.assert_not_called()

        self.assertEqual(resp.response, "Respuesta completamente independiente de Groq.")
        self.assertEqual(len(proveedor_externo.solicitudes), 1)
        self.assertIsInstance(proveedor_externo.solicitudes[0], LLMRequest)

    async def test_analizar_pantalla_despacha_via_llm_provider_sin_acceder_al_sdk_groq(self):
        """27. analizar_pantalla delega en prov.analizar_vision() sin invocar client.chat.completions.create."""
        captura_id = "test_vision_01"
        ruta_tmp = f"/tmp/captura_madeline_{captura_id}.png"
        with open(ruta_tmp, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRdummy_png")

        self.mock_provider.encolar_respuesta_vision("Veo una ventana con VS Code abierta.")

        try:
            with mock.patch.object(ms, "tomar_captura", return_value=f"CAPTURA_LISTA:{captura_id}"):
                with mock.patch.object(ms.client.chat.completions, "create") as mock_groq_create:
                    texto = await ms.analizar_pantalla("¿qué ventana tengo abierta?")
                    mock_groq_create.assert_not_called()

            self.assertEqual(texto, "Veo una ventana con VS Code abierta.")
            self.assertEqual(len(self.mock_provider.vision_calls), 1)
            call = self.mock_provider.vision_calls[0]
            self.assertEqual(call["extension"], "png")
            self.assertEqual(call["pregunta"], "¿qué ventana tengo abierta?")
            self.assertEqual(call["model"], ms.MODEL_VISION)
            self.assertEqual(ms.ULTIMA_CAPTURA_ANALISIS, captura_id)
        finally:
            if os.path.exists(ruta_tmp):
                os.remove(ruta_tmp)

    async def test_analizar_pantalla_asincrono_no_bloquea_event_loop(self):
        """28. analizar_pantalla es una función asíncrona (coroutine) que no bloquea la concurrencia."""
        import inspect
        self.assertTrue(inspect.iscoroutinefunction(ms.analizar_pantalla))

        captura_id = "test_vision_async_02"
        ruta_tmp = f"/tmp/captura_madeline_{captura_id}.png"
        with open(ruta_tmp, "wb") as f:
            f.write(b"\x89PNG\r\ndummy")

        flag_concurrente = False

        async def _tarea_concurrente():
            nonlocal flag_concurrente
            await asyncio.sleep(0.005)
            flag_concurrente = True

        async def _vision_lenta(*args, **kwargs):
            await asyncio.sleep(0.02)
            return "Análisis asíncrono completado."

        try:
            with mock.patch.object(ms, "tomar_captura", return_value=f"CAPTURA_LISTA:{captura_id}"):
                with mock.patch.object(self.mock_provider, "analizar_vision", side_effect=_vision_lenta):
                    t1 = asyncio.create_task(_tarea_concurrente())
                    t2 = asyncio.create_task(ms.analizar_pantalla("¿qué ves?"))
                    res_vision, _ = await asyncio.gather(t2, t1)

            self.assertEqual(res_vision, "Análisis asíncrono completado.")
            self.assertTrue(flag_concurrente, "La tarea concurrente debió progresar mientras corría el análisis de visión.")
        finally:
            if os.path.exists(ruta_tmp):
                os.remove(ruta_tmp)

    async def test_analizar_pantalla_flujo_completo_chat_router_local(self):
        """29. Pedido de visión vía router local se resuelve de forma no bloqueante y adjunta imagen."""
        captura_id = "test_vision_router_03"
        ruta_tmp = f"/tmp/captura_madeline_{captura_id}.png"
        with open(ruta_tmp, "wb") as f:
            f.write(b"\x89PNG\r\ndummy")

        self.mock_provider.encolar_respuesta_vision("Hay un navegador con documentación técnica.")

        try:
            with mock.patch.object(ms, "tomar_captura", return_value=f"CAPTURA_LISTA:{captura_id}"):
                resp = await ms.procesar_mensaje(
                    ms.ChatRequest(message="analiza mi pantalla y dime qué ves"),
                    origen="pc"
                )

            self.assertIn("Hay un navegador con documentación técnica.", resp.response)
            self.assertEqual(resp.image_path, f"/captura/{captura_id}")
            self.assertEqual(ms.ULTIMA_CAPTURA_ANALISIS, captura_id)
        finally:
            if os.path.exists(ruta_tmp):
                os.remove(ruta_tmp)

    async def test_analizar_pantalla_flujo_completo_chat_tool_call(self):
        """30. LLM invocando tool call analizar_pantalla ejecuta la corrutina con await y adjunta imagen."""
        captura_id = "test_vision_tool_04"
        ruta_tmp = f"/tmp/captura_madeline_{captura_id}.png"
        with open(ruta_tmp, "wb") as f:
            f.write(b"\x89PNG\r\ndummy")

        # El LLM decide invocar la herramienta analizar_pantalla
        self.mock_provider.encolar_respuesta(
            LLMResponse(
                tool_calls=[
                    LLMToolCall(
                        id="call_vision_123",
                        nombre="analizar_pantalla",
                        argumentos={"pregunta": "mira esto detalladamente"},
                    )
                ]
            )
        )
        self.mock_provider.encolar_respuesta_vision("Es un diagrama de arquitectura limpia.")

        try:
            with mock.patch.object(ms, "tomar_captura", return_value=f"CAPTURA_LISTA:{captura_id}"):
                resp = await ms.procesar_mensaje(
                    ms.ChatRequest(message="Mira esto detalladamente"),
                    origen="pc"
                )

            self.assertIn("Es un diagrama de arquitectura limpia.", resp.response)
            self.assertEqual(resp.image_path, f"/captura/{captura_id}")
            self.assertEqual(ms.ULTIMA_CAPTURA_ANALISIS, captura_id)
        finally:
            if os.path.exists(ruta_tmp):
                os.remove(ruta_tmp)

    async def test_analizar_pantalla_manejo_error_rate_limit(self):
        """31. LLMRateLimitError en visión entrega mensaje amigable y limpia el archivo temporal."""
        captura_id = "test_vision_rl_05"
        ruta_tmp = f"/tmp/captura_madeline_{captura_id}.png"
        with open(ruta_tmp, "wb") as f:
            f.write(b"\x89PNG\r\ndummy")

        self.mock_provider.encolar_respuesta_vision(
            LLMRateLimitError("Cuota agotada en visión", provider="groq")
        )

        with mock.patch.object(ms, "tomar_captura", return_value=f"CAPTURA_LISTA:{captura_id}"):
            res = await ms.analizar_pantalla("¿qué ves?")

        self.assertIn("me quedé sin cuota de Groq por ahora", res)
        self.assertFalse(os.path.exists(ruta_tmp), "El archivo temporal debió eliminarse al fallar el análisis.")
        self.assertIsNone(ms.ULTIMA_CAPTURA_ANALISIS)

    async def test_analizar_pantalla_manejo_error_timeout(self):
        """32. LLMTimeoutError en visión entrega mensaje claro y limpia el archivo temporal."""
        captura_id = "test_vision_to_06"
        ruta_tmp = f"/tmp/captura_madeline_{captura_id}.png"
        with open(ruta_tmp, "wb") as f:
            f.write(b"\x89PNG\r\ndummy")

        self.mock_provider.encolar_respuesta_vision(
            LLMTimeoutError("Timeout de conexión con visión", provider="groq")
        )

        with mock.patch.object(ms, "tomar_captura", return_value=f"CAPTURA_LISTA:{captura_id}"):
            res = await ms.analizar_pantalla("¿qué ves?")

        self.assertIn("tiempo de espera se agotó", res)
        self.assertFalse(os.path.exists(ruta_tmp))
        self.assertIsNone(ms.ULTIMA_CAPTURA_ANALISIS)

    async def test_analizar_pantalla_manejo_error_llm_general(self):
        """33. LLMError general en visión es capturado con registro y limpieza sin lanzar excepción no controlada."""
        captura_id = "test_vision_err_07"
        ruta_tmp = f"/tmp/captura_madeline_{captura_id}.png"
        with open(ruta_tmp, "wb") as f:
            f.write(b"\x89PNG\r\ndummy")

        self.mock_provider.encolar_respuesta_vision(
            LLMError("Fallo interno del modelo de visión", provider="groq")
        )

        with mock.patch.object(ms, "tomar_captura", return_value=f"CAPTURA_LISTA:{captura_id}"):
            res = await ms.analizar_pantalla("¿qué ves?")

        self.assertIn("No pude analizar la pantalla: Fallo interno del modelo de visión", res)
        self.assertFalse(os.path.exists(ruta_tmp))
        self.assertIsNone(ms.ULTIMA_CAPTURA_ANALISIS)

    async def test_analizar_pantalla_respuesta_vacia_modelo_pensando(self):
        """34. Respuesta vacía de visión (thinking sin respuesta final) produce aviso amigable."""
        captura_id = "test_vision_empty_08"
        ruta_tmp = f"/tmp/captura_madeline_{captura_id}.png"
        with open(ruta_tmp, "wb") as f:
            f.write(b"\x89PNG\r\ndummy")

        self.mock_provider.encolar_respuesta_vision("")

        try:
            with mock.patch.object(ms, "tomar_captura", return_value=f"CAPTURA_LISTA:{captura_id}"):
                res = await ms.analizar_pantalla("¿qué ves?")

            self.assertIn("El modelo se quedó pensando y no llegó a responder a tiempo", res)
            self.assertIsNone(ms.ULTIMA_CAPTURA_ANALISIS)
        finally:
            if os.path.exists(ruta_tmp):
                os.remove(ruta_tmp)

    async def test_analizar_pantalla_compatibilidad_invocacion_sincrona(self):
        """35. ejecutar_comando_local('analizar_pantalla', ...) soporta ejecución síncrona sin loop activo."""
        import concurrent.futures

        captura_id = "test_vision_sync_09"
        ruta_tmp = f"/tmp/captura_madeline_{captura_id}.png"
        with open(ruta_tmp, "wb") as f:
            f.write(b"\x89PNG\r\ndummy")

        self.mock_provider.encolar_respuesta_vision("Ejecución síncrona exitosa.")

        def _llamar_en_hilo_sin_loop():
            # En un hilo separado no hay event loop activo por defecto
            with mock.patch.object(ms, "tomar_captura", return_value=f"CAPTURA_LISTA:{captura_id}"):
                return ms.ejecutar_comando_local("analizar_pantalla", {"pregunta": "sync check"})

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                res_sync = await asyncio.to_thread(_llamar_en_hilo_sin_loop)

            self.assertEqual(res_sync, "Ejecución síncrona exitosa.")
        finally:
            if os.path.exists(ruta_tmp):
                os.remove(ruta_tmp)

    async def test_analisis_simultaneo_pc_telefono_orden_inverso_y_aislamiento_capturas(self):
        """36. Dos análisis simultáneos (PC y Teléfono) con finalización en orden inverso aíslan sus capturas."""
        cap_pc = "simultaneo_pc_10"
        cap_tel = "simultaneo_tel_10"
        ruta_pc = f"/tmp/captura_madeline_{cap_pc}.png"
        ruta_tel = f"/tmp/captura_madeline_{cap_tel}.png"

        with open(ruta_pc, "wb") as f:
            f.write(b"\x89PNG\r\npc")
        with open(ruta_tel, "wb") as f:
            f.write(b"\x89PNG\r\ntel")

        # Mock de captura para entregar el ID correspondiente a cada invocación
        llamadas_captura = []

        def _mock_tomar_captura(ancho_max=1280):
            if not llamadas_captura:
                llamadas_captura.append(cap_pc)
                return ms.Resultado(ok=True, mensaje=f"CAPTURA_LISTA:{cap_pc}", evidencia={"captura_id": cap_pc})
            llamadas_captura.append(cap_tel)
            return ms.Resultado(ok=True, mensaje=f"CAPTURA_LISTA:{cap_tel}", evidencia={"captura_id": cap_tel})

        # Mock de visión: Teléfono termina PRIMERO (0.01s), PC termina SEGUNDO (0.04s)
        async def _mock_analizar_vision(imagen_bytes, extension, pregunta, model=None):
            if b"pc" in imagen_bytes or "pc" in pregunta:
                await asyncio.sleep(0.04)
                return "Análisis para PC completado"
            await asyncio.sleep(0.01)
            return "Análisis para Teléfono completado"

        try:
            with mock.patch.object(ms, "tomar_captura", side_effect=_mock_tomar_captura):
                with mock.patch.object(self.mock_provider, "analizar_vision", side_effect=_mock_analizar_vision):
                    # Despachar simultáneamente desde PC y Teléfono
                    task_pc = asyncio.create_task(
                        ms.procesar_mensaje(ms.ChatRequest(message="analiza mi pantalla y dime de pc"), origen="pc")
                    )
                    task_tel = asyncio.create_task(
                        ms.procesar_mensaje(ms.ChatRequest(message="analiza mi pantalla y dime de tel"), origen="telefono")
                    )
                    resp_pc, resp_tel = await asyncio.gather(task_pc, task_tel)

            # Verificar que no hubo asociación cruzada a pesar del orden de finalización invertido
            self.assertIn("Análisis para PC completado", resp_pc.response)
            self.assertEqual(resp_pc.image_path, f"/captura/{cap_pc}")

            self.assertIn("Análisis para Teléfono completado", resp_tel.response)
            self.assertEqual(resp_tel.image_path, f"/captura/{cap_tel}")

            # Ambas capturas deben permanecer en disco para que /captura/{id} las entregue al cliente
            self.assertTrue(os.path.exists(ruta_pc))
            self.assertTrue(os.path.exists(ruta_tel))
        finally:
            if os.path.exists(ruta_pc):
                os.remove(ruta_pc)
            if os.path.exists(ruta_tel):
                os.remove(ruta_tel)

    async def test_analisis_simultaneo_uno_exitoso_otro_falla_no_corrompe_captura(self):
        """37. Una inferencia exitosa simultánea con una fallida no pierde ni borra la captura exitosa."""
        cap_ok = "simultaneo_ok_11"
        cap_fail = "simultaneo_fail_11"
        ruta_ok = f"/tmp/captura_madeline_{cap_ok}.png"
        ruta_fail = f"/tmp/captura_madeline_{cap_fail}.png"

        with open(ruta_ok, "wb") as f:
            f.write(b"\x89PNG\r\nok")
        with open(ruta_fail, "wb") as f:
            f.write(b"\x89PNG\r\nfail")

        llamadas = []

        def _mock_captura(ancho_max=1280):
            if not llamadas:
                llamadas.append(cap_ok)
                return ms.Resultado(ok=True, mensaje=f"CAPTURA_LISTA:{cap_ok}", evidencia={"captura_id": cap_ok})
            llamadas.append(cap_fail)
            return ms.Resultado(ok=True, mensaje=f"CAPTURA_LISTA:{cap_fail}", evidencia={"captura_id": cap_fail})

        async def _mock_vision(imagen_bytes, extension, pregunta, model=None):
            if b"ok" in imagen_bytes or "ok" in pregunta:
                await asyncio.sleep(0.03)
                return "Inferencia exitosa de PC"
            await asyncio.sleep(0.01)
            raise LLMRateLimitError("Cuota agotada en visión móvil", provider="groq")

        try:
            with mock.patch.object(ms, "tomar_captura", side_effect=_mock_captura):
                with mock.patch.object(self.mock_provider, "analizar_vision", side_effect=_mock_vision):
                    task_ok = asyncio.create_task(
                        ms.procesar_mensaje(ms.ChatRequest(message="analiza mi pantalla y consulta ok"), origen="pc")
                    )
                    task_fail = asyncio.create_task(
                        ms.procesar_mensaje(ms.ChatRequest(message="analiza mi pantalla y consulta fail"), origen="telefono")
                    )
                    resp_ok, resp_fail = await asyncio.gather(task_ok, task_fail)

            # La solicitud exitosa conserva su imagen y respuesta
            self.assertIn("Inferencia exitosa de PC", resp_ok.response)
            self.assertEqual(resp_ok.image_path, f"/captura/{cap_ok}")
            self.assertTrue(os.path.exists(ruta_ok), "La captura exitosa debe preservarse en disco.")

            # La solicitud fallida no tiene imagen y su archivo temporal fue limpiado
            self.assertIn("me quedé sin cuota de Groq por ahora", resp_fail.response)
            self.assertIsNone(resp_fail.image_path)
            self.assertFalse(os.path.exists(ruta_fail), "La captura fallida debe eliminarse de disco.")
        finally:
            if os.path.exists(ruta_ok):
                os.remove(ruta_ok)
            if os.path.exists(ruta_fail):
                os.remove(ruta_fail)

    async def test_cancelacion_durante_inferencia_vision_limpia_archivo(self):
        """38. Cancelar el await durante la inferencia de visión elimina el archivo temporal sin dejar huérfanos."""
        cap_id = "cancel_infer_12"
        ruta_tmp = f"/tmp/captura_madeline_{cap_id}.png"
        with open(ruta_tmp, "wb") as f:
            f.write(b"\x89PNG\r\ndummy")

        evento_inicio_vision = asyncio.Event()

        async def _vision_lenta(*args, **kwargs):
            evento_inicio_vision.set()
            await asyncio.sleep(1.0)
            return "No debería llegar aquí"

        with mock.patch.object(ms, "tomar_captura", return_value=f"CAPTURA_LISTA:{cap_id}"):
            with mock.patch.object(self.mock_provider, "analizar_vision", side_effect=_vision_lenta):
                task = asyncio.create_task(ms.analizar_pantalla("¿qué ves?"))
                await evento_inicio_vision.wait()
                # Cancelar la tarea mientras espera en el proveedor de visión
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        # El bloque de cancelación debió eliminar el archivo temporal
        self.assertFalse(os.path.exists(ruta_tmp), "El archivo temporal debió eliminarse al cancelarse la tarea.")

    async def test_cancelacion_durante_creacion_captura_limpia_post_terminacion(self):
        """39. Cancelar el await mientras el worker thread ejecuta tomar_captura() limpia el archivo al terminar."""
        cap_id = "cancel_screen_13"
        ruta_tmp = f"/tmp/captura_madeline_{cap_id}.png"

        def _tomar_captura_lenta(ancho_max=1280):
            time.sleep(0.04)
            with open(ruta_tmp, "wb") as f:
                f.write(b"\x89PNG\r\ncreada_por_worker")
            return ms.Resultado(ok=True, mensaje=f"CAPTURA_LISTA:{cap_id}", evidencia={"captura_id": cap_id})

        with mock.patch.object(ms, "tomar_captura", side_effect=_tomar_captura_lenta):
            task = asyncio.create_task(ms.analizar_pantalla("¿qué ves?"))
            # Cancelar inmediatamente antes de que el worker termine
            await asyncio.sleep(0.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        # Esperar a que el worker thread en segundo plano termine de ejecutar
        await asyncio.sleep(0.08)

        # El callback del executor debió limpiar el archivo post-terminación sin interrumpir el worker
        self.assertFalse(os.path.exists(ruta_tmp), "El worker terminado debió ser limpiado por el callback del executor.")

    async def test_preservacion_captura_exitosa_para_entrega_cliente(self):
        """40. Las capturas de análisis exitosos no se eliminan y quedan listas para /captura/{id}."""
        cap_id = "deliver_ok_14"
        ruta_tmp = f"/tmp/captura_madeline_{cap_id}.png"
        with open(ruta_tmp, "wb") as f:
            f.write(b"\x89PNG\r\ndata")

        self.mock_provider.encolar_respuesta_vision("Ventana de terminal activa.")

        try:
            with mock.patch.object(ms, "tomar_captura", return_value=f"CAPTURA_LISTA:{cap_id}"):
                res = await ms.analizar_pantalla("¿qué ventana tengo?")

            self.assertTrue(res.ok)
            self.assertEqual(res.evidencia.get("captura_id"), cap_id)
            self.assertTrue(os.path.exists(ruta_tmp), "El archivo temporal debe preservarse para la entrega al usuario.")
        finally:
            if os.path.exists(ruta_tmp):
                os.remove(ruta_tmp)

    async def test_despacho_asincrono_con_awaitables_y_compatibilidad_sincrona(self):
        """41. ejecutar_comandos_locales_async despacha herramientas síncronas y awaitables sin advertencias de runtime."""
        # Herramienta síncrona
        res_sync = ms.ejecutar_comando_local("volumen", {"accion": "fijar", "porcentaje": 35})
        self.assertFalse(inspect.isawaitable(res_sync))

        # Herramienta asíncrona
        res_async = ms.ejecutar_comando_local("analizar_pantalla", {"pregunta": "test awaitable"})
        self.assertTrue(inspect.isawaitable(res_async))
        res_async.close()

        # Despacho combinado en ejecutar_comandos_locales_async
        cap_id = "combined_15"
        ruta_tmp = f"/tmp/captura_madeline_{cap_id}.png"
        with open(ruta_tmp, "wb") as f:
            f.write(b"\x89PNG\r\ncombined")

        self.mock_provider.encolar_respuesta_vision("Pantalla analizada en despacho mixto.")

        try:
            with mock.patch.object(ms, "tomar_captura", return_value=f"CAPTURA_LISTA:{cap_id}"):
                resultados = await ms.ejecutar_comandos_locales_async([
                    ("volumen", {"accion": "fijar", "porcentaje": 40}),
                    ("analizar_pantalla", {"pregunta": "test"}),
                ])

            self.assertEqual(len(resultados), 2)
            self.assertEqual(resultados[0][0], "volumen")
            self.assertTrue(resultados[0][2].ok)

            self.assertEqual(resultados[1][0], "analizar_pantalla")
            self.assertTrue(resultados[1][2].ok)
            self.assertEqual(resultados[1][2].evidencia.get("captura_id"), cap_id)
        finally:
            if os.path.exists(ruta_tmp):
                os.remove(ruta_tmp)

    async def test_cancelacion_en_ventana_post_worker_captura_holder_limpia_archivo(self):
        """42. Cancelación en la ventana residual (worker terminó con cancel_token=False pero corrutina cancelada)."""
        cap_id = "holder_clean_16"
        ruta_tmp = f"/tmp/captura_madeline_{cap_id}.png"

        def _worker_tomar_captura(ancho_max=1280):
            with open(ruta_tmp, "wb") as f:
                f.write(b"\x89PNG\r\ncreada_por_worker_post")
            return ms.Resultado(ok=True, mensaje=f"CAPTURA_LISTA:{cap_id}", evidencia={"captura_id": cap_id})

        real_to_thread = asyncio.to_thread

        async def _to_thread_cancelado_justo_al_terminar_worker(func, *args, **kwargs):
            # Ejecuta el worker normalmente: se guarda en captura_holder y cancel_token es False
            res = await real_to_thread(func, *args, **kwargs)
            # Simula que la tarea asyncio recibió cancelación inmediatamente tras la finalización del worker
            raise asyncio.CancelledError()

        with mock.patch.object(ms, "tomar_captura", side_effect=_worker_tomar_captura):
            with mock.patch("asyncio.to_thread", side_effect=_to_thread_cancelado_justo_al_terminar_worker):
                with self.assertRaises(asyncio.CancelledError):
                    await ms.analizar_pantalla("¿qué ves?")

        # captura_holder debió permitir al bloque except CancelledError limpiar el archivo creado
        self.assertFalse(os.path.exists(ruta_tmp), "El archivo debió eliminarse mediante captura_holder en la ventana residual.")

    async def test_solicitud_sin_evidencia_no_consume_captura_global_de_otra(self):
        """43. Una solicitud cuya herramienta no aporta evidencia no asocia captura ni consume la global de otra."""
        cap_global_ajena = "global_ajena_17"
        ruta_ajena = f"/tmp/captura_madeline_{cap_global_ajena}.png"
        with open(ruta_ajena, "wb") as f:
            f.write(b"\x89PNG\r\najena")

        # Se fija una captura global previa (simulando otra solicitud o estado residual externo)
        ms.ULTIMA_CAPTURA_ANALISIS = cap_global_ajena

        # Resultado de herramienta sin evidencia de captura_id
        resultado_sin_evidencia = ms.Resultado(
            ok=True,
            mensaje="Análisis completado sin captura asociada",
            evidencia={},
        )

        try:
            # 1. Camino router local
            with mock.patch.object(ms, "ejecutar_comandos_locales_async", return_value=[
                ("analizar_pantalla", {"pregunta": "dime qué ves"}, resultado_sin_evidencia)
            ]):
                req = ms.ChatRequest(message="analiza mi pantalla y dime qué ves")
                resp = await ms.procesar_mensaje(req, origen="pc")

            # No debe asociar imagen porque falta evidencia individual
            self.assertIsNone(resp.image_path, "No debe asociarse imagen si falta evidencia individual.")
            # La variable global de compatibilidad externa no debe haber sido consumida como fallback
            self.assertEqual(ms.ULTIMA_CAPTURA_ANALISIS, cap_global_ajena)
            self.assertTrue(os.path.exists(ruta_ajena), "El archivo ajeno no debe ser alterado.")

            # 2. Camino tool calls
            self.mock_provider.encolar_respuesta(
                LLMResponse(
                    tool_calls=[
                        LLMToolCall(
                            id="tc_sin_evidencia",
                            nombre="analizar_pantalla",
                            argumentos={"pregunta": "mira esto"},
                        )
                    ]
                )
            )
            with mock.patch.dict(ms.TOOLS_FUNCIONES, {"analizar_pantalla": mock.AsyncMock(return_value=resultado_sin_evidencia)}):
                resp_tool = await ms.procesar_mensaje(ms.ChatRequest(message="Mira esto detalladamente"), origen="pc")

            self.assertIsNone(resp_tool.image_path, "Tool call sin evidencia no debe asociar imagen.")
            self.assertEqual(ms.ULTIMA_CAPTURA_ANALISIS, cap_global_ajena)
            self.assertTrue(os.path.exists(ruta_ajena))
        finally:
            ms.ULTIMA_CAPTURA_ANALISIS = None
            if os.path.exists(ruta_ajena):
                os.remove(ruta_ajena)


if __name__ == "__main__":
    unittest.main()
