#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/faustus-ui.service"

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: faustus-ui.service not found in $SCRIPT_DIR"
  exit 1
fi

echo "Installing Faustus UI service..."
echo "Make sure you've edited faustus-ui.service with your username and paths first!"
echo ""

sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable faustus-ui
sudo systemctl start faustus-ui
sudo systemctl status faustus-ui
