"""Batería de frases de música: qué acción debe salir de cada una.

La usa test_multimedia.py. Separada para poder agregar frases nuevas
fácilmente cuando alguna no la tome.
"""

ESPERADO = {
    ("multimedia", "reproducir"): [
        "ponle play", "ponle play a la canción", "ponle play a la música", "dale play",
        "dale play a la música", "dale al play", "play", "reanuda", "reanuda la canción",
        "continúa la música", "sigue con la música", "quita la pausa", "quítale la pausa",
        "despausa", "despausa la música", "ponle play de nuevo", "vuelve a poner la música",
        "pon la música otra vez", "reproduce la canción", "dale play otra vez",
        "ponle play al video", "play a la música", "resume la música",
    ],
    ("multimedia", "pausar"): [
        "pausa", "pausa la música", "ponle pausa", "pon pausa", "pausa la canción",
        "detén la música", "para la música", "para la canción", "pausa el video",
        "ponle stop", "stop", "ponle pausa a la música", "deten la música",
        "pausa un momento", "pausa la música un rato", "apaga la música", "dale pausa",
    ],
    ("multimedia", "siguiente"): [
        "siguiente", "siguiente canción", "pasa la canción", "pasa a la siguiente",
        "cambia la canción", "cambia de canción", "otra canción", "salta la canción",
        "sáltate esta canción", "salta esta", "pon la siguiente", "la que sigue", "next",
        "pasa esta canción", "skip", "la siguiente", "cambia el tema", "pon otra",
    ],
    ("multimedia", "anterior"): [
        "anterior", "canción anterior", "la anterior", "pon la anterior",
        "vuelve a la anterior", "regresa a la canción anterior", "pon la canción anterior",
        "la canción de antes", "vuelve a la canción anterior",
    ],
    ("multimedia", "reiniciar"): [
        "reinicia la canción", "pon la canción desde el principio", "desde el principio",
        "empieza la canción de nuevo", "repite la canción", "otra vez la canción",
        "pon esa canción de nuevo", "pon esta canción otra vez", "ponme esa canción de nuevo",
    ],
    ("info_cancion", None): [
        "qué canción es esta", "qué canción es", "qué canción está sonando",
        "cómo se llama esta canción", "cómo se llama la canción", "qué está sonando",
        "qué suena", "quién canta esto", "quién canta esta canción",
        "de quién es esta canción", "qué tema es este", "cuál es esta canción",
        "qué estoy escuchando", "qué canción es la que suena", "qué canción suena",
        "cómo se llama este tema", "qué música es esta", "quién es el artista",
        "de quién es esta", "qué canción estoy escuchando", "dime qué canción es",
        "dime el nombre de la canción", "cuál es el nombre de esta canción",
    ],
}

# Frases que NO deben disparar ninguna orden de música (van a Groq u
# otra cosa).
NO_MUSICA = [
    "qué es una pausa activa", "cuál es el siguiente feriado", "para qué sirve esto",
    "qué canción me recomiendas", "recomiéndame una canción de rock",
    "cuál es la mejor canción de queen", "quién canta bohemian rhapsody",
    "otra vez lo mismo", "cambia el fondo de pantalla", "pasa algo con el wifi",
    "la anterior presidenta de chile", "cómo se llama la capital de francia",
    "qué tema vimos ayer en clase", "explícame el tema de las derivadas",
]

# Cómo llega el texto: tal cual, con la palabra de activación como la
# escribe Whisper y con puntuación.
PREFIJOS = ["", "Madeline, ", "Madeleine, ", "oye Madelain ", "Madelein, "]
