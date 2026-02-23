#!/usr/bin/env bash
# deploy.sh — deploy dx-watch to the home-lab server
#
# Usage:
#   ./scripts/deploy.sh              # full deploy (git pull + docker compose build + up)
#   ./scripts/deploy.sh ui           # fast UI-only deploy (build locally, scp into container)
#   ./scripts/deploy.sh <service>    # rebuild + restart a single service (e.g. cw-decoder)
#
# Environment:
#   SERVER  — ssh target, default miles@192.168.1.211
#   REPO    — repo path on server, default ~/code/gmktec-sdr-project

set -euo pipefail

SERVER="${SERVER:-miles@192.168.1.211}"
REPO="${REPO:-~/code/gmktec-sdr-project}"
UI_CONTAINER="dx-watch-ui"
NGINX_ROOT="/usr/share/nginx/html"
MODE="${1:-full}"

echo "=== dx-watch deploy: mode=$MODE target=$SERVER ==="

# ── Fast UI deploy ────────────────────────────────────────────────────────────
if [[ "$MODE" == "ui" ]]; then
  echo
  echo "▶ Deploying UI (git pull + docker compose build ui + up)..."
  ssh "$SERVER" bash <<EOF
set -e
cd ${REPO}
git pull --ff-only
docker compose build ui
docker compose up -d ui
docker compose logs --tail=10 ui
EOF
  echo
  echo "✓ UI deployed."
  echo "  http://${SERVER##*@}:8080"
  exit 0
fi

# ── Single-service rebuild ────────────────────────────────────────────────────
if [[ "$MODE" != "full" ]]; then
  SERVICE="$MODE"
  echo
  echo "▶ Deploying service: $SERVICE"
  ssh "$SERVER" bash <<EOF
set -e
cd ${REPO}
git pull --ff-only
docker compose build ${SERVICE}
docker compose up -d ${SERVICE}
docker compose logs --tail=20 ${SERVICE}
EOF
  echo
  echo "✓ $SERVICE restarted."
  exit 0
fi

# ── Full deploy ───────────────────────────────────────────────────────────────
echo
echo "▶ Full deploy..."
ssh "$SERVER" bash <<EOF
set -e
cd ${REPO}
git pull --ff-only
docker compose build
docker compose up -d
docker compose ps
EOF

echo
echo "✓ Full deploy complete."
echo "  http://${SERVER##*@}:8080"
