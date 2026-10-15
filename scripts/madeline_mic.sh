#!/bin/bash

AUDIO_FILE="/tmp/madeline_voz.wav"
PID_FILE="/tmp/madeline_mic.pid"

if [ -f "$PID_FILE" ]; then
    # ------------------------------------------
    # SOLTAR WIN+J (Detener y enviar)
    # ------------------------------------------
    # ¡Magia! Esperamos medio segundo para atrapar la última palabra
    sleep 0.6
    
    kill -INT $(cat "$PID_FILE") 2>/dev/null
    rm -f "$PID_FILE"
    
    notify-send "Madeline" "Procesando orden..." -t 2000 -u low

    FILE_SIZE=$(stat -c%s "$AUDIO_FILE" 2>/dev/null)
    if [ -z "$FILE_SIZE" ] || [ "$FILE_SIZE" -lt 100 ]; then
        notify-send "Madeline" "Error: Audio vacío." -t 3000 -u critical
        rm -f "$AUDIO_FILE"
        exit 1
    fi

    # Mandamos el audio DIRECTO a nuestra API local
    RESPUESTA=$(curl -s -m 120 -X POST "http://127.0.0.1:8000/comando_audio" \
         -H "accept: application/json" \
         -F "audio=@${AUDIO_FILE}")
    CODIGO=$?

    rm -f "$AUDIO_FILE"

    # Antes la respuesta se descartaba: si Madeline fallaba o no
    # entendía, no te enterabas. Ahora se muestra el error (o lo que
    # respondió) en una notificación.
    if [ $CODIGO -ne 0 ] || [ -z "$RESPUESTA" ]; then
        notify-send "Madeline" "No pude contactar al servidor." -t 4000 -u critical
        exit 1
    fi

    ERROR=$(echo "$RESPUESTA" | jq -r '.detail // empty' 2>/dev/null)
    if [ -n "$ERROR" ]; then
        notify-send "Madeline" "$ERROR" -t 4000 -u critical
        exit 1
    fi

    TEXTO=$(echo "$RESPUESTA" | jq -r '.response // empty' 2>/dev/null)
    [ -n "$TEXTO" ] && notify-send "Madeline" "$TEXTO" -t 6000 -u low
    exit 0
fi

# ------------------------------------------
# PRESIONAR WIN+J (Grabar)
# ------------------------------------------
notify-send "Madeline" "🎙️ Escuchando..." -t 2000 -u low
rm -f "$AUDIO_FILE"

# Callamos a Madeline apenas empieza la grabación (en segundo plano,
# para no atrasar el micrófono), así no se graba su propia voz.
curl -s -m 2 -X POST "http://127.0.0.1:8000/callar" > /dev/null 2>&1 &

parecord --file-format=wav --channels=1 --rate=16000 "$AUDIO_FILE" 2>/dev/null &
echo $! > "$PID_FILE"