"""Genera el ícono de Madeline: la montaña de Celeste de noche con la
fresa como luna. Saca el ícono adaptativo de Android (fondo, frente y
monocromo), los PNG clásicos y el logo que usa la app.

Uso (desde mobile/):  ../.venv/bin/python tool/generar_icono.py
"""
import os
import random

from PIL import Image, ImageDraw, ImageFilter

S = 1024          # lienzo maestro
AA = 4            # supermuestreo para bordes suaves
RES = os.path.join(os.path.dirname(__file__), "..", "android", "app", "src", "main", "res")

CIELO_ARRIBA = (20, 12, 46)
CIELO_ABAJO = (74, 36, 104)
MONTE_ATRAS = (88, 60, 140)
MONTE_FRENTE = (140, 104, 200)
NIEVE = (246, 240, 255)
FRESA = (232, 64, 80)
FRESA_SOMBRA = (190, 40, 62)
HOJAS = (88, 196, 110)
SEMILLA = (255, 222, 150)


def lienzo(color=(0, 0, 0, 0)):
    return Image.new("RGBA", (S * AA, S * AA), color)


def p(x, y):
    """Coordenadas en 0..1 -> píxeles del lienzo supermuestreado."""
    return (x * S * AA, y * S * AA)


def fondo() -> Image.Image:
    img = lienzo()
    d = ImageDraw.Draw(img)
    alto = S * AA
    for y in range(alto):
        t = y / alto
        color = tuple(int(a + (b - a) * t) for a, b in zip(CIELO_ARRIBA, CIELO_ABAJO))
        d.line([(0, y), (alto, y)], fill=color + (255,))
    azar = random.Random(7)
    for _ in range(55):
        x, y = azar.random(), azar.random() * 0.55
        r = azar.choice([2, 3, 3, 4, 5]) * AA
        brillo = azar.randint(150, 255)
        d.ellipse([x * alto - r, y * alto - r, x * alto + r, y * alto + r], fill=(255, 255, 255, brillo))
    return img


def frente(color_unico=None) -> Image.Image:
    """Montañas + fresa, centradas en la zona segura del ícono
    adaptativo (el 66% central)."""
    img = lienzo()
    d = ImageDraw.Draw(img)
    c = lambda col: (color_unico or col) + (255,)

    # Montaña de atrás y la cumbre de Celeste, hasta el borde de abajo.
    d.polygon([p(0.06, 1.0), p(0.36, 0.47), p(0.50, 0.62), p(0.66, 1.0)], fill=c(MONTE_ATRAS))
    d.polygon([p(0.26, 1.0), p(0.57, 0.36), p(0.94, 1.0)], fill=c(MONTE_FRENTE))
    if not color_unico:
        # Ladera en sombra de la cumbre.
        d.polygon([p(0.57, 0.36), p(0.94, 1.0), p(0.70, 1.0), p(0.60, 0.55)], fill=(116, 84, 176, 255))
        # Nieve, con borde dentado.
        d.polygon([p(0.36, 0.47), p(0.415, 0.545), p(0.39, 0.53), p(0.365, 0.565),
                   p(0.34, 0.53), p(0.31, 0.55)], fill=c(NIEVE))
        d.polygon([p(0.57, 0.36), p(0.645, 0.48), p(0.61, 0.46), p(0.585, 0.505),
                   p(0.555, 0.465), p(0.525, 0.49), p(0.495, 0.475)], fill=c(NIEVE))
        # Colina oscura adelante, para dar profundidad.
        colina = [p(0.0, 0.86)]
        for i in range(21):
            x = i / 20
            colina.append(p(x, 0.84 + 0.035 * ((x * 7.3) % 1.0 - 0.5) - 0.04 * (1 - abs(x - 0.5) * 2)))
        colina += [p(1.0, 1.0), p(0.0, 1.0)]
        d.polygon(colina, fill=(44, 26, 74, 255))

    # Fresa (la "luna"): redonda arriba y en punta suave abajo.
    import math
    fx, fy, ax, ay = 0.70, 0.37, 0.072, 0.070
    contorno = []
    for i in range(120):
        t = 2 * math.pi * i / 120
        seno = math.sin(t)
        x = ax * math.cos(t) * (1 - 0.38 * max(0.0, seno))
        y = ay * seno * (1.30 if seno > 0 else 0.78)
        contorno.append(p(fx + x, fy + y))
    d.polygon(contorno, fill=c(FRESA))
    if not color_unico:
        # Brillo arriba a la izquierda y semillas.
        d.ellipse([p(fx - ax * 0.75, fy - ay * 0.6), p(fx - ax * 0.15, fy - ay * 0.05)],
                  fill=(255, 140, 150, 255))
        for sx, sy in [(-0.45, 0.25), (0.0, 0.05), (0.45, 0.25), (-0.22, 0.65), (0.22, 0.65),
                       (0.0, 1.0), (0.5, -0.25), (-0.05, -0.4)]:
            x, y, rs = fx + sx * ax, fy + sy * ay, ax * 0.085
            d.ellipse([p(x - rs, y - rs * 1.4), p(x + rs, y + rs * 1.4)], fill=SEMILLA + (255,))
    # Hojas en estrella + tallito.
    cy = fy - ay * 0.72
    for k in range(5):
        t = math.pi + math.pi * k / 4
        punta = p(fx + math.cos(t) * ax * 0.85, cy + math.sin(t) * ay * 0.5 - ay * 0.05)
        izq = p(fx + math.cos(t - 0.35) * ax * 0.3, cy + math.sin(t - 0.35) * ay * 0.18)
        der = p(fx + math.cos(t + 0.35) * ax * 0.3, cy + math.sin(t + 0.35) * ay * 0.18)
        d.polygon([izq, punta, der, p(fx, cy + ay * 0.12)], fill=c(HOJAS))
    d.line([p(fx, cy), p(fx + ax * 0.12, cy - ay * 0.45)], fill=c(HOJAS), width=int(S * AA * 0.012))
    return img


def reducir(img: Image.Image, lado: int) -> Image.Image:
    return img.resize((lado, lado), Image.LANCZOS)


def guardar(img, ruta):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    img.save(ruta)


def main():
    f, fr = fondo(), frente()
    completo = Image.alpha_composite(f, fr)

    # Ícono adaptativo: 108dp por capa.
    for carpeta, lado in {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}.items():
        guardar(reducir(f, lado), f"{RES}/mipmap-{carpeta}/ic_launcher_background.png")
        guardar(reducir(fr, lado), f"{RES}/mipmap-{carpeta}/ic_launcher_foreground.png")
        guardar(reducir(frente((255, 255, 255)), lado), f"{RES}/mipmap-{carpeta}/ic_launcher_monochrome.png")

    # Ícono clásico (Android < 8): recortado al centro y redondeado.
    centro = completo.crop((int(S * AA * 0.12),) * 2 + (int(S * AA * 0.88),) * 2)
    mascara = Image.new("L", centro.size, 0)
    ImageDraw.Draw(mascara).rounded_rectangle([0, 0, *centro.size], radius=centro.size[0] // 5, fill=255)
    clasico = Image.new("RGBA", centro.size, (0, 0, 0, 0))
    clasico.paste(centro, mask=mascara)
    for carpeta, lado in {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}.items():
        guardar(reducir(clasico, lado), f"{RES}/mipmap-{carpeta}/ic_launcher.png")

    # Logo circular para dentro de la app.
    mascara = Image.new("L", centro.size, 0)
    ImageDraw.Draw(mascara).ellipse([0, 0, *centro.size], fill=255)
    logo = Image.new("RGBA", centro.size, (0, 0, 0, 0))
    logo.paste(centro, mask=mascara)
    guardar(reducir(logo, 512), os.path.join(os.path.dirname(__file__), "..", "assets", "logo.png"))
    guardar(reducir(completo, 512), os.path.join(os.path.dirname(__file__), "icono_vista_previa.png"))


if __name__ == "__main__":
    main()
