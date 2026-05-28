#!/bin/sh
[ -f /app/config.json ] || echo '{}' > /app/config.json
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
