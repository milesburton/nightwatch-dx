#!/usr/bin/env bash
# deploy.sh — deploy nightwatch-dx to the home-lab server
#
# Usage:
#   ./scripts/deploy.sh              # full deploy (git pull + docker compose pull + up)
#   ./scripts/deploy.sh ui           # fast UI-only (pull ui + up on server)
#   ./scripts/deploy.sh <service>    # pull + restart a single service (e.g. cw-decoder)
#
# Required variables (set in .env, which is gitignored):
#   SERVER  — ssh target, e.g. miles@192.168.1.211
#   REPO    — repo path on server, e.g. ~/nightwatch-dx
#
# Quick start:
#   cp .env.example .env   # fill in your values
#   ./scripts/deploy.sh

set -euo pipefail

# Load .env if it exists
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$ENV_FILE"
fi

: "${SERVER:?Set SERVER=user@host in .env.deploy or environment}"
: "${REPO:?Set REPO=~/path/to/repo in .env.deploy or environment}"
UI_CONTAINER="nightwatch-dx-ui"
NGINX_ROOT="/usr/share/nginx/html"
MODE="${1:-full}"

echo "=== nightwatch-dx deploy: mode=$MODE target=$SERVER ==="

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
