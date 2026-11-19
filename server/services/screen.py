"""Servicio para capturas de pantalla de Wayland (grim), compresión y bandeja temporal."""

import os
import re
import subprocess
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from resultado import EstadoResultado, Resultado

# Capturas pedidas desde el PC (voz o atajo de teclado): quedan en esta bandeja
# hasta que la app del celular las descarga.
CARPETA_BANDEJA: str = "/tmp/madeline_bandeja"
os.makedirs(CARPETA_BANDEJA, exist_ok=True)
HORAS_VIDA_BANDEJA: int = 24
PATRON_ID_CAPTURA = re.compile(r"^[0-9a-f]{12}$")


def ruta_captura_disponible(captura_id: str) -> Optional[str]:
    """Devuelve la ruta en el sistema de la captura con ese ID, priorizando JPEG."""
    ruta_jpg = f"/tmp/captura_madeline_{captura_id}.jpg"
    ruta_png = f"/tmp/captura_madeline_{captura_id}.png"

    if os.path.exists(ruta_jpg):
        return ruta_jpg
    if os.path.exists(ruta_png):
        return ruta_png
    return None


def tomar_captura(
    ancho_max: int = 1920,
    *,
    _subprocess=None,
    _resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> Resultado:
    """Saca la captura usando grim en Hyprland y la comprime a JPEG.

    Cada captura se guarda con un ID único (uuid) en vez de un nombre
    fijo, para que dos solicitudes simultáneas (PC + teléfono al
    mismo tiempo) no se pisen la captura una a la otra. El ID viaja
    codificado en el resultado como "CAPTURA_LISTA:<id>".

    Permite inyección opcional de `_subprocess` y `_resolver` para
    pruebas unitarias aisladas y compatibilidad de monkeypatching.
    """
    subp = _subprocess or subprocess
    resolver = _resolver or ruta_captura_disponible

    captura_id = uuid.uuid4().hex[:12]
    ruta_png = f"/tmp/captura_madeline_{captura_id}.png"

    try:
        subp.run(
            ["grim", ruta_png],
            check=True,
            timeout=10,
        )

        try:
            from PIL import Image

            ruta_jpg = f"/tmp/captura_madeline_{captura_id}.jpg"

            with Image.open(ruta_png) as img:
                img = img.convert("RGB")

                if img.width > ancho_max:
                    ratio = ancho_max / img.width
                    img = img.resize((ancho_max, int(img.height * ratio)))

                img.save(
                    ruta_jpg,
                    "JPEG",
                    quality=85,
                    optimize=True,
                )

            os.remove(ruta_png)

        except Exception as e:
            print(
                f"[!] No se pudo comprimir la captura, "
                f"se enviará el PNG original: {e}"
            )

        ruta_final = resolver(captura_id)
        if not ruta_final or not os.path.exists(ruta_final) or os.path.getsize(ruta_final) == 0:
            return Resultado(
                ok=False,
                mensaje="Falló el módulo de captura de pantalla: el archivo no se generó o quedó vacío.",
                evidencia={"captura_id": captura_id, "verificado": False},
                estado=EstadoResultado.FAILURE,
            )

        return Resultado(
            ok=True,
            mensaje=f"CAPTURA_LISTA:{captura_id}",
            evidencia={
                "captura_id": captura_id,
                "ruta": ruta_final,
                "tamano_bytes": os.path.getsize(ruta_final),
                "verificado": True,
            },
            estado=EstadoResultado.SUCCESS,
        )

    except Exception as e:
        return Resultado(
            ok=False,
            mensaje=f"Falló el módulo de captura de pantalla: {e}",
            evidencia={"error": str(e), "verificado": False},
            estado=EstadoResultado.FAILURE,
        )


def enviar_captura_al_telefono(
    captura_id: str,
    *,
    _resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Deja la captura en la bandeja para que la app móvil la descargue."""
    resolver = _resolver or ruta_captura_disponible
    ruta = resolver(captura_id)
    if not ruta:
        return None
    destino = os.path.join(CARPETA_BANDEJA, captura_id + os.path.splitext(ruta)[1])
    os.replace(ruta, destino)
    print(f"[*] Captura {captura_id} en la bandeja del celular.")
    return destino


def capturas_en_bandeja() -> List[Dict[str, Any]]:
    """Devuelve las capturas pendientes en la bandeja, ordenadas cronológicamente."""
    archivos = []
    if not os.path.exists(CARPETA_BANDEJA):
        return archivos

    for nombre in os.listdir(CARPETA_BANDEJA):
        captura_id, _ = os.path.splitext(nombre)
        if PATRON_ID_CAPTURA.match(captura_id):
            ruta = os.path.join(CARPETA_BANDEJA, nombre)
            try:
                archivos.append({
                    "id": captura_id,
                    "creada": os.path.getmtime(ruta),
                    "ruta": ruta,
                })
            except OSError:
                pass
    return sorted(archivos, key=lambda a: a["creada"])


def limpiar_capturas_viejas(minutos: int = 10, horas_bandeja: int = HORAS_VIDA_BANDEJA) -> int:
    """Borra capturas temporales en /tmp con más de `minutos` y en la bandeja con más de `horas_bandeja`."""
    eliminados = 0
    limite_tmp = time.time() - (minutos * 60)

    for n in os.listdir("/tmp"):
        if n.startswith("captura_madeline_"):
            ruta = os.path.join("/tmp", n)
            try:
                if os.path.getmtime(ruta) < limite_tmp:
                    os.remove(ruta)
                    eliminados += 1
            except OSError:
                pass

    limite_bandeja = time.time() - (horas_bandeja * 3600)
    for captura in capturas_en_bandeja():
        if captura["creada"] < limite_bandeja:
            try:
                os.remove(captura["ruta"])
                eliminados += 1
            except OSError:
                pass

    return eliminados
