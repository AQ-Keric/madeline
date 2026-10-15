#!/bin/bash
# Pausa o reanuda la palabra de activación "Madeline" (Super+Shift+J).
PAUSA="/tmp/madeline_escucha_pausada"

if [ -f "$PAUSA" ]; then
    rm -f "$PAUSA"
    notify-send "Madeline" "👂 Escucha activada: di \"Madeline\"." -t 2500 -u low
else
    touch "$PAUSA"
    notify-send "Madeline" "🔇 Escucha en pausa." -t 2500 -u low
fi
