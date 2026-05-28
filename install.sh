#!/usr/bin/env bash

curl -O https://raw.githubusercontent.com/yuri-rage/dock-of-the-base/master/docker-compose.yml
mkdir -p config logs
curl -o config/config.json https://raw.githubusercontent.com/yuri-rage/dock-of-the-base/master/config/config.json.example

echo "Installation complete."
echo "Use \`docker compose up -d\` to start the app."