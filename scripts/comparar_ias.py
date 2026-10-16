"""Compara qué tan bien eligen tools distintas IAs, con las mismas
frases, los mismos tools (según su tema) y el mismo prompt que usa
Madeline. No ejecuta ninguna acción: solo compara la tool elegida con
la esperada.

Uso:
  .venv/bin/python scripts/comparar_ias.py groq
  .venv/bin/python scripts/comparar_ias.py gemini
  .venv/bin/python scripts/comparar_ias.py gemini --modelos   (lista los disponibles)

Las claves salen del .env: GROQ_API_KEY y GEMINI_API_KEY. El modelo de
Gemini se cambia con GEMINI_MODELO (por defecto gemini-flash-lite-latest).
"""
import json
import os
import sys
import tempfile
import time

import requests

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(RAIZ, "server"))
# Las frases de la prueba no deben quedar en el log real de Madeline.
os.environ["MADELINE_LOG_SIN_TEMA"] = os.path.join(tempfile.gettempdir(), "comparar_ias_sin_tema.log")
import madeline_server as j  # noqa: E402  (también carga el .env)

PROVEEDORES = {
    "groq": {
        "url": "https://api.groq.com/openai/v1",
        "clave": "GROQ_API_KEY",
        "modelo": j.MODEL,
        # Mismos parámetros que procesar_mensaje.
        "extra": {"temperature": 0.2, "max_completion_tokens": 350, "reasoning_effort": "low"},
        "pausa": 2.5,
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "clave": "GEMINI_API_KEY",
        "modelo": os.getenv("GEMINI_MODELO", "gemini-flash-lite-latest"),
        "extra": {"temperature": 0.2, "max_tokens": 350},
        # El plan gratuito de Gemini permite pocas consultas por minuto.
        "pausa": 7,
    },
}

# (frase, tool esperada o None si es conversación, args que deben coincidir)
CASOS = [
    # apps
    ("abre el navegador", "abrir_aplicacion", {}),
    ("necesito escribir un documento, ábreme el libreoffice", "abrir_aplicacion", {}),
    ("cierra el discord que me tiene aburrido", "cerrar_aplicacion", {}),
    ("quítame el steam de encima", "cerrar_aplicacion", {}),
    ("ayer abrí una cuenta en el banco", None, {}),
    # ventanas
    ("mueve firefox al escritorio 3", "mover_ventana", {"escritorio": 3}),
    ("manda la terminal al workspace 2", "mover_ventana", {"escritorio": 2}),
    ("mi escritorio de madera está rayado", None, {}),
    # volumen
    ("no se escucha nada, súbele", "ajustar_volumen", {"accion": "subir"}),
    ("está muy fuerte, bájale un poco", "ajustar_volumen", {"accion": "bajar"}),
    ("deja el volumen en 40", "ajustar_volumen", {"accion": "setear", "porcentaje": 40}),
    ("silencia todo", "ajustar_volumen", {"accion": "mutear"}),
    ("el volumen de ventas subió este mes", None, {}),
    # clima
    ("¿necesito paraguas mañana?", "reporte_clima", {"dia": "mañana"}),
    ("¿hace frío afuera?", "reporte_clima", {}),
    ("¿va a llover pasado mañana?", "reporte_clima", {"dia": "pasado mañana"}),
    ("odio cuando hace calor", None, {}),
    # sistema
    ("el pc anda lento, ¿cómo está la ram?", "estado_sistema", {}),
    ("¿cuánto está usando el procesador?", "estado_sistema", {}),
    ("¿qué es la memoria ram?", None, {}),
    # energía
    ("apaga el computador", "control_energia", {"accion": "apagar"}),
    ("reinicia el pc por favor", "control_energia", {"accion": "reiniciar"}),
    ("cierra todo, rápido", "cerrar_todo", {}),
    ("ayer se apagó la luz en mi casa", None, {}),
    # actualizaciones
    ("¿hay actualizaciones pendientes?", "verificar_actualizaciones", {}),
    ("actualiza el sistema", "actualizar_sistema", {}),
    ("ayer actualicé el celular", None, {}),
    # bluetooth
    ("conecta los audífonos", "conectar_bluetooth", {}),
    ("desconecta el parlante", "desconectar_bluetooth", {}),
    ("prende el bluetooth", "controlar_bluetooth", {"accion": "encender"}),
    ("odio cuando se desconecta el bluetooth", None, {}),
    # pantalla
    ("toma una captura", "tomar_captura", {}),
    ("¿qué ves en mi pantalla?", "analizar_pantalla", {}),
    ("¿qué error me sale en la pantalla?", "analizar_pantalla", {}),
    ("se me quebró la pantalla del celular", None, {}),
    # búsqueda
    ("busca quién ganó el partido de anoche", "buscar_en_web", {}),
    ("noticias de chile de hoy", "buscar_en_web", {}),
    ("busco pareja jaja", None, {}),
    ("ábreme papers sobre redes neuronales", "abrir_pestanas_busqueda", {}),
    # música
    ("pon esa canción de nuevo", "control_multimedia", {"accion": "reiniciar"}),
    ("no me gusta esta canción, cámbiala", "control_multimedia", {"accion": "siguiente"}),
    ("detén eso", "control_multimedia", {"accion": "pausar"}),
    ("¿quién canta esto?", "info_cancion", {}),
    ("ponme algo de música", "abrir_radio_musica", {}),
    ("la música de ahora es mala", None, {}),
    ("me encanta cuando pausan la música en las fiestas", None, {}),
]


def llamar(prov, cuerpo, clave):
    """POST con reintentos si el proveedor pide esperar (429)."""
    for intento in range(4):
        r = requests.post(
            f"{prov['url']}/chat/completions", json=cuerpo, timeout=60,
            headers={"Authorization": f"Bearer {clave}"},
        )
        if r.status_code != 429:
            return r
        espera = 20 * (intento + 1)
        print(f"   (límite por minuto, espero {espera}s)", flush=True)
        time.sleep(espera)
    return r


def preguntar(prov, clave, frase):
    cuerpo = {
        "model": prov["modelo"],
        "messages": [
            {"role": "system", "content": j.system_prompt_con_fecha(j.SYSTEM_PROMPT)},
            {"role": "user", "content": frase},
        ],
        **prov["extra"],
    }
    tools = j.tools_necesarias(frase)
    if tools:
        cuerpo["tools"] = tools
        cuerpo["tool_choice"] = "auto"

    inicio = time.time()
    r = llamar(prov, cuerpo, clave)
    segundos = time.time() - inicio
    if r.status_code != 200:
        return "ERROR", r.text[:200], segundos, 0

    datos = r.json()
    tokens = (datos.get("usage") or {}).get("total_tokens", 0)
    mensaje = datos["choices"][0]["message"]
    llamadas = mensaje.get("tool_calls") or []
    if llamadas:
        f = llamadas[0]["function"]
        try:
            args = json.loads(f.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        return f["name"], args, segundos, tokens
    return None, (mensaje.get("content") or "")[:70], segundos, tokens


def args_ok(esperados, obtenidos):
    for k, v in esperados.items():
        o = obtenidos.get(k) if isinstance(obtenidos, dict) else None
        if isinstance(v, str):
            if not isinstance(o, str) or o.lower() != v:
                return False
        else:
            try:
                if int(o) != v:
                    return False
            except (TypeError, ValueError):
                return False
    return True


def listar_modelos(prov, clave):
    r = requests.get(f"{prov['url']}/models", headers={"Authorization": f"Bearer {clave}"}, timeout=30)
    r.raise_for_status()
    for m in sorted(m["id"] for m in r.json()["data"]):
        print(m)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in PROVEEDORES:
        sys.exit(f"Uso: {sys.argv[0]} {'|'.join(PROVEEDORES)} [--modelos]")
    prov = PROVEEDORES[sys.argv[1]]
    clave = os.getenv(prov["clave"], "").strip()
    if not clave:
        sys.exit(f"Falta {prov['clave']} en el .env")

    if "--modelos" in sys.argv:
        listar_modelos(prov, clave)
        return

    print(f"Probando {sys.argv[1]} / {prov['modelo']} con {len(CASOS)} frases...\n", flush=True)
    aciertos, tiempos, tokens_total, fallos = 0, [], 0, []
    for n, (frase, esperada, args) in enumerate(CASOS, 1):
        nombre, obtenidos, segundos, tokens = preguntar(prov, clave, frase)
        tiempos.append(segundos)
        tokens_total += tokens
        ok = nombre == esperada and (nombre is None or args_ok(args, obtenidos))
        aciertos += ok
        print(f"{n:2}. {'OK ' if ok else 'MAL'} {segundos:4.1f}s  {frase}", flush=True)
        if not ok:
            fallos.append(f"  {frase!r}: {nombre} {obtenidos}  (esperado {esperada} {args})")
        if n < len(CASOS):
            time.sleep(prov["pausa"])

    tiempos.sort()
    print(f"\n{sys.argv[1]} / {prov['modelo']}")
    print(f"Aciertos: {aciertos}/{len(CASOS)}")
    print(f"Tiempo por frase: mediana {tiempos[len(tiempos) // 2]:.2f}s, máx {tiempos[-1]:.2f}s")
    print(f"Tokens usados: {tokens_total} (~{tokens_total // len(CASOS)} por frase)")
    if fallos:
        print("Fallos:")
        print("\n".join(fallos))


if __name__ == "__main__":
    main()
