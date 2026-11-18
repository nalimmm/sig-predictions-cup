#!/usr/bin/env bash
# Deploy the bot and the dashboard to the trading server, restart them, then run the post-deploy
# check. The deploy only counts as done when the check prints OK: a patch that is copied but not
# restarted, or restarted but crashing, fails here instead of failing silently on election night.
#
# Usage: deploy/deploy.sh user@server        (DEST defaults to /opt/predictions-cup)
set -euo pipefail

HOST="${1:?usage: deploy/deploy.sh user@server}"
DEST="${DEST:-/opt/predictions-cup}"
WAIT="${WAIT:-180}"
cd "$(dirname "$0")/.."

if ! git diff --quiet HEAD -- src deploy; then
  echo "Uncommitted changes in src/ or deploy/: commit them first, so the server runs a known commit." >&2
  exit 1
fi

manifest="$(mktemp)"
trap 'rm -f "$manifest"' EXIT
python3 deploy/post_deploy_check.py --write-manifest "$manifest"
echo "Deploying $(git rev-parse --short HEAD) to $HOST:$DEST"

rsync -az src deploy "$HOST:$DEST/"
rsync -az "$manifest" "$HOST:$DEST/deploy/manifest.sha256"

ssh "$HOST" "set -e; S=\$([ \"\$(id -u)\" = 0 ] || echo sudo)
  \$S install -m 644 $DEST/deploy/basket-bot.service $DEST/deploy/dashboard.service /etc/systemd/system/
  \$S systemctl daemon-reload
  \$S systemctl restart basket-bot dashboard"

ssh "$HOST" "S=\$([ \"\$(id -u)\" = 0 ] || echo sudo)
  \$S python3 $DEST/deploy/post_deploy_check.py --root $DEST --wait $WAIT"
