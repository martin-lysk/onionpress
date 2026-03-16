#!/bin/sh
# OnionHeaven Tor Address Manager
# Manages dynamic onion service entries for address takeover/release.
# Supports both Arti (arti.toml + keystore) and C Tor (torrc + HiddenServiceDir).
#
# Usage:
#   onionheaven-tor-manager.sh takeover <content_address>
#   onionheaven-tor-manager.sh release <content_address>
#   onionheaven-tor-manager.sh sighup
#
# On takeover: installs key + config, signals Tor to reload.
# On release: removes config + keys, signals Tor to reload.

# Which Tor implementation are we managing?
TOR_IMPL="${TOR_IMPL:-tor}"

# Detect config: onionheaven/takeover-worker containers use arti-onionheaven.toml, main tor uses arti.toml
if [ "${NO_ONION_SERVICE}" = "1" ] || [ "${TAKEOVER_WORKER}" = "1" ]; then
    ARTI_TOML="/etc/arti/arti-onionheaven.toml"
else
    ARTI_TOML="/etc/arti/arti.toml"
fi
ARTI_KEYSTORE="/var/lib/arti/state/keystore/hss"
CTOR_TORRC="/etc/tor/torrc"
CTOR_HS_DIR="/var/lib/tor/hidden_service"
ONIONHEAVEN_KEYS_DIR="/var/lib/onionpress/onionheaven/keys"
REDIRECT_PORT=8082

send_sighup() {
    if [ "$TOR_IMPL" = "tor" ]; then
        local tor_pid
        tor_pid=$(pidof tor 2>/dev/null || ps aux | grep '[/]usr/bin/tor' | awk '{print $2}' | head -1)
        if [ -n "$tor_pid" ]; then
            kill -HUP "$tor_pid"
            echo "Sent SIGHUP to C Tor (pid $tor_pid)"
        else
            echo "WARNING: C Tor process not found, cannot send SIGHUP"
        fi
    else
        local arti_pid
        arti_pid=$(pidof arti 2>/dev/null || ps aux | grep '[/]usr/local/bin/arti' | awk '{print $2}' | head -1)
        if [ -n "$arti_pid" ]; then
            kill -HUP "$arti_pid"
            echo "Sent SIGHUP to Arti (pid $arti_pid)"
        else
            echo "WARNING: Arti process not found, cannot send SIGHUP"
        fi
    fi
}

usage() {
    echo "Usage: $0 takeover|release [--no-sighup] <content_address>"
    echo "       $0 sighup"
    exit 1
}

if [ $# -lt 1 ]; then
    usage
fi

ACTION="$1"
shift

# Parse flags
NO_SIGHUP=0
CONTENT_ADDRESS=""
for arg in "$@"; do
    case "$arg" in
        --no-sighup) NO_SIGHUP=1 ;;
        *) CONTENT_ADDRESS="$arg" ;;
    esac
done

# Handle sighup action early (no address needed)
if [ "$ACTION" = "sighup" ]; then
    send_sighup
    exit 0
fi

# Sanitize: strip any trailing whitespace/newlines
CONTENT_ADDRESS=$(echo "$CONTENT_ADDRESS" | tr -d '\n\r ')

# Validate address format (56 chars of base32 + .onion)
if ! echo "$CONTENT_ADDRESS" | grep -qE '^[a-z2-7]{56}\.onion$'; then
    echo "ERROR: Invalid .onion address format: $CONTENT_ADDRESS"
    exit 1
fi

# Nickname convention: onionheaven_ + first 16 chars of address (without .onion)
ADDR_PREFIX=$(echo "$CONTENT_ADDRESS" | sed 's/\.onion$//' | cut -c1-16)
NICKNAME="onionheaven_${ADDR_PREFIX}"

# Keystore directory for this service (Arti)
KEYSTORE_DIR="${ARTI_KEYSTORE}/${NICKNAME}"
# Hidden service directory for this service (C Tor)
HS_SERVICE_DIR="${CTOR_HS_DIR}/${NICKNAME}"

# ==================== Arti takeover/release ====================

do_takeover_arti() {
    local keys_src="${ONIONHEAVEN_KEYS_DIR}/${CONTENT_ADDRESS}"
    local key_file="${keys_src}/ks_hs_id.ed25519_expanded_private"

    # Check for plaintext Arti PEM key
    if [ ! -f "$key_file" ]; then
        echo "ERROR: No Arti key found for ${CONTENT_ADDRESS}"
        exit 1
    fi

    # Validate key integrity before copying to Arti keystore
    # Check 1: file must not be empty
    if [ ! -s "$key_file" ]; then
        echo "ERROR: Empty key file for ${CONTENT_ADDRESS}"
        exit 1
    fi
    # Check 2: must have proper PEM header
    if ! head -1 "$key_file" | grep -q "BEGIN OPENSSH PRIVATE KEY"; then
        echo "ERROR: Invalid PEM header in key for ${CONTENT_ADDRESS}"
        exit 1
    fi
    # Check 3: must have proper PEM footer
    if ! tail -1 "$key_file" | grep -q "END OPENSSH PRIVATE KEY"; then
        echo "ERROR: Invalid PEM footer in key for ${CONTENT_ADDRESS}"
        exit 1
    fi
    # Check 4: no NUL bytes (the specific Arti "PEM preamble contains invalid data" error)
    if tr -d '\0' < "$key_file" | cmp -s - "$key_file"; then
        : # no NUL bytes, good
    else
        echo "ERROR: Key contains NUL bytes for ${CONTENT_ADDRESS} — removing corrupted key"
        rm -f "$key_file"
        exit 1
    fi

    # Create the Arti keystore directory for this service
    mkdir -p "$KEYSTORE_DIR"

    # Copy the plaintext PEM key to the keystore
    if ! cp "$key_file" "${KEYSTORE_DIR}/ks_hs_id.ed25519_expanded_private"; then
        echo "ERROR: Failed to copy key for ${CONTENT_ADDRESS}"
        rm -rf "$KEYSTORE_DIR"
        exit 1
    fi

    # Set correct ownership and permissions
    chown -R arti:arti "$KEYSTORE_DIR" || echo "ERROR: Failed to chown keystore directory $KEYSTORE_DIR"
    chmod 700 "$KEYSTORE_DIR" || echo "ERROR: Failed to chmod keystore directory $KEYSTORE_DIR"
    chmod 600 "${KEYSTORE_DIR}/ks_hs_id.ed25519_expanded_private" || echo "ERROR: Failed to chmod keystore key file"

    # Add onion service config to arti.toml if not already present
    local marker="# onionheaven:${CONTENT_ADDRESS}"
    if grep -q "$marker" "$ARTI_TOML"; then
        echo "Service config already exists for ${CONTENT_ADDRESS}"
    else
        cat >> "$ARTI_TOML" << EOF

${marker}
[onion_services."${NICKNAME}"]
enabled = true
proxy_ports = [["80", "127.0.0.1:${REDIRECT_PORT}"]]
EOF
        echo "Added onion service config for ${CONTENT_ADDRESS}"
    fi

    echo "Takeover complete for ${CONTENT_ADDRESS} (Arti)"
}

do_release_arti() {
    # Remove onion service config from arti.toml
    local marker="# onionheaven:${CONTENT_ADDRESS}"
    if grep -q "$marker" "$ARTI_TOML"; then
        # Remove the marker line and the 3 config lines that follow it
        local tmp_toml="${ARTI_TOML}.tmp"
        awk -v marker="$marker" '
        BEGIN { skip = 0 }
        $0 == marker { skip = 3; next }
        skip > 0 { skip--; next }
        { print }
        ' "$ARTI_TOML" > "$tmp_toml"

        # Clean up any trailing blank lines left over
        sed -i '/^$/N;/^\n$/d' "$tmp_toml" 2>/dev/null || echo "WARNING: Failed to clean trailing blank lines in $tmp_toml"
        mv "$tmp_toml" "$ARTI_TOML"
        echo "Removed onion service config for ${CONTENT_ADDRESS}"
    else
        echo "No service config found for ${CONTENT_ADDRESS}"
    fi

    # Remove the keystore directory
    if [ -d "$KEYSTORE_DIR" ]; then
        rm -rf "$KEYSTORE_DIR"
        echo "Removed keystore directory for ${CONTENT_ADDRESS}"
    fi

    echo "Release complete for ${CONTENT_ADDRESS} (Arti)"
}

# ==================== C Tor takeover/release ====================

do_takeover_ctor() {
    local keys_src="${ONIONHEAVEN_KEYS_DIR}/${CONTENT_ADDRESS}"
    local key_file="${keys_src}/ks_hs_id.ed25519_expanded_private"

    # Check for key (Arti PEM format — we convert it)
    if [ ! -f "$key_file" ]; then
        echo "ERROR: No key found for ${CONTENT_ADDRESS}"
        exit 1
    fi
    if [ ! -s "$key_file" ]; then
        echo "ERROR: Empty key file for ${CONTENT_ADDRESS}"
        exit 1
    fi

    # Create hidden service directory for this address
    mkdir -p "$HS_SERVICE_DIR"

    # Convert Arti PEM key to C Tor format
    if ! python3 /key-convert.py arti-to-ctor "$key_file" "$HS_SERVICE_DIR"; then
        echo "ERROR: Key conversion failed for ${CONTENT_ADDRESS}"
        rm -rf "$HS_SERVICE_DIR"
        exit 1
    fi

    # Set correct ownership and permissions (C Tor is strict)
    chown -R debian-tor:debian-tor "$HS_SERVICE_DIR" 2>/dev/null || chown -R tor:tor "$HS_SERVICE_DIR" 2>/dev/null || true
    chmod 700 "$HS_SERVICE_DIR"

    # Add hidden service config to torrc if not already present
    local marker="# onionheaven:${CONTENT_ADDRESS}"
    if grep -q "$marker" "$CTOR_TORRC"; then
        echo "Service config already exists for ${CONTENT_ADDRESS}"
    else
        cat >> "$CTOR_TORRC" << EOF

${marker}
HiddenServiceDir ${HS_SERVICE_DIR}
HiddenServicePort 80 127.0.0.1:${REDIRECT_PORT}
EOF
        echo "Added hidden service config for ${CONTENT_ADDRESS}"
    fi

    echo "Takeover complete for ${CONTENT_ADDRESS} (C Tor)"
}

do_release_ctor() {
    # Remove hidden service config from torrc
    local marker="# onionheaven:${CONTENT_ADDRESS}"
    if grep -q "$marker" "$CTOR_TORRC"; then
        # Remove the marker line and the 2 config lines that follow it
        # (HiddenServiceDir, HiddenServicePort)
        local tmp_torrc="${CTOR_TORRC}.tmp"
        awk -v marker="$marker" '
        BEGIN { skip = 0 }
        $0 == marker { skip = 2; next }
        skip > 0 { skip--; next }
        { print }
        ' "$CTOR_TORRC" > "$tmp_torrc"

        sed -i '/^$/N;/^\n$/d' "$tmp_torrc" 2>/dev/null || true
        mv "$tmp_torrc" "$CTOR_TORRC"
        echo "Removed hidden service config for ${CONTENT_ADDRESS}"
    else
        echo "No service config found for ${CONTENT_ADDRESS}"
    fi

    # Remove the hidden service directory
    if [ -d "$HS_SERVICE_DIR" ]; then
        rm -rf "$HS_SERVICE_DIR"
        echo "Removed hidden service directory for ${CONTENT_ADDRESS}"
    fi

    echo "Release complete for ${CONTENT_ADDRESS} (C Tor)"
}

# ==================== Dispatch ====================

do_takeover() {
    if [ "$TOR_IMPL" = "tor" ]; then
        do_takeover_ctor
    else
        do_takeover_arti
    fi

    # Signal Tor to reload configuration (unless --no-sighup)
    if [ "$NO_SIGHUP" -eq 0 ]; then
        send_sighup
    else
        echo "Skipping SIGHUP (--no-sighup)"
    fi
}

do_release() {
    if [ "$TOR_IMPL" = "tor" ]; then
        do_release_ctor
    else
        do_release_arti
    fi

    # Signal Tor to reload configuration (unless --no-sighup)
    if [ "$NO_SIGHUP" -eq 0 ]; then
        send_sighup
    else
        echo "Skipping SIGHUP (--no-sighup)"
    fi
}

case "$ACTION" in
    takeover)
        if [ -z "$CONTENT_ADDRESS" ]; then usage; fi
        do_takeover
        ;;
    release)
        if [ -z "$CONTENT_ADDRESS" ]; then usage; fi
        do_release
        ;;
    *)
        usage
        ;;
esac
