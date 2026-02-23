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
  echo "▶ Building UI locally..."
  cd "$(dirname "$0")/../ui"
  npm run build
  cd ..

  echo "▶ Packaging dist/..."
  tar czf /tmp/dx-watch-ui.tar.gz -C ui/dist .

  echo "▶ Copying to server..."
  scp /tmp/dx-watch-ui.tar.gz "$SERVER:/tmp/dx-watch-ui.tar.gz"

  echo "▶ Deploying into container $UI_CONTAINER..."
  ssh "$SERVER" bash <<EOF
set -e
docker cp /tmp/dx-watch-ui.tar.gz ${UI_CONTAINER}:/tmp/
docker exec ${UI_CONTAINER} sh -c '
  rm -rf ${NGINX_ROOT}/*
  tar xzf /tmp/dx-watch-ui.tar.gz -C ${NGINX_ROOT}
  rm /tmp/dx-watch-ui.tar.gz
  echo "  files in ${NGINX_ROOT}:"
  ls ${NGINX_ROOT}
'
rm -f /tmp/dx-watch-ui.tar.gz
EOF

  rm -f /tmp/dx-watch-ui.tar.gz

  echo
  echo "✓ UI deployed. Nginx serves the new build immediately (no restart needed)."
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
