#!/usr/bin/env bash
# Bienvenida al iniciar sesión (exec-once de Hyprland).
#
# Antes era un "sleep 8 && curl" fijo: en un arranque en frío el
# servidor a veces no alcanzaba a estar listo en 8 s y el curl fallaba
# en silencio, así que no sonaba nada. Ahora espera a que el servidor
# responda de verdad (hasta 2 minutos) y deja un log para diagnosticar.

URL="http://127.0.0.1:8000"
LOG="/tmp/madeline_bienvenida.log"

echo "$(date '+%F %T') esperando al servidor..." > "$LOG"

for _ in $(seq 120); do
    if curl -fsS --max-time 2 "$URL/" >/dev/null 2>&1; then
        echo "$(date '+%F %T') servidor listo, pidiendo bienvenida" >> "$LOG"
        curl -sS -X POST "$URL/bienvenida" >> "$LOG" 2>&1
        echo >> "$LOG"
        exit 0
    fi
    sleep 1
done

echo "$(date '+%F %T') el servidor nunca respondió" >> "$LOG"
exit 1
