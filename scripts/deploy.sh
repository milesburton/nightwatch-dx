#!/usr/bin/env bash
# deploy.sh — deploy dx-watch to the home-lab server
#
# Usage:
#   ./scripts/deploy.sh              # full deploy (git pull + docker compose build + up)
#   ./scripts/deploy.sh ui           # fast UI-only (build ui + up on server)
#   ./scripts/deploy.sh <service>    # rebuild + restart a single service (e.g. cw-decoder)
#
# Required environment variables (set in .env.deploy, which is gitignored):
#   SERVER  — ssh target, e.g. user@192.168.1.x
#   REPO    — repo path on server, e.g. ~/code/dx-watch
#
# Quick start:
#   cp .env.deploy.example .env.deploy   # fill in your values
#   source .env.deploy && ./scripts/deploy.sh

set -euo pipefail

# Load .env.deploy if it exists
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env.deploy"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$ENV_FILE"
fi

: "${SERVER:?Set SERVER=user@host in .env.deploy or environment}"
: "${REPO:?Set REPO=~/path/to/repo in .env.deploy or environment}"
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
