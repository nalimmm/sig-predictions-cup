#!/usr/bin/env bash
# Install the shadow market maker (started) and the real one (installed, never started here).
# Run as root from the extracted code:  sudo bash /opt/smart-mm/deploy/install_smart_live.sh
# Writes no key, arms nothing, sends no order. Never prints the content of an env file.
set -euo pipefail
ROOT=/opt/smart-mm
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -d "$ROOT/smart/live" ] || { echo "extract the code into $ROOT first"; exit 1; }

PY=/opt/sig-predictions-cup/.venv/bin/python
[ -x "$PY" ] || { echo "no venv at /opt/sig-predictions-cup/.venv"; exit 1; }
for mod in websockets requests boto3 cryptography; do
    "$PY" -c "import $mod" 2>/dev/null || "$PY" -m pip install -q "$mod"
done
"$PY" -c "import py_clob_client" 2>/dev/null || "$PY" -m pip install -q "py-clob-client==0.34.6"
echo "interpreter ok: $("$PY" -c 'import py_clob_client, websockets; print("py-clob-client and websockets present")')"

# /etc/smart-live: readable directory (the STOP and ARMED files are checked by the services),
# wallet.env and the key readable by root only.
install -d -m 755 -o root -g root /etc/smart-live
if [ ! -e /etc/smart-live/wallet.env ]; then
    install -m 600 -o root -g root /dev/null /etc/smart-live/wallet.env
    printf '%s\n' '# Filled in by the owner (see the start-up sheet). Not secret, but private.' \
        '# POLYMARKET_FUNDER=0x...            the address shown on the Polymarket profile' \
        '# POLYMARKET_SIGNATURE_TYPE=1        0 plain wallet, 1 email or Magic login, 2 browser wallet' > /etc/smart-live/wallet.env
fi
[ -s /etc/smart-mm.env ] || { echo "/etc/smart-mm.env (market list) is missing: install the paper market maker first"; exit 1; }

for unit in smart-mm-shadow smart-mm-real; do
    sed "s#/opt/sig-predictions-cup/.venv/bin/python#$PY#" "$ROOT/deploy/$unit.service" > /etc/systemd/system/$unit.service
done
systemctl daemon-reload
systemctl disable smart-mm-real.service 2>/dev/null || true      # real mode is started by the owner only

systemctl enable smart-mm-shadow.service
systemctl restart smart-mm-shadow.service
sleep 90
echo "shadow: $(systemctl is-active smart-mm-shadow.service || true)"
journalctl -u smart-mm-shadow -n 15 --no-pager | cut -c1-220
ls -la /var/lib/smart-shadow/ 2>/dev/null || true
echo "paper market maker still running: $(systemctl is-active smart-mm-live.service || true)"
echo "real mode: $(systemctl is-enabled smart-mm-real.service 2>/dev/null || true), $(systemctl is-active smart-mm-real.service || true) (expected: disabled, inactive)"
echo "--- read-only check of the real mode prerequisites ---"
( cd "$ROOT" && "$PY" -m smart.live check ) || true
