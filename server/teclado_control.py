"""Control del Teclado RGB (Pegasum KAI y perfiles) para Madeline.

Permite consultar el estado, los modelos/perfiles de teclado, la lista de
temas diseñados (46 temas de alto contraste) y aplicar iluminación
directamente desde el celular (apartado Deck).
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# Ruta del proyecto KAI RGB Studio
KAI_STUDIO_DIR = os.getenv("KAI_STUDIO_DIR", "/home/keric/Desarrollo/kai-rgb-studio")
if os.path.isdir(KAI_STUDIO_DIR) and KAI_STUDIO_DIR not in sys.path:
    sys.path.insert(0, KAI_STUDIO_DIR)

try:
    from backend.device import KaiKeyboard
    from backend.matrix import DESIGN_THEMES
except Exception:
    KaiKeyboard = None
    DESIGN_THEMES = {}

# Si no está disponible el backend de KAI Studio, cargar perfiles guardados de ~/.config/kai-rgb/profiles.json
if not DESIGN_THEMES:
    _RUTA_PERFILES_LOCAL = os.path.expanduser("~/.config/kai-rgb/profiles.json")
    if os.path.exists(_RUTA_PERFILES_LOCAL):
        try:
            with open(_RUTA_PERFILES_LOCAL, "r", encoding="utf-8") as _f:
                _perfiles = json.load(_f)
                for _p in _perfiles:
                    _pid = _p.get("id")
                    if _pid:
                        _color = _p.get("color", "#FF8DA1")
                        DESIGN_THEMES[_pid] = {
                            "name": _p.get("name", _pid),
                            "desc": _p.get("desc", ""),
                            "category": "perfil",
                            "icon": "✨",
                            "swatches": [[_p.get("name", _pid), _color]],
                        }
        except Exception:
            pass

CARPETA_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
ARCHIVO_CONFIG = os.path.join(CARPETA_DATA, "teclado_config.json")

MODELOS_DEFAULT = [
    {
        "id": "kai_65",
        "nombre": "Pegasum KAI 65%",
        "tipo": "Físico (USB / 2.4G)",
        "descripcion": "68 teclas · RGB por tecla programable",
        "icono": "keyboard",
    },
    {
        "id": "perfil_mmo",
        "nombre": "Perfil MMO / WoW",
        "tipo": "Perfil de Juego",
        "descripcion": "Enfoque en teclas de rotación y cooldowns",
        "icono": "sports_esports",
    },
    {
        "id": "perfil_noche",
        "nombre": "Perfil Noche / Escritura",
        "tipo": "Perfil de Confort",
        "descripcion": "Tonos cálidos y descanso visual",
        "icono": "nightlight_round",
    },
]

FAVORITOS_DEFAULT = [
    "wow_souso_frost",
    "cyberpunk_2077",
    "zelda_triforce_master",
    "sakura_snow",
    "miami_vice_matrix",
    "lava_fire_5",
    "split_horizon",
    "cyber_limon",
]

_kbd_instancia: Optional[Any] = None


def _get_keyboard() -> Optional[Any]:
    global _kbd_instancia
    if KaiKeyboard is None:
        return None
    if _kbd_instancia is None:
        _kbd_instancia = KaiKeyboard()
    return _kbd_instancia


def cargar_config() -> Dict[str, Any]:
    os.makedirs(CARPETA_DATA, exist_ok=True)
    if os.path.exists(ARCHIVO_CONFIG):
        try:
            with open(ARCHIVO_CONFIG, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "modelo_activo": "kai_65",
        "tema_activo": "wow_souso_frost",
        "brillo": 100,
        "favoritos": list(FAVORITOS_DEFAULT),
    }


def guardar_config(cfg: Dict[str, Any]) -> None:
    os.makedirs(CARPETA_DATA, exist_ok=True)
    try:
        with open(ARCHIVO_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] Error al guardar config de teclado: {e}")


def listar_modelos() -> List[Dict[str, Any]]:
    kbd = _get_keyboard()
    conectado = False
    dispositivos_info = []
    if kbd:
        try:
            devs = kbd.scan_devices()
            conectado = len(devs) > 0
            dispositivos_info = [
                {"nombre": d.name, "nodo": d.node, "inalambrico": d.is_wireless}
                for d in devs
            ]
        except Exception:
            pass

    cfg = cargar_config()
    modelo_activo = cfg.get("modelo_activo", "kai_65")

    modelos = []
    for m in MODELOS_DEFAULT:
        m_copy = dict(m)
        m_copy["activo"] = (m["id"] == modelo_activo)
        if m["id"] == "kai_65":
            m_copy["conectado"] = conectado
            m_copy["dispositivos"] = dispositivos_info
        else:
            m_copy["conectado"] = True
            m_copy["dispositivos"] = []
        modelos.append(m_copy)
    return modelos


def listar_temas() -> List[Dict[str, Any]]:
    temas = []
    for tid, info in DESIGN_THEMES.items():
        swatches = [
            {"nombre": s[0], "hex": s[1]}
            for s in info.get("swatches", [])
        ]
        preview_colores = [s[1] for s in info.get("swatches", [])]
        temas.append({
            "id": tid,
            "nombre": info.get("name", tid),
            "icono": info.get("icon", "✨"),
            "categoria": info.get("category", "multicolor"),
            "descripcion": info.get("desc", ""),
            "swatches": swatches,
            "preview_colores": preview_colores,
        })
    return temas


def obtener_estado() -> Dict[str, Any]:
    cfg = cargar_config()
    modelos = listar_modelos()
    temas = listar_temas()
    
    # Validar que los favoritos existan en DESIGN_THEMES
    valid_favs = [f for f in cfg.get("favoritos", []) if f in DESIGN_THEMES]
    if not valid_favs:
        valid_favs = list(FAVORITOS_DEFAULT)

    return {
        "modelos": modelos,
        "modelo_activo": cfg.get("modelo_activo", "kai_65"),
        "tema_activo": cfg.get("tema_activo", "wow_souso_frost"),
        "brillo": cfg.get("brillo", 100),
        "favoritos": valid_favs,
        "total_temas": len(temas),
    }


def cambiar_modelo(modelo_id: str) -> Tuple[bool, str]:
    cfg = cargar_config()
    cfg["modelo_activo"] = modelo_id
    guardar_config(cfg)
    return True, f"Teclado cambiado a {modelo_id}"


def _brillo_pct_to_hw(pct: int) -> int:
    """Convierte 0..100% al valor de hardware 0..4."""
    if pct <= 0:
        return 0
    if pct <= 25:
        return 1
    if pct <= 50:
        return 2
    if pct <= 75:
        return 3
    return 4


def aplicar_tema(
    tema_id: str,
    brillo: Optional[int] = None,
    modelo_id: Optional[str] = None
) -> Tuple[bool, str]:
    if tema_id not in DESIGN_THEMES:
        return False, f"El tema '{tema_id}' no existe."

    cfg = cargar_config()
    if brillo is not None:
        cfg["brillo"] = max(0, min(100, int(brillo)))
    if modelo_id:
        cfg["modelo_activo"] = modelo_id

    brillo_pct = cfg.get("brillo", 100)
    hw_brightness = _brillo_pct_to_hw(brillo_pct)

    kbd = _get_keyboard()
    if not kbd:
        cfg["tema_activo"] = tema_id
        guardar_config(cfg)
        tema_nombre = DESIGN_THEMES[tema_id].get("name", tema_id)
        return True, f"Tema '{tema_nombre}' guardado (controlador no disponible)."

    try:
        ok, msg = kbd.connect()
        if not ok:
            return False, f"No se pudo conectar al teclado: {msg}"

        success = kbd.apply_matrix_theme(theme_id=tema_id, brightness=hw_brightness)
        if success:
            cfg["tema_activo"] = tema_id
            guardar_config(cfg)
            tema_nombre = DESIGN_THEMES[tema_id].get("name", tema_id)
            return True, f"Tema '{tema_nombre}' aplicado con éxito."
        else:
            return False, f"Error del teclado: {kbd.last_error}"
    except Exception as e:
        return False, f"Error al enviar datos al teclado: {e}"


def ajustar_brillo(brillo: int) -> Tuple[bool, str]:
    cfg = cargar_config()
    tema_actual = cfg.get("tema_activo", "wow_souso_frost")
    return aplicar_tema(tema_actual, brillo=brillo)


def apagar_luces() -> Tuple[bool, str]:
    return ajustar_brillo(0)


def guardar_favoritos(nuevos_favoritos: List[str]) -> Tuple[bool, str]:
    cfg = cargar_config()
    cfg["favoritos"] = [f for f in nuevos_favoritos if f in DESIGN_THEMES]
    guardar_config(cfg)
    return True, "Favoritos actualizados."
