#!/usr/bin/env bash
# Restart-on-crash wrapper for the watcher (macOS / Linux).
set -u
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] starting watcher"
    "$PY" main.py
    code=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] watcher exited (code $code) — restarting in 30s"
    sleep 30
done
