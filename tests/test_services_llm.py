"""Pruebas unitarias para el subsistema de servicios LLM agnóstico y adaptador Groq (U-4.2A).

Cubre:
1. Contratos neutrales y tipado (LLMRole, LLMMessage, LLMToolDefinition, LLMToolChoice,
   LLMRequest, LLMResponse, LLMToolCall, validación estricta y ausencia de dependencias Groq).
2. Configuración histórica del adaptador Groq (modelo por defecto 'openai/gpt-oss-120b',
   visión 'qwen/qwen3.6-27b', timeout 20s, max_retries 1, inicialización perezosa).
3. Conversión de estructuras neutrales a payload Groq y generación de texto.
4. Soporte y mapeo de tool calls (un tool call, múltiples, argumentos inválidos).
5. Análisis multimodal de visión (validación estricta de PNG/JPG/JPEG, normalización MIME,
   rechazo de extensiones no admitidas, parámetros históricos, limpieza de <think>).
6. Concurrencia y cancelación (ejecución no bloqueante en to_thread, semántica de cancelación).
7. Mapeo exhaustivo de excepciones y preservación estricta de causalidad (__cause__).
"""

import asyncio
import base64
import os
import sys
import threading
import time
import unittest
from unittest import mock

import groq
import httpx

from server.services.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMRole,
    LLMToolCall,
    LLMToolChoice,
    LLMToolChoiceMode,
    LLMToolDefinition,
)
from server.services.llm.exceptions import (
    LLMConfigurationError,
    LLMConnectionError,
    LLMError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMValidationError,
)
from server.services.llm.providers.groq import GroqProvider


# Helpers para simular respuestas del SDK de Groq
class MockMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class MockChoice:
    def __init__(self, message=None, finish_reason="stop"):
        self.message = message or MockMessage()
        self.finish_reason = finish_reason


class MockFunctionCall:
    def __init__(self, name="", arguments=""):
        self.name = name
        self.arguments = arguments


class MockGroqToolCall:
    def __init__(self, id="call_1", name="test_fn", arguments="{}"):
        self.id = id
        self.function = MockFunctionCall(name=name, arguments=arguments)


class MockChatCompletion:
    def __init__(self, content=None, tool_calls=None, model="openai/gpt-oss-120b", finish_reason="stop"):
        self.model = model
        self.choices = [
            MockChoice(
                message=MockMessage(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ]


class TestLLMContracts(unittest.TestCase):
    """1. Pruebas de contratos de datos neutrales y validación de tipos."""

    def test_roles_enum_y_valores(self):
        self.assertEqual(LLMRole.SYSTEM.value, "system")
        self.assertEqual(LLMRole.USER.value, "user")
        self.assertEqual(LLMRole.ASSISTANT.value, "assistant")
        self.assertEqual(LLMRole.TOOL.value, "tool")

    def test_message_constructores_helpers(self):
        sys_msg = LLMMessage.system("Instrucción del sistema")
        self.assertEqual(sys_msg.rol, LLMRole.SYSTEM)
        self.assertEqual(sys_msg.contenido, "Instrucción del sistema")

        user_msg = LLMMessage.user("Consulta del usuario")
        self.assertEqual(user_msg.rol, LLMRole.USER)
        self.assertEqual(user_msg.contenido, "Consulta del usuario")

        tc = LLMToolCall(id="c1", nombre="hora", argumentos={})
        asst_msg = LLMMessage.assistant(contenido=None, tool_calls=[tc])
        self.assertEqual(asst_msg.rol, LLMRole.ASSISTANT)
        self.assertIsNone(asst_msg.contenido)
        self.assertEqual(len(asst_msg.tool_calls), 1)

        tool_msg = LLMMessage.tool_result(tool_call_id="c1", contenido="14:00", nombre="hora")
        self.assertEqual(tool_msg.rol, LLMRole.TOOL)
        self.assertEqual(tool_msg.tool_call_id, "c1")
        self.assertEqual(tool_msg.contenido, "14:00")
        self.assertEqual(tool_msg.nombre, "hora")

    def test_message_validacion_rol_invalido(self):
        with self.assertRaises(LLMValidationError) as ctx:
            LLMMessage(rol="rol_inexistente", contenido="hola")
        self.assertIn("no reconocido", str(ctx.exception))

    def test_message_validacion_contenido_none_sin_tools(self):
        with self.assertRaises(LLMValidationError) as ctx:
            LLMMessage(rol=LLMRole.USER, contenido=None)
        self.assertIn("no puede tener contenido None sin tool_calls", str(ctx.exception))

    def test_message_validacion_contenido_tipo_invalido(self):
        with self.assertRaises(LLMValidationError) as ctx:
            LLMMessage(rol=LLMRole.USER, contenido=12345)
        self.assertIn("debe ser str o None", str(ctx.exception))

    def test_tool_definition_valida(self):
        td = LLMToolDefinition(
            nombre="obtener_clima",
            descripcion="Devuelve el clima de una ciudad",
            parametros={"type": "object", "properties": {"ciudad": {"type": "string"}}},
        )
        self.assertEqual(td.nombre, "obtener_clima")
        self.assertEqual(td.descripcion, "Devuelve el clima de una ciudad")
        self.assertEqual(td.parametros["type"], "object")

    def test_tool_definition_nombre_vacio_lanza_error(self):
        with self.assertRaises(LLMValidationError) as ctx:
            LLMToolDefinition(nombre="", descripcion="desc")
        self.assertIn("no vacía", str(ctx.exception))

    def test_tool_definition_parametros_no_dict_lanza_error(self):
        with self.assertRaises(LLMValidationError) as ctx:
            LLMToolDefinition(nombre="f", descripcion="d", parametros="no_dict")
        self.assertIn("deben ser un diccionario", str(ctx.exception))

    def test_tool_choice_modos(self):
        c_auto = LLMToolChoice.auto()
        self.assertEqual(c_auto.modo, LLMToolChoiceMode.AUTO)

        c_none = LLMToolChoice.none()
        self.assertEqual(c_none.modo, LLMToolChoiceMode.NONE)

        c_req = LLMToolChoice.required()
        self.assertEqual(c_req.modo, LLMToolChoiceMode.REQUIRED)

        c_spec = LLMToolChoice.specific("mi_tool")
        self.assertEqual(c_spec.modo, LLMToolChoiceMode.SPECIFIC)
        self.assertEqual(c_spec.nombre_herramienta, "mi_tool")

    def test_tool_choice_specific_sin_nombre_lanza_error(self):
        with self.assertRaises(LLMValidationError) as ctx:
            LLMToolChoice(modo=LLMToolChoiceMode.SPECIFIC, nombre_herramienta=None)
        self.assertIn("requiere especificar nombre_herramienta", str(ctx.exception))

    def test_tool_choice_modo_invalido_lanza_error(self):
        with self.assertRaises(LLMValidationError) as ctx:
            LLMToolChoice(modo="modo_inventado")
        self.assertIn("no reconocido", str(ctx.exception))

    def test_request_valida_con_y_sin_tools(self):
        req_sin_tools = LLMRequest(mensajes=[LLMMessage.user("hola")])
        self.assertEqual(len(req_sin_tools.mensajes), 1)
        self.assertIsNone(req_sin_tools.tools)

        req_con_tools = LLMRequest(
            mensajes=[LLMMessage.user("busca")],
            tools=[LLMToolDefinition(nombre="t", descripcion="d")],
            tool_choice=LLMToolChoice.auto(),
        )
        self.assertEqual(len(req_con_tools.tools), 1)
        self.assertEqual(req_con_tools.tool_choice.modo, LLMToolChoiceMode.AUTO)

    def test_request_mensajes_vacio_lanza_error(self):
        with self.assertRaises(LLMValidationError) as ctx:
            LLMRequest(mensajes=[])
        self.assertIn("lista no vacía", str(ctx.exception))

    def test_request_elemento_no_llmmessage_lanza_error(self):
        with self.assertRaises(LLMValidationError) as ctx:
            LLMRequest(mensajes=["no_es_llmmessage"])
        self.assertIn("debe ser una instancia de LLMMessage", str(ctx.exception))

    def test_request_tool_no_llmtooldefinition_lanza_error(self):
        with self.assertRaises(LLMValidationError) as ctx:
            LLMRequest(mensajes=[LLMMessage.user("a")], tools=["no_es_tool_def"])
        self.assertIn("debe ser una instancia de LLMToolDefinition", str(ctx.exception))

    def test_response_normal_text_sin_tools(self):
        resp = LLMResponse(texto="Respuesta simple", modelo="modelo-a", proveedor="groq")
        self.assertEqual(resp.texto, "Respuesta simple")
        self.assertEqual(resp.tool_calls, [])
        self.assertIsNone(resp.finish_reason)
        self.assertEqual(resp.modelo, "modelo-a")
        self.assertEqual(resp.proveedor, "groq")

    def test_response_multiple_tool_calls_texto_none(self):
        tc1 = LLMToolCall(id="c1", nombre="fn1", argumentos={"x": 1})
        tc2 = LLMToolCall(id="c2", nombre="fn2", argumentos={"y": 2})
        resp = LLMResponse(
            texto=None,
            tool_calls=[tc1, tc2],
            finish_reason="tool_calls",
            modelo="modelo-b",
            proveedor="groq",
        )
        self.assertIsNone(resp.texto)
        self.assertEqual(len(resp.tool_calls), 2)
        self.assertEqual(resp.tool_calls[0].nombre, "fn1")
        self.assertEqual(resp.tool_calls[1].nombre, "fn2")
        self.assertEqual(resp.finish_reason, "tool_calls")

    def test_tool_call_from_raw_json_string(self):
        tc = LLMToolCall.from_raw("c1", "calcular", '{"a": 10, "b": "suma"}')
        self.assertEqual(tc.id, "c1")
        self.assertEqual(tc.nombre, "calcular")
        self.assertEqual(tc.argumentos, {"a": 10, "b": "suma"})

    def test_tool_call_from_raw_dict(self):
        tc = LLMToolCall.from_raw("c2", "buscar", {"query": "test"})
        self.assertEqual(tc.argumentos, {"query": "test"})

    def test_tool_call_direct_post_init_validation(self):
        tc = LLMToolCall(id="c3", nombre="ok", argumentos={"valido": True})
        self.assertTrue(tc.argumentos["valido"])

        with self.assertRaises(LLMValidationError) as ctx:
            LLMToolCall(id="c4", nombre="err", argumentos="string no dict")
        self.assertIn("deben ser un diccionario", str(ctx.exception))

    def test_tool_call_from_raw_invalid_json(self):
        with self.assertRaises(LLMValidationError) as ctx:
            LLMToolCall.from_raw("c1", "test", '{"malformado":')
        self.assertIn("no son un JSON válido", str(ctx.exception))
        self.assertIsNotNone(ctx.exception.__cause__)

    def test_tool_call_from_raw_json_array_fails(self):
        with self.assertRaises(LLMValidationError) as ctx:
            LLMToolCall.from_raw("c1", "test", "[1, 2, 3]")
        self.assertIn("deben ser un objeto JSON (dict)", str(ctx.exception))

    def test_contratos_sin_dependencias_groq(self):
        import server.services.llm.base as base_mod
        import server.services.llm.exceptions as exc_mod

        with open(base_mod.__file__, "r", encoding="utf-8") as f:
            base_src = f.read()
        with open(exc_mod.__file__, "r", encoding="utf-8") as f:
            exc_src = f.read()

        self.assertNotIn("import groq", base_src)
        self.assertNotIn("from groq", base_src)
        self.assertNotIn("import httpx", base_src)
        self.assertNotIn("from httpx", base_src)

        self.assertNotIn("import groq", exc_src)
        self.assertNotIn("from groq", exc_src)
        self.assertNotIn("import httpx", exc_src)
        self.assertNotIn("from httpx", exc_src)


class TestGroqProviderInit(unittest.IsolatedAsyncioTestCase):
    """2. Pruebas de configuración histórica e inicialización de GroqProvider."""

    def test_modelo_predeterminado_es_openai_gpt_oss_120b(self):
        provider = GroqProvider(api_key="gsk_test")
        self.assertEqual(provider.default_model, "openai/gpt-oss-120b")
        self.assertEqual(provider.nombre, "groq")

    def test_modelo_vision_predeterminado(self):
        provider = GroqProvider(api_key="gsk_test")
        self.assertEqual(provider.default_vision_model, "qwen/qwen3.6-27b")

    def test_timeout_y_retries_predeterminados(self):
        provider = GroqProvider(api_key="gsk_test")
        self.assertEqual(provider._timeout, 20.0)
        self.assertEqual(provider._max_retries, 1)

    def test_timeout_y_retries_personalizados(self):
        provider = GroqProvider(api_key="gsk_test", timeout=45.0, max_retries=2)
        client = provider._get_client()
        self.assertEqual(provider._timeout, 45.0)
        self.assertEqual(provider._max_retries, 2)
        self.assertEqual(client.max_retries, 2)

    async def test_lazy_init_sin_api_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GROQ_API_KEY", None)
            provider = GroqProvider()
            self.assertEqual(provider.nombre, "groq")

            req = LLMRequest(mensajes=[LLMMessage.user("hola")])
            with self.assertRaises(LLMConfigurationError) as ctx:
                await provider.generar_respuesta(req)
            self.assertIn("GROQ_API_KEY no está configurada", str(ctx.exception))

    def test_init_con_api_key_explicita(self):
        provider = GroqProvider(api_key="gsk_explicita_123")
        client = provider._get_client()
        self.assertEqual(client.api_key, "gsk_explicita_123")

    def test_init_con_api_key_en_env(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk_env_999"}):
            provider = GroqProvider()
            client = provider._get_client()
            self.assertEqual(client.api_key, "gsk_env_999")


class TestGroqProviderGeneracion(unittest.IsolatedAsyncioTestCase):
    """3. Pruebas de conversión y generación de respuestas."""

    def test_conversion_mensajes_neutrales_a_payload_groq(self):
        provider = GroqProvider(api_key="gsk_test")
        tc = LLMToolCall(id="call_x", nombre="calcular", argumentos={"val": 5})

        mensajes = [
            LLMMessage.system("Instrucción"),
            LLMMessage.user("Hola"),
            LLMMessage.assistant(contenido="Llamo tool", tool_calls=[tc]),
            LLMMessage.tool_result(tool_call_id="call_x", contenido="resultado 10", nombre="calcular"),
        ]

        payload = provider._convertir_mensajes(mensajes)
        self.assertEqual(len(payload), 4)

        self.assertEqual(payload[0], {"role": "system", "content": "Instrucción"})
        self.assertEqual(payload[1], {"role": "user", "content": "Hola"})

        self.assertEqual(payload[2]["role"], "assistant")
        self.assertEqual(payload[2]["content"], "Llamo tool")
        self.assertEqual(len(payload[2]["tool_calls"]), 1)
        self.assertEqual(payload[2]["tool_calls"][0]["id"], "call_x")
        self.assertEqual(payload[2]["tool_calls"][0]["function"]["name"], "calcular")
        self.assertEqual(payload[2]["tool_calls"][0]["function"]["arguments"], '{"val": 5}')

        self.assertEqual(payload[3]["role"], "tool")
        self.assertEqual(payload[3]["tool_call_id"], "call_x")
        self.assertEqual(payload[3]["content"], "resultado 10")
        self.assertEqual(payload[3]["name"], "calcular")

    def test_conversion_tools_neutrales_a_esquema_groq(self):
        provider = GroqProvider(api_key="gsk_test")
        tools = [
            LLMToolDefinition(
                nombre="buscar",
                descripcion="Busca información",
                parametros={"type": "object", "properties": {"q": {"type": "string"}}},
            )
        ]
        groq_tools = provider._convertir_tools(tools)
        self.assertEqual(len(groq_tools), 1)
        self.assertEqual(groq_tools[0]["type"], "function")
        self.assertEqual(groq_tools[0]["function"]["name"], "buscar")
        self.assertEqual(groq_tools[0]["function"]["description"], "Busca información")
        self.assertEqual(groq_tools[0]["function"]["parameters"]["type"], "object")

    def test_conversion_tool_choice_auto_none_required_specific(self):
        provider = GroqProvider(api_key="gsk_test")

        self.assertEqual(provider._convertir_tool_choice(LLMToolChoice.auto()), "auto")
        self.assertEqual(provider._convertir_tool_choice(LLMToolChoice.none()), "none")
        self.assertEqual(provider._convertir_tool_choice(LLMToolChoice.required()), "required")

        spec = provider._convertir_tool_choice(LLMToolChoice.specific("mi_tool"))
        self.assertEqual(spec, {"type": "function", "function": {"name": "mi_tool"}})

    async def test_generar_respuesta_construccion_parametros(self):
        mock_client = mock.MagicMock()
        mock_client.chat.completions.create.return_value = MockChatCompletion(
            content="Hola Madeline", model="openai/gpt-oss-120b", finish_reason="stop"
        )
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(
            mensajes=[LLMMessage.user("Hola")],
            tools=[LLMToolDefinition(nombre="f", descripcion="desc", parametros={})],
            tool_choice=LLMToolChoice.auto(),
            temperature=0.7,
            max_tokens=150,
            timeout=15.0,
        )

        resp = await provider.generar_respuesta(req)

        mock_client.chat.completions.create.assert_called_once()
        _, kwargs = mock_client.chat.completions.create.call_args

        self.assertEqual(kwargs["model"], "openai/gpt-oss-120b")
        self.assertEqual(len(kwargs["messages"]), 1)
        self.assertEqual(kwargs["messages"][0]["content"], "Hola")
        self.assertEqual(len(kwargs["tools"]), 1)
        self.assertEqual(kwargs["tool_choice"], "auto")
        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["max_tokens"], 150)
        self.assertEqual(kwargs["timeout"], 15.0)

        self.assertEqual(resp.texto, "Hola Madeline")
        self.assertEqual(resp.modelo, "openai/gpt-oss-120b")
        self.assertEqual(resp.proveedor, "groq")

    async def test_generar_respuesta_con_reasoning_effort_usa_max_completion_tokens(self):
        mock_client = mock.MagicMock()
        mock_client.chat.completions.create.return_value = MockChatCompletion(content="Respuesta razonada")
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(
            mensajes=[LLMMessage.user("pensá")],
            max_tokens=250,
            reasoning_effort="low",
        )

        await provider.generar_respuesta(req)

        _, kwargs = mock_client.chat.completions.create.call_args
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertEqual(kwargs["max_completion_tokens"], 250)
        self.assertNotIn("max_tokens", kwargs)


class TestGroqProviderToolCalls(unittest.IsolatedAsyncioTestCase):
    """4. Pruebas de invocación de herramientas (tool calls)."""

    async def test_mapeo_respuesta_con_un_tool_call(self):
        mock_client = mock.MagicMock()
        tc = MockGroqToolCall(
            id="call_abc123",
            name="obtener_clima",
            arguments='{"ciudad": "Buenos Aires", "unidades": "celsius"}',
        )
        mock_client.chat.completions.create.return_value = MockChatCompletion(
            content=None,
            tool_calls=[tc],
            finish_reason="tool_calls",
        )
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("¿cómo está el clima?")])
        resp = await provider.generar_respuesta(req)

        self.assertIsNone(resp.texto)
        self.assertEqual(len(resp.tool_calls), 1)
        call = resp.tool_calls[0]
        self.assertEqual(call.id, "call_abc123")
        self.assertEqual(call.nombre, "obtener_clima")
        self.assertEqual(call.argumentos, {"ciudad": "Buenos Aires", "unidades": "celsius"})
        self.assertEqual(resp.finish_reason, "tool_calls")

    async def test_mapeo_respuesta_con_multiples_tool_calls(self):
        mock_client = mock.MagicMock()
        tc1 = MockGroqToolCall(
            id="call_1",
            name="leer_archivo",
            arguments='{"ruta": "/tmp/test.txt"}',
        )
        tc2 = MockGroqToolCall(
            id="call_2",
            name="notificar_usuario",
            arguments='{"mensaje": "listo"}',
        )
        mock_client.chat.completions.create.return_value = MockChatCompletion(
            content="",
            tool_calls=[tc1, tc2],
            finish_reason="tool_calls",
        )
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("hacé ambas cosas")])
        resp = await provider.generar_respuesta(req)

        self.assertEqual(resp.texto, "")
        self.assertEqual(len(resp.tool_calls), 2)
        self.assertEqual(resp.tool_calls[0].nombre, "leer_archivo")
        self.assertEqual(resp.tool_calls[0].argumentos, {"ruta": "/tmp/test.txt"})
        self.assertEqual(resp.tool_calls[1].nombre, "notificar_usuario")
        self.assertEqual(resp.tool_calls[1].argumentos, {"mensaje": "listo"})

    async def test_tool_call_con_argumentos_json_malformado_lanza_error(self):
        mock_client = mock.MagicMock()
        tc = MockGroqToolCall(
            id="call_err",
            name="funcion_rota",
            arguments='{"invalido": no_es_json',
        )
        mock_client.chat.completions.create.return_value = MockChatCompletion(
            content=None,
            tool_calls=[tc],
        )
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("ejecutá")])
        with self.assertRaises(LLMValidationError) as ctx:
            await provider.generar_respuesta(req)
        self.assertIn("no son un JSON válido", str(ctx.exception))
        self.assertIsNotNone(ctx.exception.__cause__)


class TestGroqProviderVision(unittest.IsolatedAsyncioTestCase):
    """5. Pruebas de análisis multimodal de visión y validación de imágenes."""

    async def test_analizar_vision_png(self):
        mock_client = mock.MagicMock()
        mock_client.chat.completions.create.return_value = MockChatCompletion(content="Veo una ventana azul.")
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        raw_bytes = b"\x89PNG\r\n\x1a\nfake_image_data"
        expected_b64 = base64.b64encode(raw_bytes).decode("utf-8")

        res = await provider.analizar_vision(
            imagen_bytes=raw_bytes,
            extension="png",
            pregunta="¿qué ves?",
        )

        self.assertEqual(res, "Veo una ventana azul.")
        _, kwargs = mock_client.chat.completions.create.call_args
        image_part = kwargs["messages"][0]["content"][1]
        self.assertEqual(image_part["image_url"]["url"], f"data:image/png;base64,{expected_b64}")

    async def test_analizar_vision_jpg_normaliza_a_jpeg(self):
        mock_client = mock.MagicMock()
        mock_client.chat.completions.create.return_value = MockChatCompletion(content="Foto jpg.")
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        raw_bytes = b"\xff\xd8\xfffake_jpg"
        expected_b64 = base64.b64encode(raw_bytes).decode("utf-8")

        await provider.analizar_vision(
            imagen_bytes=raw_bytes,
            extension=".jpg",
            pregunta="analizar",
        )

        _, kwargs = mock_client.chat.completions.create.call_args
        image_part = kwargs["messages"][0]["content"][1]
        self.assertEqual(image_part["image_url"]["url"], f"data:image/jpeg;base64,{expected_b64}")

    async def test_analizar_vision_jpeg(self):
        mock_client = mock.MagicMock()
        mock_client.chat.completions.create.return_value = MockChatCompletion(content="Foto jpeg.")
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        raw_bytes = b"\xff\xd8\xfffake_jpeg"
        expected_b64 = base64.b64encode(raw_bytes).decode("utf-8")

        await provider.analizar_vision(
            imagen_bytes=raw_bytes,
            extension="jpeg",
            pregunta="analizar",
        )

        _, kwargs = mock_client.chat.completions.create.call_args
        image_part = kwargs["messages"][0]["content"][1]
        self.assertEqual(image_part["image_url"]["url"], f"data:image/jpeg;base64,{expected_b64}")

    async def test_analizar_vision_extension_invalida_lanza_validation_error(self):
        mock_client = mock.MagicMock()
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        formatos_invalidos = ["gif", "bmp", "webp", "tiff", "svg", "exe", ""]
        for ext in formatos_invalidos:
            with self.subTest(ext=ext):
                with self.assertRaises(LLMValidationError) as ctx:
                    await provider.analizar_vision(b"bytes", ext, "¿qué es?")
                self.assertIn("no admitida", str(ctx.exception))
                self.assertIn("PNG, JPG, JPEG", str(ctx.exception))

    async def test_analizar_vision_parametros_historicos_fijados(self):
        mock_client = mock.MagicMock()
        mock_client.chat.completions.create.return_value = MockChatCompletion(content="OK")
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        await provider.analizar_vision(b"dummy", "png", "test")

        _, kwargs = mock_client.chat.completions.create.call_args
        self.assertEqual(kwargs["model"], "qwen/qwen3.6-27b")
        self.assertEqual(kwargs["temperature"], 0.3)
        self.assertEqual(kwargs["max_completion_tokens"], 300)
        self.assertEqual(kwargs["reasoning_effort"], "none")

    async def test_analizar_vision_limpieza_tags_think(self):
        mock_client = mock.MagicMock()
        mock_client.chat.completions.create.return_value = MockChatCompletion(
            content="<think>Razonando píxeles...</think>Tenés abierto Firefox."
        )
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        res = await provider.analizar_vision(b"dummy", "png", "test")
        self.assertEqual(res, "Tenés abierto Firefox.")

    async def test_analizar_vision_think_sin_cerrar_retorna_vacio(self):
        mock_client = mock.MagicMock()
        mock_client.chat.completions.create.return_value = MockChatCompletion(
            content="<think>Pensando sin cerrar porque se cortó el tiempo"
        )
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        res = await provider.analizar_vision(b"dummy", "png", "test")
        self.assertEqual(res, "")


class TestGroqProviderConcurrencia(unittest.IsolatedAsyncioTestCase):
    """6. Pruebas de ejecución en worker thread y comportamiento de cancelación."""

    async def test_ejecucion_en_worker_thread_no_bloqueante(self):
        loop_thread = threading.get_ident()
        worker_threads = []

        def fake_create(**kwargs):
            worker_threads.append(threading.get_ident())
            return MockChatCompletion(content="ok")

        mock_client = mock.MagicMock()
        mock_client.chat.completions.create.side_effect = fake_create
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("hilo")])
        resp = await provider.generar_respuesta(req)

        self.assertEqual(resp.texto, "ok")
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], loop_thread)

    async def test_cancelacion_corrutina_to_thread_comportamiento(self):
        """Demuestra que cancelar la corrutina en asyncio interrumpe la espera en el event loop,

        pero NO cancela la ejecución síncrona en el hilo secundario (que continúa hasta terminar).
        """
        thread_started = threading.Event()
        thread_completed = threading.Event()

        def slow_sync_call(**kwargs):
            thread_started.set()
            time.sleep(0.1)
            thread_completed.set()
            return MockChatCompletion(content="finalizado_en_hilo")

        mock_client = mock.MagicMock()
        mock_client.chat.completions.create.side_effect = slow_sync_call
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("cancelar")])
        task = asyncio.create_task(provider.generar_respuesta(req))

        # Esperar a que el worker thread arranque efectivamente
        await asyncio.to_thread(thread_started.wait, 2.0)
        self.assertTrue(thread_started.is_set())

        # Cancelar la tarea de asyncio
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        # La corrutina fue cancelada en el event loop, pero el worker thread continuó ejecutándose
        await asyncio.to_thread(thread_completed.wait, 2.0)
        self.assertTrue(thread_completed.is_set(), "El worker thread continuó su trabajo síncrono hasta completar")


class TestGroqProviderMapeoExcepciones(unittest.IsolatedAsyncioTestCase):
    """7. Pruebas de traducción de excepciones y preservación de causa (__cause__)."""

    def _crear_response_mock(self, status_code=400):
        req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        return httpx.Response(status_code=status_code, request=req)

    async def test_rate_limit_error_mapping_con_causa(self):
        mock_client = mock.MagicMock()
        mock_resp = self._crear_response_mock(status_code=429)
        orig_exc = groq.RateLimitError("Rate limit reached", response=mock_resp, body={"error": "rate_limit"})
        mock_client.chat.completions.create.side_effect = orig_exc
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("hi")])
        with self.assertRaises(LLMRateLimitError) as ctx:
            await provider.generar_respuesta(req)

        self.assertIs(ctx.exception.__cause__, orig_exc)
        self.assertEqual(ctx.exception.provider, "groq")

    async def test_api_timeout_error_mapping_con_causa(self):
        mock_client = mock.MagicMock()
        req_http = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        orig_exc = groq.APITimeoutError(request=req_http)
        mock_client.chat.completions.create.side_effect = orig_exc
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("hi")])
        with self.assertRaises(LLMTimeoutError) as ctx:
            await provider.generar_respuesta(req)

        self.assertIs(ctx.exception.__cause__, orig_exc)

    async def test_httpx_timeout_error_mapping_con_causa(self):
        mock_client = mock.MagicMock()
        orig_exc = httpx.TimeoutException("Read timeout")
        mock_client.chat.completions.create.side_effect = orig_exc
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("hi")])
        with self.assertRaises(LLMTimeoutError) as ctx:
            await provider.generar_respuesta(req)

        self.assertIs(ctx.exception.__cause__, orig_exc)

    async def test_api_connection_error_mapping_con_causa(self):
        mock_client = mock.MagicMock()
        req_http = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        orig_exc = groq.APIConnectionError(request=req_http)
        mock_client.chat.completions.create.side_effect = orig_exc
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("hi")])
        with self.assertRaises(LLMConnectionError) as ctx:
            await provider.generar_respuesta(req)

        self.assertIs(ctx.exception.__cause__, orig_exc)

    async def test_httpx_connect_error_mapping_con_causa(self):
        mock_client = mock.MagicMock()
        orig_exc = httpx.ConnectError("Failed to connect")
        mock_client.chat.completions.create.side_effect = orig_exc
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("hi")])
        with self.assertRaises(LLMConnectionError) as ctx:
            await provider.generar_respuesta(req)

        self.assertIs(ctx.exception.__cause__, orig_exc)

    async def test_authentication_error_mapping_con_causa(self):
        mock_client = mock.MagicMock()
        mock_resp = self._crear_response_mock(status_code=401)
        orig_exc = groq.AuthenticationError("Invalid API key", response=mock_resp, body={"error": "auth"})
        mock_client.chat.completions.create.side_effect = orig_exc
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("hi")])
        with self.assertRaises(LLMConfigurationError) as ctx:
            await provider.generar_respuesta(req)

        self.assertIs(ctx.exception.__cause__, orig_exc)
        self.assertEqual(ctx.exception.provider, "groq")

    async def test_api_generic_error_mapping_con_causa(self):
        mock_client = mock.MagicMock()
        mock_resp = self._crear_response_mock(status_code=500)
        orig_exc = groq.APIError("Internal server error", request=mock_resp.request, body={"error": "server_error"})
        mock_client.chat.completions.create.side_effect = orig_exc
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("hi")])
        with self.assertRaises(LLMProviderError) as ctx:
            await provider.generar_respuesta(req)

        self.assertIs(ctx.exception.__cause__, orig_exc)

    async def test_httpx_http_error_mapping_con_causa(self):
        mock_client = mock.MagicMock()
        orig_exc = httpx.HTTPError("Generic HTTP failure")
        mock_client.chat.completions.create.side_effect = orig_exc
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("hi")])
        with self.assertRaises(LLMProviderError) as ctx:
            await provider.generar_respuesta(req)

        self.assertIs(ctx.exception.__cause__, orig_exc)

    async def test_unexpected_exception_mapping_con_causa(self):
        mock_client = mock.MagicMock()
        orig_exc = RuntimeError("Algo falló inesperadamente")
        mock_client.chat.completions.create.side_effect = orig_exc
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        req = LLMRequest(mensajes=[LLMMessage.user("hi")])
        with self.assertRaises(LLMProviderError) as ctx:
            await provider.generar_respuesta(req)

        self.assertIs(ctx.exception.__cause__, orig_exc)

    async def test_vision_mapeo_excepciones_con_causa(self):
        mock_client = mock.MagicMock()
        mock_resp = self._crear_response_mock(status_code=429)
        orig_exc = groq.RateLimitError("Rate limit in vision", response=mock_resp, body={})
        mock_client.chat.completions.create.side_effect = orig_exc
        provider = GroqProvider(api_key="gsk_test", client=mock_client)

        with self.assertRaises(LLMRateLimitError) as ctx:
            await provider.analizar_vision(b"dummy", "png", "¿qué ves?")

        self.assertIs(ctx.exception.__cause__, orig_exc)
