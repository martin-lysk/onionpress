#!/bin/bash

# OnionPress Installer for Raspberry Pi / Linux
# Usage: curl -sSL https://raw.githubusercontent.com/brewsterkahle/onionpress/main/linux/install.sh | bash
#
# Or clone the repo and run: bash linux/install.sh

set -e

INSTALL_DIR="/opt/onionpress"
REPO_URL="https://github.com/brewsterkahle/onionpress"

# Resolve the real user even when run with sudo
if [ -n "$SUDO_USER" ]; then
    REAL_USER="$SUDO_USER"
    REAL_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
else
    REAL_USER="$(whoami)"
    REAL_HOME="$HOME"
fi
DATA_DIR="$REAL_HOME/.onionpress"

echo ""
echo "  OnionPress Installer for Linux"
echo "  ==============================="
echo ""

# ─── Checks ──────────────────────────────────────────────────────────

# Check architecture
ARCH=$(uname -m)
case "$ARCH" in
    aarch64|arm64)
        echo "  Architecture: ARM64 (Raspberry Pi / Apple Silicon)"
        ;;
    x86_64)
        echo "  Architecture: x86_64"
        ;;
    armv7l)
        echo "  Architecture: ARM32 (may be slow, 64-bit OS recommended)"
        ;;
    *)
        echo "ERROR: Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

# Check for root/sudo
if [ "$EUID" -ne 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo "ERROR: This script must be run as root or with sudo available."
        exit 1
    fi
    SUDO="sudo"
else
    SUDO=""
fi

# ─── Install Docker ──────────────────────────────────────────────────

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo "  Docker: already installed ($(docker --version | cut -d' ' -f3 | tr -d ','))"
else
    echo "  Installing Docker..."
    curl -sSL https://get.docker.com | $SUDO sh
    # Add the real user to docker group
    $SUDO usermod -aG docker "$REAL_USER"
    echo "  Docker installed (added $REAL_USER to docker group)"
fi

# Check docker compose plugin
if docker compose version >/dev/null 2>&1; then
    echo "  Docker Compose: available"
else
    echo "  Installing Docker Compose plugin..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq docker-compose-plugin
    echo "  Docker Compose plugin installed"
fi

# Ensure jq is available (used by status command)
if ! command -v jq >/dev/null 2>&1; then
    echo "  Installing jq..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq jq
fi

# Ensure python3 is available
if ! command -v python3 >/dev/null 2>&1; then
    echo "  Installing python3..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq python3
fi

# Ensure unzip and zip are available (needed for plugin installs and backups)
if ! command -v unzip >/dev/null 2>&1 || ! command -v zip >/dev/null 2>&1; then
    echo "  Installing zip/unzip..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq zip unzip
fi

# ─── Install OnionPress files ────────────────────────────────────────

echo ""
echo "  Installing OnionPress to $INSTALL_DIR..."

$SUDO mkdir -p "$INSTALL_DIR"

# Determine source: if we're in the repo, use local files; otherwise clone
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/onionpress" ] && [ -d "$SCRIPT_DIR/../OnionPress.app" ]; then
    # Running from cloned repo
    REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
    echo "  Source: local repo at $REPO_DIR"
else
    # Download from GitHub
    echo "  Downloading from GitHub..."
    TMPDIR=$(mktemp -d)
    git clone --depth 1 "$REPO_URL" "$TMPDIR/onionpress"
    REPO_DIR="$TMPDIR/onionpress"
fi

# Verify required source directories exist
RESOURCES_DIR="$REPO_DIR/OnionPress.app/Contents/Resources"
if [ ! -d "$RESOURCES_DIR/docker" ]; then
    echo "  ERROR: $RESOURCES_DIR/docker not found."
    echo "  If you cloned with sparse checkout or files are missing, try: git checkout -- OnionPress.app/"
    exit 1
fi

# Copy files
$SUDO cp "$REPO_DIR/linux/onionpress" "$INSTALL_DIR/onionpress"
$SUDO chmod +x "$INSTALL_DIR/onionpress"

$SUDO cp -r "$RESOURCES_DIR/docker" "$INSTALL_DIR/docker"
$SUDO cp -r "$RESOURCES_DIR/plugins" "$INSTALL_DIR/plugins"

if [ -d "$REPO_DIR/OnionPress.app/Contents/Resources/scripts" ]; then
    $SUDO cp -r "$REPO_DIR/OnionPress.app/Contents/Resources/scripts" "$INSTALL_DIR/scripts"
fi

# Copy OnionHeaven client scripts
$SUDO mkdir -p "$INSTALL_DIR/scripts"
$SUDO cp "$REPO_DIR/src/onion_auth.py" "$INSTALL_DIR/scripts/"
$SUDO cp "$REPO_DIR/src/key_manager.py" "$INSTALL_DIR/scripts/"
$SUDO cp "$REPO_DIR/linux/onionheaven-client.py" "$INSTALL_DIR/scripts/"

# Bind WordPress and SOCKS ports to 0.0.0.0 for LAN access (Pi is headless,
# users access from another device). The main compose file uses 127.0.0.1.
# Docker Compose override files don't reliably merge port bindings, so we patch directly.
$SUDO sed -i 's/127\.0\.0\.1:\${ONIONPRESS_WP_PORT/0.0.0.0:${ONIONPRESS_WP_PORT/' "$INSTALL_DIR/docker/docker-compose.yml"
$SUDO sed -i 's/127\.0\.0\.1:\${ONIONPRESS_SOCKS_PORT/0.0.0.0:${ONIONPRESS_SOCKS_PORT/' "$INSTALL_DIR/docker/docker-compose.yml"

# Write version file
VERSION="unknown"
if [ -f "$REPO_DIR/VERSION" ]; then
    VERSION=$(cat "$REPO_DIR/VERSION" | tr -d '[:space:]')
elif [ -f "$REPO_DIR/OnionPress.app/Contents/Info.plist" ] && command -v python3 >/dev/null 2>&1; then
    VERSION=$(python3 -c "
import xml.etree.ElementTree as ET, sys
try:
    tree = ET.parse(sys.argv[1])
    keys = list(tree.iter())
    for i, el in enumerate(keys):
        if el.tag == 'key' and el.text == 'CFBundleShortVersionString':
            print(keys[i+1].text); break
except: print('unknown')
" "$REPO_DIR/OnionPress.app/Contents/Info.plist" 2>/dev/null || echo "unknown")
fi
echo "$VERSION" | $SUDO tee "$INSTALL_DIR/VERSION" > /dev/null

echo "  OnionPress $VERSION installed to $INSTALL_DIR"

# Clean up temp dir if we cloned
if [ -n "${TMPDIR:-}" ] && [ -d "${TMPDIR:-}" ]; then
    rm -rf "$TMPDIR"
fi

# ─── Create data directory & secrets ─────────────────────────────────

echo ""
echo "  Setting up data directory at $DATA_DIR..."

# Create data dirs owned by the real user (not root)
install -d -o "$REAL_USER" -m 755 "$DATA_DIR"
install -d -o "$REAL_USER" -m 755 "$DATA_DIR/shared"
install -d -o "$REAL_USER" -m 755 "$DATA_DIR/shared/vanity-keys"

# Generate secrets if they don't exist
if [ ! -f "$DATA_DIR/secrets" ]; then
    WP_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)
    ROOT_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)

    cat > "$DATA_DIR/secrets" <<EOF
# Database passwords - generated on $(date)
# DO NOT SHARE THESE PASSWORDS
WORDPRESS_DB_PASSWORD='$WP_PASS'
MYSQL_PASSWORD='$WP_PASS'
MYSQL_ROOT_PASSWORD='$ROOT_PASS'
EOF
    chmod 600 "$DATA_DIR/secrets"
    chown "$REAL_USER" "$DATA_DIR/secrets"
    echo "  Database passwords generated"
else
    echo "  Existing secrets preserved"
fi

# Create default config
if [ ! -f "$DATA_DIR/config" ]; then
    cat > "$DATA_DIR/config" <<EOF
ADDRESS_PREFIX=op2
INSTALL_IA_PLUGIN=yes
UPDATE_ON_LAUNCH=no
START_ON_BOOT=yes
REGISTER_WITH_ONIONHEAVEN=yes
ONIONHEAVEN_ADDRESS=oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion
EOF
    chown "$REAL_USER" "$DATA_DIR/config"
    echo "  Default config created"
fi

# ─── Install systemd service ─────────────────────────────────────────

echo ""
echo "  Installing systemd service..."

# Install systemd service configured for the real user
cat > /tmp/onionpress.service.tmp <<SVCEOF
[Unit]
Description=OnionPress - WordPress over Tor Onion Service
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=$REAL_USER
ExecStart=/opt/onionpress/onionpress start
ExecStop=/opt/onionpress/onionpress stop
TimeoutStartSec=300
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF
$SUDO cp /tmp/onionpress.service.tmp /etc/systemd/system/onionpress.service
rm -f /tmp/onionpress.service.tmp

# Install a timer that polls for action requests from the WordPress settings page
# (restart, stop, config changes) — similar to the Mac menubar app's polling loop
cat > /tmp/onionpress-watcher.service.tmp <<WATCHSVCEOF
[Unit]
Description=OnionPress settings watcher
After=onionpress.service

[Service]
Type=oneshot
User=$REAL_USER
ExecStart=/opt/onionpress/onionpress handle-action
StandardOutput=journal
StandardError=journal
WATCHSVCEOF

cat > /tmp/onionpress-watcher.timer.tmp <<WATCHTMREOF
[Unit]
Description=Poll for OnionPress settings changes every 10s

[Timer]
OnActiveSec=10
OnUnitActiveSec=10
AccuracySec=5

[Install]
WantedBy=timers.target
WATCHTMREOF

$SUDO cp /tmp/onionpress-watcher.service.tmp /etc/systemd/system/onionpress-watcher.service
$SUDO cp /tmp/onionpress-watcher.timer.tmp /etc/systemd/system/onionpress-watcher.timer
rm -f /tmp/onionpress-watcher.service.tmp /tmp/onionpress-watcher.timer.tmp

# Install OnionHeaven client service (registers with hub, sends heartbeats)
cat > /tmp/onionpress-onionheaven.service.tmp <<OHSVCEOF
[Unit]
Description=OnionPress OnionHeaven client
After=onionpress.service
Requires=onionpress.service

[Service]
Type=simple
User=$REAL_USER
ExecStart=/usr/bin/python3 /opt/onionpress/scripts/onionheaven-client.py
Restart=on-failure
RestartSec=30
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
OHSVCEOF
$SUDO cp /tmp/onionpress-onionheaven.service.tmp /etc/systemd/system/onionpress-onionheaven.service
rm -f /tmp/onionpress-onionheaven.service.tmp

$SUDO systemctl daemon-reload
$SUDO systemctl enable onionpress
$SUDO systemctl enable --now onionpress-watcher.timer
$SUDO systemctl enable onionpress-onionheaven
echo "  Systemd service installed and enabled (starts on boot)"

# ─── Start OnionPress ─────────────────────────────────────────────────

echo ""
echo "  Starting OnionPress (this may take a few minutes on first run)..."
echo "  Docker will pull container images for WordPress, MariaDB, and Tor."
echo ""

$SUDO systemctl daemon-reload
$SUDO systemctl restart onionpress

# Wait for the service to finish starting
echo "  Waiting for services..."
local_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

# Wait for WordPress container to respond (DB can take 20-30s on first run)
wp_wait=0
while [ $wp_wait -lt 60 ]; do
    if curl -s --max-time 3 "http://localhost:8080" >/dev/null 2>&1; then
        break
    fi
    sleep 2
    wp_wait=$((wp_wait + 2))
done

# Check if it started successfully
if $SUDO systemctl is-active --quiet onionpress; then
    # Try to get the onion address
    onion_addr=$("$INSTALL_DIR/onionpress" address 2>/dev/null || echo "Generating...")

    echo ""
    echo "  ======================================="
    echo "  OnionPress is running!"
    echo "  ======================================="
    echo ""
    echo "  Local access:  http://${local_ip}:8080"
    echo "  Status page:   http://${local_ip}:8080/onionpress-status"
    echo ""
    if [ "$onion_addr" != "Generating..." ] && [ -n "$onion_addr" ]; then
        echo "  Onion address: http://${onion_addr}"
    else
        echo "  Onion address: Still generating... (run 'onionpress address' to check)"
    fi
    echo ""

    # Run first-time WordPress setup if not already installed
    if ! docker exec onionpress-wordpress wp core is-installed --allow-root >/dev/null 2>&1; then
        # Run as the real user so DATA_DIR resolves correctly
        if [ -n "$SUDO_USER" ]; then
            sudo -u "$SUDO_USER" "$INSTALL_DIR/onionpress" setup
        else
            "$INSTALL_DIR/onionpress" setup
        fi
    fi

    echo ""
    echo "  Commands:"
    echo "    onionpress status       - Show container status"
    echo "    onionpress address      - Show .onion address"
    echo "    onionpress setup        - Re-run first-time setup"
    echo "    onionpress logs         - Stream container logs"
    echo "    onionpress write-status - Update status page data"
    echo "    sudo systemctl restart onionpress - Restart"
    echo "    sudo systemctl stop onionpress    - Stop"
    echo ""
    echo "  Log file: $DATA_DIR/onionpress.log"
    echo ""
else
    echo ""
    echo "  WARNING: OnionPress may still be starting."
    echo "  Check status with: sudo systemctl status onionpress"
    echo "  Check logs with:   journalctl -u onionpress"
    echo "  Or:                cat $DATA_DIR/onionpress.log"
    echo ""
fi

# Create symlink for easy CLI access
$SUDO ln -sf "$INSTALL_DIR/onionpress" /usr/local/bin/onionpress 2>/dev/null || true
