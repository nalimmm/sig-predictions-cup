#!/usr/bin/env bash
# Run on the server, as root, from the repository after `git pull`:
#   sudo /opt/sig-predictions-cup/deploy/install_book_recorder.sh
# Installs the Python dependencies, the recorder service, the daily trades timer and, once
# /etc/book-recorder.env holds the object storage credentials, the daily archive timer. Starts
# them and shows the first lines of the journal. Safe to run again after an update.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

if ! python3 -c "import websockets, requests" 2>/dev/null; then
  apt-get install -y python3-websockets python3-requests \
    || python3 -m pip install --break-system-packages "websockets>=10" "requests>=2.31"
fi
if ! python3 -c "import boto3" 2>/dev/null; then
  apt-get install -y python3-boto3 || python3 -m pip install --break-system-packages "boto3>=1.26"
fi
id bot >/dev/null 2>&1 || { echo "user 'bot' missing: create it or edit User= in the units" >&2; exit 1; }

install -m 644 deploy/book-recorder.service deploy/book-trades.service deploy/book-trades.timer \
  deploy/book-offload.service deploy/book-offload.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now book-recorder.service book-trades.timer
if [ -f /etc/book-recorder.env ]; then
  chmod 600 /etc/book-recorder.env
  systemctl enable --now book-offload.timer
else
  echo "No /etc/book-recorder.env yet: archive timer not enabled (see deploy/book-recorder.env.example)."
fi
systemctl restart book-recorder.service
sleep 20
systemctl --no-pager --lines=15 status book-recorder.service || true
ls -la /var/lib/book-recorder/book/ || true
