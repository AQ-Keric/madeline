"""Dibuja los sprites del avatar de Madeline (pixel art 32x32).

Inspirada en la Madeline de Celeste (el pelo largo al viento, el
mechón parado), con pelo morado, sudadera y audífonos con luz. Mira a
la derecha; para caminar a la izquierda el avatar se refleja en
shell.qml.

Se arma por capas (pelo, ropa, cara, audífonos) y al final se le pone
el contorno oscuro automáticamente, como en el juego. La luz de los
audífonos queda como un hoyo transparente: shell.qml la pinta por
debajo con el color que toque (arcoíris al hablar, parpadeo con
música). Dónde está ese hoyo en cada cuadro va en sprites/luz.json.

    python avatar/generar_sprites.py              # colores por defecto
    python avatar/generar_sprites.py menta        # otros colores
    python avatar/generar_sprites.py --muestra RUTA.png   # todos, para comparar
"""

import json
import os
import sys

from PIL import Image

TAM = 32
CARPETA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sprites")
# La app del celular usa los mismos cuadros (la Madeline de la cabecera).
CARPETA_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mobile", "assets", "avatar")

COMUNES = {
    "K": (26, 20, 35),       # contorno
    "S": (250, 214, 176),    # piel
    "s": (236, 160, 140),    # mejillas
    "E": (40, 26, 60),       # ojos
    "M": (190, 70, 100),     # boca
}

# Pelo morado oscuro, igual en todos los estilos.
PELO = {"H": (98, 46, 160), "h": (64, 26, 112), "L": (140, 86, 204)}
# Como en Celeste: después del dash el pelo queda azul hasta aterrizar.
PELO_AZUL = {"H": (70, 150, 230), "h": (40, 96, 180), "L": (150, 210, 255)}

# Colores de la sudadera (C/c/I y cordones D), el short (P/p), las
# medias (T), las zapatillas (B y suela W), los audífonos (A) y su luz
# en reposo (G).
ESTILOS = {
    "grafito": {
        "C": (70, 70, 84), "c": (48, 48, 60), "I": (98, 98, 116), "D": (236, 236, 240),
        "P": (40, 40, 52), "p": (28, 28, 38), "T": (30, 28, 40),
        "B": (236, 236, 240), "W": (150, 150, 165), "A": (236, 236, 244), "G": (170, 120, 255),
    },
    "menta": {
        "C": (70, 70, 84), "c": (48, 48, 60), "I": (98, 98, 116), "D": (120, 226, 190),
        "P": (40, 40, 52), "p": (28, 28, 38), "T": (30, 28, 40),
        "B": (120, 226, 190), "W": (236, 236, 240), "A": (40, 40, 52), "G": (120, 236, 200),
    },
    "noche": {
        "C": (40, 54, 98), "c": (28, 38, 72), "I": (64, 80, 130), "D": (250, 196, 80),
        "P": (34, 34, 46), "p": (24, 24, 34), "T": (30, 28, 40),
        "B": (236, 236, 240), "W": (250, 196, 80), "A": (236, 236, 244), "G": (250, 196, 80),
    },
    "lila": {
        "C": (176, 160, 214), "c": (140, 122, 184), "I": (206, 194, 236), "D": (60, 40, 96),
        "P": (40, 38, 56), "p": (28, 26, 40), "T": (66, 60, 88),
        "B": (40, 38, 56), "W": (236, 236, 240), "A": (40, 38, 56), "G": (190, 140, 255),
    },
    "bosque": {
        "C": (70, 100, 78), "c": (50, 74, 58), "I": (96, 130, 104), "D": (240, 230, 200),
        "P": (60, 50, 44), "p": (44, 36, 32), "T": (30, 28, 40),
        "B": (130, 84, 56), "W": (240, 230, 200), "A": (60, 50, 44), "G": (150, 230, 120),
    },
}
ESTILO_POR_DEFECTO = "lila"


class Lienzo:
    def __init__(self):
        self.px = {}

    def punto(self, x, y, c):
        if 0 <= x < TAM and 0 <= y < TAM:
            self.px[(x, y)] = c

    def rect(self, x0, y0, x1, y1, c):
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                self.punto(x, y, c)

    def elipse(self, cx, cy, rx, ry, c):
        for y in range(TAM):
            for x in range(TAM):
                if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1:
                    self.punto(x, y, c)

    def contorno(self):
        """Pinta de oscuro cada píxel vacío pegado al personaje."""
        nuevos = []
        for y in range(TAM):
            for x in range(TAM):
                if (x, y) in self.px:
                    continue
                if any((x + dx, y + dy) in self.px for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                    nuevos.append((x, y))
        for p in nuevos:
            self.px[p] = "K"

    def imagen(self, colores):
        img = Image.new("RGBA", (TAM, TAM), (0, 0, 0, 0))
        for (x, y), c in self.px.items():
            if c in colores:
                img.putpixel((x, y), colores[c] + (255,))
        return img


def pelo_largo(l, dy, viento):
    """Pelo liso que cae por la espalda hasta la cintura. viento: cuánto
    se va hacia atrás la punta (0 quieta, 1-2 caminando)."""
    anchos = (4, 5, 5, 5, 5, 5, 5, 4, 4, 4, 3, 3, 2, 2)
    for i, ancho in enumerate(anchos):
        y = 8 + i + dy
        corrimiento = (viento * i) // 8
        x1 = 13 - corrimiento
        x0 = x1 - ancho + 1
        l.rect(x0, y, x1, y, "H")
        l.punto(x0, y, "h")
        # Brillo liso que baja por el medio.
        if 2 <= i <= 9:
            l.punto(x1 - 2, y, "L")


def ropa(l, dy, brazo, pose_brazo="normal"):
    l.rect(14, 16 + dy, 20, 23 + dy, "C")
    l.punto(14, 16 + dy, "c")
    l.punto(20, 16 + dy, "c")
    l.rect(14, 23 + dy, 20, 23 + dy, "c")
    # Cordones de la sudadera.
    l.rect(19, 17 + dy, 19, 19 + dy, "D")
    l.rect(15, 16 + dy, 17, 16 + dy, "I")
    if pose_brazo == "arriba":
        # Estirado en diagonal hacia arriba y adelante (al caer).
        for x, y in ((19, 17), (20, 16), (20, 15), (21, 14), (21, 13), (22, 12)):
            l.rect(x, y + dy, x + 1, y + dy, "C")
        l.rect(23, 11 + dy, 24, 12 + dy, "S")
    elif pose_brazo == "adelante":
        # Horizontal hacia adelante (dash).
        l.rect(18, 18 + dy, 24, 19 + dy, "C")
        l.rect(18, 19 + dy, 24, 19 + dy, "c")
        l.rect(25, 18 + dy, 26, 19 + dy, "S")
    else:
        # Brazo delantero (se mece al caminar).
        bx = 16 + brazo
        l.rect(bx, 17 + dy, bx + 2, 22 + dy, "c")
        l.rect(bx, 17 + dy, bx + 1, 21 + dy, "C")
        l.rect(bx, 23 + dy, bx + 1, 23 + dy, "S")


def piernas(l, pos):
    """pos: ((dx, sube) pierna lejana, (dx, sube) pierna cercana) o
    'sentada'.

    Está casi de perfil (tres cuartos mirando a la derecha): la pierna
    cercana tapa a la lejana, que asoma 1 px más adelante y más oscura.
    Al caminar se cruzan como tijera en la dirección en que avanza; la
    rodilla va a medio camino (dx // 2), así la pierna sale inclinada
    desde la cadera y no abierta hacia los lados.
    """
    if pos == "sentada":
        l.rect(15, 25, 21, 26, "t")
        l.rect(14, 26, 21, 27, "T")
        l.rect(14, 25, 17, 26, "P")
        l.rect(22, 24, 23, 27, "B")
        l.rect(22, 27, 23, 27, "W")
        return
    # Piernas de 3 px bajo el centro del torso (x 14-20).
    for (dx, sube), x, (cpierna, czapato, csuela) in zip(
            pos, (17, 16), (("t", "b", "w"), ("T", "B", "W"))):
        rodilla = int(dx / 2)
        l.rect(x + rodilla, 26, x + 2 + rodilla, 26, cpierna)
        l.rect(x + dx, 27, x + 2 + dx, 28 - sube, cpierna)
        l.rect(x + dx, 29 - sube, x + 3 + dx, 30 - sube, czapato)
        l.rect(x + dx, 30 - sube, x + 3 + dx, 30 - sube, csuela)
    # Short.
    l.rect(14, 24, 20, 25, "P")
    l.rect(14, 25, 20, 25, "p")


def cabeza(l, dy, ojos, boca):
    # Pelo de arriba, con un brillo en diagonal.
    l.elipse(17, 9 + dy, 6.6, 5.6, "H")
    l.rect(14, 5 + dy, 16, 5 + dy, "L")
    l.rect(13, 6 + dy, 14, 6 + dy, "L")
    l.rect(11, 9 + dy, 11, 13 + dy, "h")
    # Mechón parado, como en el arte del juego.
    l.punto(18, 3 + dy, "H")
    l.punto(19, 2 + dy, "H")
    # Cara.
    l.rect(16, 10 + dy, 22, 14 + dy, "S")
    l.rect(17, 15 + dy, 21, 15 + dy, "S")
    # Flequillo y un mechón que cae junto a la cara.
    l.rect(16, 9 + dy, 23, 9 + dy, "H")
    for x in (16, 17, 19, 22):
        l.punto(x, 10 + dy, "H")
    l.rect(15, 10 + dy, 15, 15 + dy, "H")
    l.punto(15, 15 + dy, "h")
    # Mejillas.
    l.punto(17, 14 + dy, "s")
    l.punto(22, 14 + dy, "s")
    # Ojos.
    if ojos == "abiertos":
        for x in (18, 21):
            l.rect(x, 12 + dy, x, 13 + dy, "E")
    elif ojos == "arriba":
        for x in (18, 21):
            l.rect(x, 11 + dy, x, 12 + dy, "E")
    elif ojos == "cerrados":
        for x in (17, 18, 20, 21):
            l.punto(x, 13 + dy, "E")
    # Boca.
    if boca == "abierta":
        l.rect(19, 14 + dy, 20, 15 + dy, "M")
    elif boca == "sonrisa":
        l.punto(19, 15 + dy, "M")
        l.punto(20, 15 + dy, "M")
    # Audífonos: el arco por encima y el auricular sobre la oreja.
    # "*" es la luz: queda transparente y la pinta shell.qml.
    for x, y in ((13, 7), (13, 6), (14, 5), (15, 4), (16, 3), (17, 3),
                 (20, 3), (21, 4), (22, 5)):
        l.punto(x, y + dy, "A")
    l.rect(12, 8 + dy, 16, 13 + dy, "K")
    l.rect(13, 9 + dy, 15, 12 + dy, "A")
    l.rect(14, 10 + dy, 14, 11 + dy, "*")


def dibujar(estilo, *, dy=0, viento=0, brazo=0, piernas_pos=((0, 0), (0, 0)),
            ojos="abiertos", boca="", con_luz=False, pose_brazo="normal", azul=False):
    """Devuelve (imagen, [x, y, ancho, alto] de la luz)."""
    l = Lienzo()
    alto = dy + (3 if piernas_pos == "sentada" else 0)
    pelo_largo(l, alto, viento)
    ropa(l, alto, brazo, pose_brazo)
    piernas(l, piernas_pos)
    cabeza(l, alto, ojos, boca)
    if pose_brazo != "normal":
        # El brazo va delante de la cara: se dibuja de nuevo encima.
        ropa_brazo = Lienzo()
        ropa(ropa_brazo, alto, brazo, pose_brazo)
        for (x, y), c in ropa_brazo.px.items():
            if c in ("C", "c", "S") and not (14 <= x <= 20 and 16 + alto <= y <= 23 + alto):
                l.px[(x, y)] = c
    l.contorno()
    if piernas_pos == "sentada":
        # Sentada queda 3 px más baja: que apoye en el piso.
        l.px = {(x, y + 3): c for (x, y), c in l.px.items() if y + 3 < TAM}
    luz = [p for p, c in l.px.items() if c == "*"]
    xs, ys = [x for x, _ in luz], [y for _, y in luz]
    caja = [min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1]
    colores = {**COMUNES, **(PELO_AZUL if azul else PELO), **estilo}
    # La pierna lejana va en sombra.
    for clara, oscura in (("T", "t"), ("B", "b"), ("W", "w")):
        colores[oscura] = tuple(int(v * 0.68) for v in colores[clara])
    if con_luz:
        colores["*"] = estilo["G"]
    return l.imagen(colores), caja


def cuadros():
    """(nombre, argumentos) de cada cuadro de cada animación."""
    lista = [
        # Quieta: respira y pestañea en el 3er cuadro.
        ("quieta_0", {}),
        ("quieta_1", {"dy": 1}),
        ("quieta_2", {"ojos": "cerrados"}),
        # Hablando: abre y cierra la boca.
        ("hablar_0", {"boca": "abierta"}),
        ("hablar_1", {"boca": "sonrisa"}),
        # Pensando: mira hacia arriba.
        ("pensar_0", {"ojos": "arriba"}),
        ("pensar_1", {"ojos": "arriba", "dy": 1}),
        # Escuchando: atenta.
        ("escuchar_0", {"boca": "sonrisa"}),
        ("escuchar_1", {"boca": "sonrisa"}),
        # Durmiendo: sentada con los ojos cerrados.
        ("dormir_0", {"piernas_pos": "sentada", "ojos": "cerrados"}),
        ("dormir_1", {"piernas_pos": "sentada", "ojos": "cerrados", "dy": 1}),
    ]
    # Caminar: 4 cuadros, el pelo flamea y los brazos se mecen.
    # De perfil: apoyo (una pierna adelante y la otra atrás), y en el
    # cuadro intermedio el pie que pasa adelante se levanta y el cuerpo
    # sube. (lejana, cercana), brazo, dy.
    pasos = [
        (((-2, 0), (2, 0)), -1, 0),
        (((0, 2), (0, 0)), 0, -1),
        (((2, 0), (-2, 0)), 1, 0),
        (((0, 0), (0, 2)), 0, -1),
    ]
    for i, (pos, brazo, dy) in enumerate(pasos):
        lista.append((f"caminar_{i}", {"dy": dy, "viento": 2 if i % 2 else 1,
                                       "brazo": brazo, "piernas_pos": pos}))
    lista += [
        # Saltar (impulso): piernas recogidas.
        ("saltar_0", {"piernas_pos": ((0, 2), (1, 2)), "dy": -1}),
        # Cayendo: brazo arriba y piernas sueltas.
        ("caer_0", {"pose_brazo": "arriba", "piernas_pos": ((-1, 0), (1, 1)), "viento": 1}),
        ("caer_azul_0", {"pose_brazo": "arriba", "piernas_pos": ((-1, 0), (1, 1)),
                         "viento": 1, "azul": True}),
        # Dash: pelo azul al viento, brazo adelante y piernas atrás.
        ("dash_0", {"pose_brazo": "adelante", "piernas_pos": ((-3, 1), (-2, 0)),
                    "viento": 3, "azul": True}),
    ]
    return lista


def generar(nombre_estilo, carpeta=None):
    estilo = ESTILOS[nombre_estilo]
    carpeta = carpeta or CARPETA
    os.makedirs(carpeta, exist_ok=True)
    cajas = {}
    for nombre, args in cuadros():
        img, cajas[nombre] = dibujar(estilo, **args)
        img.save(os.path.join(carpeta, nombre + ".png"))
        if nombre == "dash_0":
            # Silueta celeste sólida para la estela del dash.
            silueta = Image.new("RGBA", img.size, (0, 0, 0, 0))
            for y in range(TAM):
                for x in range(TAM):
                    if img.getpixel((x, y))[3]:
                        silueta.putpixel((x, y), (120, 220, 255, 255))
            silueta.save(os.path.join(carpeta, "dash_silueta.png"))
    hexa = "#{:02x}{:02x}{:02x}".format
    with open(os.path.join(carpeta, "luz.json"), "w", encoding="utf-8") as f:
        json.dump({"reposo": hexa(*estilo["G"]), "apagada": hexa(*estilo["A"]),
                   "cuadros": cajas}, f, indent=1)


def muestra(ruta, escala=8):
    """Una fila por estilo con algunos cuadros, para elegir."""
    nombres = ("quieta_0", "caminar_1", "caminar_3", "hablar_0", "dormir_0")
    args = dict(cuadros())
    lado = TAM * escala
    hoja = Image.new("RGBA", (lado * len(nombres), lado * len(ESTILOS)), (236, 236, 242, 255))
    for fila, estilo in enumerate(ESTILOS.values()):
        for col, nombre in enumerate(nombres):
            img = dibujar(estilo, **args[nombre], con_luz=True)[0].resize((lado, lado), Image.NEAREST)
            hoja.paste(img, (col * lado, fila * lado), img)
    hoja.save(ruta)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--muestra":
        muestra(sys.argv[2])
    else:
        estilo = sys.argv[1] if len(sys.argv) > 1 else ESTILO_POR_DEFECTO
        generar(estilo)
        if os.path.isdir(os.path.dirname(CARPETA_APP)):
            generar(estilo, CARPETA_APP)
