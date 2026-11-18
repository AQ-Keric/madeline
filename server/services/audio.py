"""Servicio de control de audio del sistema (pamixer) y reproducción multimedia (playerctl)."""

import subprocess
import time
from typing import Callable, Optional


def ajustar_volumen(accion: str, porcentaje: int = 10, aplicacion: str = None) -> str:
    """Ajusta el volumen del sistema o de una aplicación específica."""
    try:
        if accion == "mutear":
            if aplicacion:
                subprocess.run(
                    ["pamixer", "--stream", "--toggle-mute",
                     "--sink-input", aplicacion],
                    check=True,
                )
                return f"Se cambió el mute de la aplicación: {aplicacion}."
            else:
                subprocess.run(["pamixer", "-t"], check=True)
                return "Volumen general muteado o desmuteado."

        elif accion == "setear":
            if aplicacion:
                subprocess.run(
                    ["pamixer", "--stream", "--set-volume",
                     str(porcentaje), "--sink-input", aplicacion],
                    check=True,
                )
                return f"Volumen fijado exactamente al {porcentaje}% en {aplicacion}."
            else:
                subprocess.run(
                    ["pamixer", "--set-volume", str(porcentaje)],
                    check=True,
                )
                return f"Volumen general fijado exactamente al {porcentaje}%."

        elif accion == "subir":
            if aplicacion:
                subprocess.run(
                    ["pamixer", "--stream", "-i", str(porcentaje),
                     "--sink-input", aplicacion],
                    check=True,
                )
                return f"Volumen subido un {porcentaje}% en {aplicacion}."
            else:
                subprocess.run(["pamixer", "-i", str(porcentaje)], check=True)
                return f"Volumen general subido un {porcentaje}%."

        elif accion == "bajar":
            if aplicacion:
                subprocess.run(
                    ["pamixer", "--stream", "-d", str(porcentaje),
                     "--sink-input", aplicacion],
                    check=True,
                )
                return f"Volumen bajado un {porcentaje}% en {aplicacion}."
            else:
                subprocess.run(["pamixer", "-d", str(porcentaje)], check=True)
                return f"Volumen general bajado un {porcentaje}%."

        return "Acción no reconocida."

    except Exception as e:
        return f"Error ejecutando pamixer: {e}"


def hay_reproductor() -> bool:
    """Si hay algún reproductor (Spotify, YouTube en el navegador...)
    que playerctl pueda controlar."""
    resultado = subprocess.run(
        ["playerctl", "status"], capture_output=True, text=True
    )
    return resultado.returncode == 0


def info_cancion() -> str:
    """Obtiene el nombre de la canción y artista actual."""
    try:
        resultado = subprocess.check_output(
            [
                "playerctl",
                "metadata",
                "--format",
                "{{ artist }} - {{ title }}",
            ]
        ).decode("utf-8").strip()

        if resultado and resultado != "-":
            return f"Actualmente está sonando: {resultado}"

        return "Al parecer no hay ninguna canción reproduciéndose."

    except Exception as e:
        return f"Pucha, no pude leer la canción: {e}"


def control_multimedia(
    accion: str,
    hay_reproductor_fn: Optional[Callable[[], bool]] = None,
    abrir_radio_fn: Optional[Callable[[], str]] = None,
) -> str:
    """Controla la reproducción de música con soporte de callbacks opcionales."""
    acciones_validas = {
        "pausar": ["pause"],
        "reproducir": ["play"],
        "siguiente": ["next"],
        "anterior": ["previous"],
        "reiniciar": ["position", "0"],
    }

    comando = acciones_validas.get(accion.lower())

    if not comando:
        return f"Error: La acción '{accion}' no es válida. No se ejecutó nada."

    _hay = hay_reproductor_fn if hay_reproductor_fn is not None else hay_reproductor

    try:
        if accion.lower() == "anterior":
            subprocess.run(["playerctl", "previous"], check=True)
            time.sleep(0.1)
            subprocess.run(["playerctl", "previous"], check=True)
            return "Se saltó a la canción anterior con doble toque."

        if accion.lower() == "reproducir" and not _hay():
            if abrir_radio_fn is not None:
                return abrir_radio_fn()
            return "No hay ningún reproductor activo para reanudar."

        subprocess.run(["playerctl", *comando], check=True)
        return f"Se ejecutó la acción: {accion}."

    except Exception as e:
        return f"Pucha, no pude controlar la música: {e}"
