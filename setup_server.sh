#!/usr/bin/env bash
# One-command install on a fresh Ubuntu server (Oracle Cloud Always Free, Hetzner, etc.).
# Run it from inside the story-factory folder:   bash setup_server.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"

echo "==> Installing Python…"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

echo "==> Creating a private Python environment…"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --upgrade pip -q
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt" -q

if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/.env.example" "$DIR/.env"
  chmod 600 "$DIR/.env"
  echo ""
  echo "!!  Fill in your keys now:   nano $DIR/.env"
  echo "    (paste with right-click, save with Ctrl+O then Enter, exit with Ctrl+X)"
  echo "    Then run this script again."
  exit 0
fi

chmod 600 "$DIR/.env"
echo "==> Creating the Discord channels…"
"$DIR/.venv/bin/python" "$DIR/run.py" --setup-discord

echo "==> Installing the 24/7 service (restarts by itself if it crashes or the server reboots)…"
sudo tee /etc/systemd/system/story-factory.service > /dev/null <<EOF
[Unit]
Description=Story Factory
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/run.py --loop
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now story-factory
echo ""
echo "✅ Running 24/7. Useful commands:"
echo "   see what it's doing:   journalctl -u story-factory -f"
echo "   restart after edits:   sudo systemctl restart story-factory"
echo "   stop it:               sudo systemctl stop story-factory"
