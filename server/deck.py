"""Íconos y nombres de las apps para el deck del celular.

El deck es la pantalla de botones de la app (como un Stream Deck):
cada botón ejecuta una acción directa en el PC, sin pasar por Groq.
Este módulo solo resuelve cómo se ve cada app: busca su .desktop
(el mismo que usa el lanzador), saca el nombre y el ícono del tema
de íconos, y lo deja como PNG de 128 px en data/iconos.
"""
import glob
import os
import re
import subprocess

TAMANO_ICONO = 128

CARPETA_ICONOS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "iconos"
)

_HOME = os.path.expanduser("~")
CARPETAS_DESKTOP = [
    f"{_HOME}/.local/share/applications",
    "/usr/share/applications",
    "/var/lib/flatpak/exports/share/applications",
    f"{_HOME}/.local/share/flatpak/exports/share/applications",
]

# El tema del usuario (kora) primero, así los botones se ven igual
# que en su escritorio; hicolor tiene los íconos de Steam y flatpak.
CARPETAS_ICONOS = [
    f"{_HOME}/.local/share/icons",
    "/usr/share/icons/kora",
    f"{_HOME}/.local/share/icons/hicolor",
    "/usr/share/icons/hicolor",
    "/var/lib/flatpak/exports/share/icons/hicolor",
    f"{_HOME}/.local/share/flatpak/exports/share/icons/hicolor",
    "/usr/share/pixmaps",
]

# Nombres en español o más conocidos que el Name del .desktop.
NOMBRES = {
    "vesktop": "Discord",
    "zen-browser": "Zen",
    "nautilus": "Archivos",
    "gnome-calendar": "Calendario",
    "gnome-text-editor": "Editor de texto",
    "qalculate-gtk": "Calculadora",
    "pavucontrol": "Volumen",
    "blueman-manager": "Bluetooth",
    "nvidia-settings": "NVIDIA",
    "nwg-displays": "Pantallas",
    "btop": "btop",
    "kitty": "Terminal",
    "virt-manager": "Máquinas virtuales",
    "libreoffice": "LibreOffice",
    "libreoffice-writer": "Writer",
    "libreoffice-calc": "Calc",
    "libreoffice-impress": "Impress",
    "vlc": "VLC",
    "mpv": "mpv",
    "pokemmo": "PokeMMO",
    "lime3ds": "Lime3DS",
    "prismlauncher": "Minecraft",
    "sober": "Roblox",
    "code": "VS Code",
    "rocket_stats": "Rocket Stats",
    "heroic": "Heroic",
    "steam": "Steam",
}

# Cuando el .desktop no se encuentra solo (otro nombre de archivo).
DESKTOP_FORZADO = {
    "zen-browser": "zen",
    "libreoffice": "libreoffice-startcenter",
    "steam": "steam",
    "kitty": "kitty",
    "vlc": "vlc",
    "pokemmo": "com.pokemmo.PokeMMO",
    "lime3ds": "lime3ds",
    "rocket_stats": "dev.keric.rocket_stats",
}

_entradas = None
_indice_iconos = None


def _leer_desktop(ruta: str) -> dict:
    datos = {}
    seccion = None
    try:
        with open(ruta, encoding="utf-8", errors="ignore") as f:
            for linea in f:
                linea = linea.strip()
                if linea.startswith("["):
                    seccion = linea
                    continue
                if seccion != "[Desktop Entry]":
                    continue
                clave, _, valor = linea.partition("=")
                if clave in ("Name", "Exec", "Icon") and clave not in datos:
                    datos[clave] = valor
    except OSError:
        pass
    return datos


def entradas_desktop() -> dict:
    """{nombre del archivo sin .desktop: datos}, la primera carpeta gana."""
    global _entradas
    if _entradas is None:
        _entradas = {}
        for carpeta in CARPETAS_DESKTOP:
            for ruta in sorted(glob.glob(f"{carpeta}/*.desktop")):
                nombre = os.path.basename(ruta)[: -len(".desktop")]
                if nombre not in _entradas:
                    _entradas[nombre] = _leer_desktop(ruta)
    return _entradas


def _sin_codigos(exec_: str) -> str:
    """Quita los %U, %f... del Exec para compararlo con un comando."""
    return re.sub(r"\s*%\w", "", exec_).strip()


def buscar_desktop(tecnico: str, comando: str) -> dict | None:
    """El .desktop de una app: por nombre de archivo, por el appid de
    Steam o por el comando que ejecuta."""
    entradas = entradas_desktop()

    forzado = DESKTOP_FORZADO.get(tecnico)
    if forzado in entradas:
        return entradas[forzado]

    juego = re.search(r"rungameid/(\d+)", comando)
    if juego:
        for datos in entradas.values():
            if f"rungameid/{juego.group(1)}" in datos.get("Exec", ""):
                return datos
        return None

    flatpak = comando.split()[-1] if comando.startswith("flatpak run") else None
    for clave in (flatpak, tecnico):
        if clave and clave in entradas:
            return entradas[clave]

    candidatas = []
    binario = os.path.basename(comando.split()[0])
    for datos in entradas.values():
        exec_ = _sin_codigos(datos.get("Exec", ""))
        if not exec_:
            continue
        if exec_ == comando:
            return datos
        if os.path.basename(exec_.split()[0]) == binario:
            candidatas.append(datos)
    # Entre varias del mismo binario, la de Exec más corto es la
    # principal (no "kitty URL Launcher" ni "VLC (Blu-ray)").
    if candidatas:
        return min(candidatas, key=lambda d: len(d.get("Exec", "")))
    return None


def _indexar_iconos() -> dict:
    global _indice_iconos
    if _indice_iconos is None:
        _indice_iconos = {}
        for prioridad, carpeta in enumerate(CARPETAS_ICONOS):
            for raiz, _, archivos in os.walk(carpeta, followlinks=True):
                for archivo in archivos:
                    nombre, ext = os.path.splitext(archivo)
                    if ext not in (".svg", ".png", ".xpm"):
                        continue
                    _indice_iconos.setdefault(nombre, []).append(
                        (prioridad, os.path.join(raiz, archivo))
                    )
    return _indice_iconos


def _tamano_de(ruta: str) -> int:
    """Tamaño según la carpeta (48x48, 256x256@2...): más grande es mejor."""
    if ruta.endswith(".svg"):
        return 10_000
    m = re.search(r"/(\d+)x\d+", ruta)
    return int(m.group(1)) if m else 0


def archivo_de_icono(nombre_icono: str) -> str | None:
    if not nombre_icono:
        return None
    if os.path.isabs(nombre_icono):
        return nombre_icono if os.path.exists(nombre_icono) else None
    opciones = [
        (p, r) for p, r in _indexar_iconos().get(nombre_icono, [])
        if "/symbolic/" not in r and os.path.exists(r)
    ]
    if not opciones:
        return None
    # Mismo tema primero; dentro del tema, el más grande.
    return min(opciones, key=lambda o: (o[0], -_tamano_de(o[1])))[1]


def _nombre_cache(tecnico: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", tecnico.lower()) + ".png"


def icono_png(tecnico: str, comando: str) -> str | None:
    """Ruta a un PNG de 128 px con el ícono de la app (en caché)."""
    destino = os.path.join(CARPETA_ICONOS, _nombre_cache(tecnico))
    if os.path.exists(destino):
        return destino

    datos = buscar_desktop(tecnico, comando) or {}
    origen = archivo_de_icono(datos.get("Icon")) or archivo_de_icono(tecnico)
    if not origen:
        return None

    os.makedirs(CARPETA_ICONOS, exist_ok=True)
    lado = str(TAMANO_ICONO)
    if origen.endswith(".svg"):
        comando_conversion = ["rsvg-convert", "-w", lado, "-h", lado,
                              "--keep-aspect-ratio", "-o", destino, origen]
    else:
        comando_conversion = ["magick", origen, "-background", "none",
                              "-resize", f"{lado}x{lado}", destino]
    try:
        subprocess.run(comando_conversion, check=True, timeout=10,
                       capture_output=True)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"[!] No pude convertir el ícono de {tecnico}: {e}")
        return None
    return destino if os.path.exists(destino) else None


def nombre_visible(tecnico: str, comando: str, alias: str) -> str:
    if tecnico in NOMBRES:
        return NOMBRES[tecnico]
    datos = buscar_desktop(tecnico, comando) or {}
    nombre = datos.get("Name", "").replace("®", "").replace("™", "").strip()
    return nombre or alias.title()


def catalogo(diccionario_apps: dict, comandos_apertura: dict) -> list:
    """Una entrada por app (no por alias), ordenadas por nombre."""
    primer_alias = {}
    for alias, tecnico in diccionario_apps.items():
        primer_alias.setdefault(tecnico, alias)

    apps = []
    for tecnico, alias in primer_alias.items():
        comando = comandos_apertura.get(tecnico, tecnico)
        apps.append({
            "id": tecnico,
            "nombre": nombre_visible(tecnico, comando, alias),
        })
    return sorted(apps, key=lambda a: a["nombre"].lower())
