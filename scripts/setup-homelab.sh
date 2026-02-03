#!/bin/bash
# Setup script for home lab deployment
# This clones the home-lab-deploy repository for local use only

set -e

HOMELAB_DIR="infrastructure/home-lab-deploy"

echo "🏠 Home Lab Setup"
echo "================="

# Check if we're on the home network
if ! ping -c 1 192.168.1.1 &> /dev/null; then
    echo "⚠️  Warning: Not on home network. This script is for home lab use only."
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Clone home-lab-deploy if it doesn't exist
if [ ! -d "$HOMELAB_DIR" ]; then
    echo "📦 Cloning home-lab-deploy repository..."
    mkdir -p infrastructure
    git clone https://github.com/milesburton/home-lab-deploy.git "$HOMELAB_DIR"
    echo "✅ home-lab-deploy cloned successfully"
else
    echo "✅ home-lab-deploy already exists"

    # Update it
    echo "🔄 Updating home-lab-deploy..."
    cd "$HOMELAB_DIR"
    git pull
    cd -
fi

echo ""
echo "✅ Home lab setup complete!"
echo ""
echo "The home-lab-deploy repository is available at:"
echo "  $HOMELAB_DIR"
echo ""
echo "Note: This directory is gitignored and won't be committed."
