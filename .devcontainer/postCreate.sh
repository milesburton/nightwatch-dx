#!/usr/bin/env bash
set -euo pipefail

# ── Install Node dependencies ──────────────────────────────────────────────────
cd ui && npm install && cd ..

# ── Fish config: MOTD ─────────────────────────────────────────────────────────
FISH_CONFIG_DIR="$HOME/.config/fish"
mkdir -p "$FISH_CONFIG_DIR"

cat > "$FISH_CONFIG_DIR/config.fish" << 'EOF'
# dx-watch dev container

# Show MOTD on interactive login shells only
if status is-interactive
    echo ""
    echo "  dx-watch — live HF monitoring" | lolcat
    echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" | lolcat
    echo ""
    echo "  Listens on 14 MHz (20m) via RTL-SDR + HF upconverter."
    echo "  Decodes CW (Morse) and SSTV transmissions in real time."
    echo ""
    echo "  Quick start:"
    echo "    make test        — run all Python + TypeScript tests"
    echo "    make quality     — lint + typecheck + test"
    echo "    cd ui && npm run dev   — start Vite dev server"
    echo ""
    echo "  Docs: docs/architecture.md · docs/signal-chain.md"
    echo ""
end
EOF

echo "Dev container setup complete."
