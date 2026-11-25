#!/usr/bin/env bash
# Read-only health check of the Helsinki server. It changes nothing (two git fetches aside, which
# only refresh what the repository knows of the remote) and prints no secret: no key, no storage
# identifier, no wallet address. Env files are never printed, only the names of their variables.
#   cd /opt/sig-predictions-cup && git fetch -q origin claude/project-thread-xc6pkv && git show FETCH_HEAD:deploy/sanity.sh > /tmp/sanity.sh && bash /tmp/sanity.sh 2>&1 | tee /tmp/sanity.txt
set +e
export LC_ALL=C
REPO=/opt/sig-predictions-cup; ROOT=/opt/smart-mm; PY=$REPO/.venv/bin/python
PAPER=claude/project-thread-xc6pkv; LIVE=claude/project-thread-qlk8wu
URL=https://5mo5m.upcloudobjects.com/binomial-books/smart-dashboard
h() { echo; echo "=== $* ==="; }
age() { [ -e "$1" ] && echo "$(( $(date +%s) - $(stat -c %Y "$1") )) s  $(stat -c %s "$1") bytes  $1" || echo "MISSING  $1"; }

h "1. services"
for u in book-recorder smart-mm-live smart-mm-shadow smart-mm-real polymarket-dryrun; do
  printf '%-18s active=%-9s enabled=%-9s restarts=%-3s mem=%s/%s since=%s\n' "$u" \
    "$(systemctl is-active $u.service)" "$(systemctl is-enabled $u.service 2>/dev/null)" \
    "$(systemctl show $u.service -p NRestarts --value)" \
    "$(systemctl show $u.service -p MemoryCurrent --value | awk '{ if ($1+0>0 && $1+0<1e15) printf "%dM", $1/1048576; else print "-" }')" \
    "$(systemctl show $u.service -p MemoryMax --value | awk '{ if ($1+0>0 && $1+0<1e15) printf "%dM", $1/1048576; else print "-" }')" \
    "$(systemctl show $u.service -p ActiveEnterTimestamp --value)"
done
h "2. timers and the last run of what they start"
for t in book-offload book-trades smart-mm-scan polymarket-dryrun; do
  printf '%-18s timer=%-9s enabled=%-9s last_result=%-8s exit=%s\n' "$t" "$(systemctl is-active $t.timer)" \
    "$(systemctl is-enabled $t.timer 2>/dev/null)" "$(systemctl show $t.service -p Result --value)" \
    "$(systemctl show $t.service -p ExecMainStatus --value)"
done
systemctl list-timers --all --no-pager 2>/dev/null | grep -E 'NEXT|book-|smart-|polymarket' | cut -c1-150
echo "failed units: $(systemctl --failed --no-legend --no-pager | wc -l)"; systemctl --failed --no-legend --no-pager | cut -c1-120

h "3. warnings and errors in the journals, last 24 h"
for u in book-recorder book-offload book-trades smart-mm-live smart-mm-shadow smart-mm-scan smart-mm-real; do
  n=$(sudo journalctl -u $u.service --since -24h --no-pager -q 2>/dev/null | grep -cE 'WARNING|ERROR|CRITICAL|Traceback|oom|Out of memory')
  echo "$u: $n"
  [ "$n" -gt 0 ] && sudo journalctl -u $u.service --since -24h --no-pager -q | grep -E 'WARNING|ERROR|CRITICAL|Traceback|oom|Out of memory' | tail -3 | cut -c1-220
done
echo "kernel out-of-memory kills, 7 days: $(sudo journalctl -k --since -7d --no-pager -q 2>/dev/null | grep -ciE 'out of memory|oom-kill')"

h "4. machine"
uptime; free -m | sed -n 1,3p; df -h / | tail -1
echo "clock synchronised: $(timedatectl show -p NTPSynchronized --value)"
echo "books on disk: $(du -sh /var/lib/book-recorder/book 2>/dev/null | cut -f1), day files: $(ls /var/lib/book-recorder/book 2>/dev/null | grep -c jsonl)"
ls /var/lib/book-recorder/book 2>/dev/null | grep jsonl | sed 's/\..*//' | sort -u | tr '\n' ' '; echo

h "5. freshness (seconds since the last write)"
age "$(ls -t /var/lib/book-recorder/book/* 2>/dev/null | head -1)"
for f in data.enc data-guest.enc state.json btc_spot.jsonl scan/scan.json scan_history.jsonl; do age /var/lib/smart-mm/$f; done
for f in $(sudo ls -t /var/lib/smart-shadow 2>/dev/null | head -6); do [ -f /var/lib/smart-shadow/$f ] && age /var/lib/smart-shadow/$f; done
age "$(sudo sh -c 'ls -t /var/lib/smart-shadow/journal/* 2>/dev/null | head -1')"
echo "what a visitor's browser gets:"
for f in index.html data.enc data-guest.enc data-shadow.enc data-shadow-guest.enc; do
  curl -sI -m 15 "$URL/$f" | tr -d '\r' | awk -v f=$f 'NR==1{c=$2} tolower($1)=="last-modified:"{$1="";m=$0} tolower($1)=="content-length:"{l=$2} END{print f": http "c", "l" bytes, modified"m}'
done
echo "now: $(date -u '+%a, %d %b %Y %H:%M:%S GMT')"

h "6. archive to object storage"
sudo journalctl -u book-offload.service -n 4 --no-pager -q | cut -c1-200

h "7. code in service against the branches"
cd $REPO || exit 1
cmp_branch() {   # branch, grep filter for the paths
  git fetch -q origin "$1" || { echo "fetch of $1 failed"; return; }
  ref=$(git rev-parse --short FETCH_HEAD); same=0; diff=0
  for p in $(git ls-tree -r --name-only FETCH_HEAD smart book_recorder deploy/smart-dashboard | grep -E "$2"); do
    a=$(git show FETCH_HEAD:"$p" | sha256sum | cut -c1-16); b=$(sha256sum "$ROOT/$p" 2>/dev/null | cut -c1-16)
    if [ "$a" = "$b" ]; then same=$((same+1)); else diff=$((diff+1)); echo "  differs: $p"; fi
  done
  echo "$1 at $ref: $same files identical, $diff different"
}
cmp_branch $LIVE '^smart/live/.*\.py$'
cmp_branch $PAPER '^(smart/[a-z_]+\.py|book_recorder/.*\.py|deploy/smart-dashboard/index\.html)$'
for u in book-recorder smart-mm-live smart-mm-shadow; do
  s=$(systemctl show $u.service -p ActiveEnterTimestamp --value)
  echo "$u: python files changed after its start: $(find $ROOT/smart $ROOT/book_recorder -name '*.py' -newermt "$s" 2>/dev/null | wc -l), runs from $(systemctl show $u.service -p WorkingDirectory --value)"
done
for f in book-recorder.service smart-mm-live.service smart-mm-scan.service smart-mm-scan.timer smart-mm-shadow.service smart-mm-real.service; do
  if [ -e $ROOT/deploy/$f ]; then cmp -s /etc/systemd/system/$f $ROOT/deploy/$f && echo "unit $f: as in the code" || echo "unit $f: DIFFERS from the code"; else echo "unit $f: no copy in $ROOT/deploy"; fi
done

h "8. paper and shadow: same markets, same rules"
echo "variables set in /etc/smart-mm.env: $(sudo sed -n 's/^\([A-Z_]*\)=.*/\1/p' /etc/smart-mm.env | tr '\n' ' ')"
echo "recorder options: $(sudo cat /proc/$(systemctl show -p MainPID --value book-recorder)/cmdline 2>/dev/null | tr '\0' ' ' | sed 's/.*record //' | cut -c1-900)"
cd $ROOT && sudo -u bot $PY - <<'EOF'
import json, os, re
from dataclasses import asdict
from smart.mm import MMConfig
c = asdict(MMConfig())
print("rules in the code:", {k: c[k] for k in ("quote_usdc", "max_inventory_usdc", "max_group_usdc", "max_net_delta_usdc", "stop_loss_usdc",
                                               "stop_exit_hours", "exit_hours", "min_hours_left", "scan_fail_limit") if k in c})
live = " ".join(open(os.path.join("smart/live", f)).read() for f in os.listdir("smart/live") if f.endswith(".py"))
print("shadow code mentions: scan rule %s, two-stage exit %s" % ("yes" if "on_scan" in live else "NO", "yes" if "stop_exit_hours" in live else "NO"))
def show(name, path):
    try:
        st = json.load(open(path))
    except Exception as e:
        print(name, "state not readable:", type(e).__name__); return set()
    qs = st.get("quoters", [])
    print("%s: %d markets, %d fills, stopped %d, scans failed in a row %s" % (
        name, len(qs), sum(len(q.get("fills", [])) for q in qs), sum(1 for q in qs if q.get("reduce_only")),
        {k[:28]: v for k, v in st.get("scan", {}).get("fails", {}).items() if v} or "none"))
    for q in qs:
        print("   %-62s inv %8.1f  stopped %s" % (q["slug"][:62], q.get("inv", 0), bool(q.get("reduce_only"))))
    return {q["slug"] for q in qs}
a, b = show("paper", "/var/lib/smart-mm/state.json"), show("shadow", "/var/lib/smart-shadow/state.json")
print("same markets in paper and shadow:", "yes" if a and a == b else "NO  only paper %s  only shadow %s" % (sorted(a - b), sorted(b - a)))
try:
    d = json.load(open("/var/lib/smart-mm/scan/scan.json"))
    print("last scan: window %s h, %d markets measured, %d selected, verdicts written: %s" % (
        d.get("hours"), d.get("markets_measured", 0), len(d.get("selected", [])), "yes" if "verdicts" in d else "no (scan older than the new code)"))
except Exception as e:
    print("last scan not readable:", type(e).__name__)
EOF
sudo journalctl -u smart-mm-live.service -u smart-mm-shadow.service --since -24h --no-pager -q | grep -E 'scan rule|guest access|shadow mode|no order is ever sent' | tail -6 | cut -c1-200

h "9. real mode: disarmed, no key (presence only, no value is read out)"
echo "smart-mm-real: active=$(systemctl is-active smart-mm-real.service) enabled=$(systemctl is-enabled smart-mm-real.service 2>/dev/null)"
for f in ARMED key STOP; do sudo test -e /etc/smart-live/$f && echo "/etc/smart-live/$f: PRESENT" || echo "/etc/smart-live/$f: absent"; done
echo "wallet.env lines filled in: $(sudo grep -cE '^POLYMARKET_[A-Z_]+=.+' /etc/smart-live/wallet.env 2>/dev/null)"
echo "dry-run bot key filled in: $(sudo grep -cE '^POLYMARKET_PRIVATE_KEY=.+' /etc/polymarket-bot.env 2>/dev/null)"
echo "files under /etc, /home/bot, /var/lib/polymarket-bot holding something shaped like a private key (names only):"
sudo grep -rIlE '(^|[^0-9a-fA-F])(0x)?[0-9a-fA-F]{64}([^0-9a-fA-F]|$)' /etc/*.env /etc/smart-live /home/bot/*.env /home/bot/.env /var/lib/polymarket-bot 2>/dev/null | head -10
echo "(end of list)"
echo "real orders sent, ever: $(sudo sh -c 'ls /var/lib/smart-live 2>/dev/null | wc -l') files under /var/lib/smart-live"

h "10. access and location"
sudo ufw status | head -8
echo "listening ports: $(sudo ss -tlnH | awk '{print $4}' | tr '\n' ' ')"
echo "fail2ban: $(systemctl is-active fail2ban)"
sudo sshd -T 2>/dev/null | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication) '
curl -s -m 15 https://polymarket.com/api/geoblock | $PY -c 'import json,sys; print("polymarket geoblock, blocked:", json.load(sys.stdin).get("blocked"))' 2>/dev/null || echo "geoblock check failed"
echo; echo "=== end ==="
