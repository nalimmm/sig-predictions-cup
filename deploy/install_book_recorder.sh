#!/usr/bin/env bash
# Run on the server, as root, from the repository after `git pull`:
#   sudo /opt/sig-predictions-cup/deploy/install_book_recorder.sh
# Installs the two Python dependencies, the recorder service and the daily trades timer, starts
# them, and shows the first lines of the journal. Safe to run again after an update.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

if ! python3 -c "import websockets, requests" 2>/dev/null; then
  apt-get install -y python3-websockets python3-requests \
    || python3 -m pip install --break-system-packages "websockets>=10" "requests>=2.31"
fi
id bot >/dev/null 2>&1 || { echo "user 'bot' missing: create it or edit User= in the units" >&2; exit 1; }

install -m 644 deploy/book-recorder.service deploy/book-trades.service deploy/book-trades.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now book-recorder.service book-trades.timer
systemctl restart book-recorder.service
sleep 20
systemctl --no-pager --lines=15 status book-recorder.service || true
ls -la /var/lib/book-recorder/book/ || true
