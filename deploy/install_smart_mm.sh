#!/usr/bin/env bash
# Install the paper market maker and its private dashboard feed. Run as root from the extracted
# code:  sudo bash /opt/smart-mm/deploy/install_smart_mm.sh
# Sends no order and holds no wallet key. Never prints the content of an env file.
set -euo pipefail
ROOT=/opt/smart-mm
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -d "$ROOT/smart" ] || { echo "extract the code into $ROOT first"; exit 1; }

PY=""
for p in /opt/sig-predictions-cup/.venv/bin/python /usr/bin/python3; do
    if [ -x "$p" ] && "$p" -c "import boto3" 2>/dev/null; then PY="$p"; break; fi
done
[ -n "$PY" ] || { echo "no interpreter with boto3 found"; exit 1; }
if ! "$PY" -c "import cryptography" 2>/dev/null; then
    if [ "$PY" = /usr/bin/python3 ]; then apt-get install -y -q python3-cryptography
    else "$PY" -m pip install -q cryptography; fi
fi
echo "interpreter: $PY, cryptography $("$PY" -c 'import cryptography; print(cryptography.__version__)')"

if [ ! -s /etc/smart-mm.env ]; then        # the market list chosen on 2026-11-23, not a secret
    echo 'SMART_MM_SLUGS=will-bitcoin-dip-to-40000-by-december-31-2026-593-522-842-874-931-776-991-167-531-959,will-bitcoin-dip-to-50k-in-november-2026,will-chiang-wan-an-win-the-next-taipei-mayor-election-20260813125857101,will-bitcoin-dip-to-52pt5k-in-november-2026,will-bitcoin-dip-to-55000-by-december-31-2026-527-627-868-745-188-361-314-673-612-946-821-624-855-557-684-381-884-939-182-939-516,will-bitcoin-dip-to-47pt5k-in-november-2026' > /etc/smart-mm.env
fi
echo "markets: $(tr ',' '\n' < /etc/smart-mm.env | wc -l)"

sed "s#/opt/sig-predictions-cup/.venv/bin/python#$PY#" "$ROOT/deploy/smart-mm-live.service" > /etc/systemd/system/smart-mm-live.service
sed "s#/opt/sig-predictions-cup/.venv/bin/python#$PY#" "$ROOT/deploy/smart-mm-scan.service" > /etc/systemd/system/smart-mm-scan.service
cp "$ROOT/deploy/smart-mm-scan.timer" /etc/systemd/system/smart-mm-scan.timer
systemctl daemon-reload
systemctl enable smart-mm-scan.timer
systemctl restart smart-mm-scan.timer        # takes a changed schedule into account
echo "scan timer: $(systemctl is-active smart-mm-scan.timer || true), next $(systemctl show smart-mm-scan.timer -p NextElapseUSecRealtime --value)"

( set -a; . /etc/book-recorder.env; set +a; cd "$ROOT" && "$PY" -m smart dash-page --page deploy/smart-dashboard/index.html )

if [ -s /etc/smart-dash.env ]; then
    chown root:root /etc/smart-dash.env; chmod 600 /etc/smart-dash.env
    systemctl enable smart-mm-live.service
    systemctl restart smart-mm-live.service
    sleep 75
    echo "service: $(systemctl is-active smart-mm-live.service || true)"
    journalctl -u smart-mm-live -n 12 --no-pager | cut -c1-200
    ls -la /var/lib/smart-mm/ 2>/dev/null || true
else
    echo "password file /etc/smart-dash.env is missing or empty: service installed but not started"
fi
echo "other services: recorder $(systemctl is-active book-recorder.service || true), offload timer $(systemctl is-active book-offload.timer || true)"
