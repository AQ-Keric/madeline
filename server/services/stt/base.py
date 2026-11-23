"""Contrato agnóstico y validaciones para proveedores de Speech-To-Text (STT).

Define la interfaz abstracta `STTProvider` y validaciones estrictas de entrada
independientes de cualquier proveedor o SDK propietario.
"""

from abc import ABC, abstractmethod
import os
from typing import Optional

from .exceptions import STTValidationError

EXTENSIONES_AUDIO_PERMITIDAS: frozenset[str] = frozenset({
    "wav",
    "mp3",
    "m4a",
    "ogg",
    "flac",
    "webm",
    "mp4",
    "mpeg",
    "mpga",
    "opus",
    "aac",
})


def validar_parametros_transcripcion(
    audio_bytes: bytes,
    filename: str,
    language: str = "es",
    prompt: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    """Valida los parámetros de entrada para una solicitud de transcripción.

    Raises:
        STTValidationError: Si alguna de las entradas no cumple con el contrato.
    """
    # 1. Validación de audio_bytes
    if not isinstance(audio_bytes, (bytes, bytearray)):
        raise STTValidationError(
            f"El audio debe ser bytes o bytearray, recibido: {type(audio_bytes).__name__}."
        )

    if len(audio_bytes) == 0:
        raise STTValidationError("El contenido de audio no puede estar vacío (0 bytes).")

    # 2. Validación de filename
    if not isinstance(filename, str):
        raise STTValidationError(
            f"El nombre de archivo ('filename') debe ser str, recibido: {type(filename).__name__}."
        )

    filename_limpio = filename.strip()
    if not filename_limpio:
        raise STTValidationError("El nombre de archivo ('filename') no puede estar vacío.")

    if "\0" in filename_limpio:
        raise STTValidationError("El nombre de archivo contiene caracteres nulos inválidos.")

    # Extracción y validación de extensión
    _, ext = os.path.splitext(filename_limpio.lower())
    ext_sin_punto = ext.lstrip(".")

    if not ext_sin_punto or ext_sin_punto not in EXTENSIONES_AUDIO_PERMITIDAS:
        formatos_validos = sorted(list(EXTENSIONES_AUDIO_PERMITIDAS))
        raise STTValidationError(
            f"Extensión de archivo '{ext}' no soportada o ausente en '{filename}'. "
            f"Formatos permitidos: {formatos_validos}."
        )

    # 3. Validación de language
    if not isinstance(language, str) or not language.strip():
        raise STTValidationError(
            f"El parámetro de idioma ('language') debe ser una cadena no vacía, recibido: {language!r}."
        )

    # 4. Validación de prompt opcional
    if prompt is not None and not isinstance(prompt, str):
        raise STTValidationError(
            f"El prompt contextual debe ser una cadena o None, recibido: {type(prompt).__name__}."
        )

    # 5. Validación de model opcional
    if model is not None:
        if not isinstance(model, str):
            raise STTValidationError(
                f"El identificador del modelo ('model') debe ser str o None, recibido: {type(model).__name__}."
            )
        if not model.strip():
            raise STTValidationError(
                "El identificador del modelo ('model') no puede ser una cadena vacía."
            )
        if model != model.strip():
            raise STTValidationError(
                f"El identificador del modelo ('{model}') no debe contener espacios al inicio o al final."
            )


class STTProvider(ABC):
    """Contrato abstracto e independiente para proveedores de Speech-To-Text (STT)."""

    @property
    @abstractmethod
    def nombre(self) -> str:
        """Nombre identificador único del proveedor (ej. 'groq')."""

    @abstractmethod
    async def transcribir(
        self,
        audio_bytes: bytes,
        filename: str,
        language: str = "es",
        prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """Transcribe un fragmento de audio a texto de forma asíncrona.

        Args:
            audio_bytes: Contenido binario del audio a transcribir.
            filename: Nombre del archivo de audio con extensión válida (ej. 'audio.wav').
            language: Código de idioma ISO (por defecto 'es').
            prompt: Pista contextual o vocabulario especializado para guiar la transcripción.
            model: Identificador opcional del modelo específico del proveedor.

        Returns:
            Texto transcrito normalizado en formato de cadena (str).

        Raises:
            STTValidationError: Si los parámetros o el audio no son válidos.
            STTRateLimitError: Si se excede la cuota del proveedor.
            STTTimeoutError: Si se agota el tiempo de espera.
            STTConfigurationError: Si faltan credenciales o configuración.
            STTProviderError: Si ocurre un fallo en el proveedor o respuesta malformada.
        """
