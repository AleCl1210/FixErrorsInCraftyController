#!/bin/sh
# Crafty Medic - entrypoint
# Supporta PUID/PGID per allinearsi ai permessi dei file di Crafty.
set -e

PUID="${PUID:-0}"
PGID="${PGID:-0}"

if [ "$CRAFTY_MEDIC_DEMO" = "true" ] && [ ! -f /app/demo/servers.json ]; then
    echo "[crafty-medic] genero i dati demo..."
    python /app/scripts/make_demo.py --out /app/demo --force || true
fi

if [ "$PUID" != "0" ] && [ "$PUID" != "$(id -u medic)" ]; then
    echo "[crafty-medic] avvio come uid=$PUID gid=$PGID"
    usermod -o -u "$PUID" medic 2>/dev/null || true
    groupmod -o -g "$PGID" medic 2>/dev/null || true
    chown -R "$PUID:$PGID" /data 2>/dev/null || true
    exec gosu medic "$@"
fi

exec "$@"
