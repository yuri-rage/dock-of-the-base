#!/usr/bin/env bash
set -euo pipefail

echo

if ! command -v docker &>/dev/null; then
    echo "ERROR: Docker not found. Install it before starting the service."
    echo "  https://docs.docker.com/engine/install/"
    exit 1
fi

echo "Downloading configuration files..."
curl -fsSO https://raw.githubusercontent.com/yuri-rage/dock-of-the-base/master/docker-compose.yml || { echo "Failed to download docker-compose.yml"; exit 1; }
curl -fsSo config/config.json https://raw.githubusercontent.com/yuri-rage/dock-of-the-base/master/config/config.json.example || { echo "Failed to download config.json"; exit 1; }

echo "Setting up directories..."
mkdir -p config logs

if [ ! -f config/config.json ]; then
    echo "Downloading default configuration..."
    curl -fsSo config/config.json https://raw.githubusercontent.com/yuri-rage/dock-of-the-base/master/config/config.json.example || { echo "Failed to download config.json"; exit 1; }
    echo "Default configuration downloaded."
else
    echo "Existing config/config.json found — skipping default config download."
fi

echo "Installation complete. Use \`docker compose up -d\` to start the app."