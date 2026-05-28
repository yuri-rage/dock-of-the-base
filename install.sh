#!/usr/bin/env bash
set -euo pipefail

echo

if ! command -v docker &>/dev/null; then
    echo "WARNING: Docker not found. Install it before starting the service."
    echo "  https://docs.docker.com/engine/install/"
fi

curl -fsSO https://raw.githubusercontent.com/yuri-rage/dock-of-the-base/master/docker-compose.yml
mkdir -p config logs

if [ ! -f config/config.json ]; then
    curl -fsSo config/config.json https://raw.githubusercontent.com/yuri-rage/dock-of-the-base/master/config/config.json.example
else
    echo "Existing config/config.json found — skipping default config download."
fi

echo "Installation complete. Use \`docker compose up -d\` to start the app."