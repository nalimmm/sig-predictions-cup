#!/usr/bin/env bash
# Run on the server, as root, from the repository after `git pull`:
#   sudo /opt/sig-predictions-cup/deploy/install_smart_paper.sh
# Installs and starts the Smart pod's paper trader (signal S1). It only reads the recorder's
# files under /var/lib/book-recorder and writes /var/lib/smart; it holds no key and sends nothing.
# Safe to run again after an update (the paper state in /var/lib/smart/state.json is kept).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
systemctl is-active --quiet book-recorder.service || { echo "book-recorder.service is not running: the paper trader needs its files" >&2; exit 1; }
install -m 644 deploy/smart-paper.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable smart-paper.service
systemctl restart smart-paper.service
sleep 15
systemctl --no-pager --lines=15 status smart-paper.service || true
echo
echo "Journal: /var/lib/smart/journal/   Report: cd $REPO && python3 -m smart report /var/lib/smart/journal"
