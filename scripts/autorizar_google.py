#!/usr/bin/env python3
"""Conecta Madeline con tu Google Calendar (se hace una sola vez).

1. En https://console.cloud.google.com crea un proyecto y activa la
   "Google Calendar API".
2. En "Google Auth Platform": tipo de usuario Externo, agrega tu correo
   como usuario de prueba y publica la app ("En producción"; si queda en
   prueba, el permiso vence cada 7 días).
3. En "Clientes", crea un ID de cliente OAuth de tipo "App de escritorio"
   y descarga el JSON a data/google/credentials.json.
4. Corre: .venv/bin/python scripts/autorizar_google.py

Se abre el navegador, eliges tu cuenta y el permiso queda en
data/google/token.json (no sale del PC; data/ está en .gitignore).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

import agenda  # noqa: E402
from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402


def main():
    if not os.path.exists(agenda.RUTA_CREDENCIALES):
        sys.exit(f"Falta {os.path.normpath(agenda.RUTA_CREDENCIALES)} (ver los pasos al inicio de este script).")
    flujo = InstalledAppFlow.from_client_secrets_file(agenda.RUTA_CREDENCIALES, agenda.SCOPES)
    credenciales = flujo.run_local_server(port=0, open_browser=True)
    with open(agenda.RUTA_TOKEN, "w", encoding="utf-8") as f:
        f.write(credenciales.to_json())
    os.chmod(agenda.RUTA_TOKEN, 0o600)
    print("Listo: Madeline ya puede leer y crear eventos en tu calendario.")


if __name__ == "__main__":
    main()
