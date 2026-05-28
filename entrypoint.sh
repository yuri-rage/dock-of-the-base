#!/bin/sh
mkdir -p /app/config /app/logs
[ -f /app/config/config.json ] || echo '{"tty_exclude": "^tty(\\d+|S\\d+)?$"}' > /app/config/config.json
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
