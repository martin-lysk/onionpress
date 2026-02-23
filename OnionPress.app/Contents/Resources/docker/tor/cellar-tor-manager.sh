#!/bin/sh
# OnionCellar Arti Address Manager
# Manages dynamic onion service entries in arti.toml for address takeover/release.
#
# Usage:
#   cellar-tor-manager.sh takeover <content_address>
#   cellar-tor-manager.sh release <content_address>
#
# On takeover: decrypts Arti PEM key from cellar storage, installs into Arti keystore,
#              appends service config to arti.toml, signals Arti to reload.
# On release: removes service config from arti.toml, cleans up keystore directory,
#              signals Arti to reload.

ARTI_TOML="/etc/arti/arti.toml"
ARTI_KEYSTORE="/var/lib/arti/state/keystore/hss"
CELLAR_KEYS_DIR="/var/lib/onionpress/cellar/keys"
CELLAR_UNLOCKED_FILE="/var/lib/onionpress/cellar/.master-key-unlocked"
REDIRECT_PORT=8082

usage() {
    echo "Usage: $0 takeover|release <content_address>"
    exit 1
}

if [ $# -lt 2 ]; then
    usage
fi

ACTION="$1"
CONTENT_ADDRESS="$2"

# Sanitize: strip any trailing whitespace/newlines
CONTENT_ADDRESS=$(echo "$CONTENT_ADDRESS" | tr -d '\n\r ')

# Validate address format (56 chars of base32 + .onion)
if ! echo "$CONTENT_ADDRESS" | grep -qE '^[a-z2-7]{56}\.onion$'; then
    echo "ERROR: Invalid .onion address format: $CONTENT_ADDRESS"
    exit 1
fi

# Nickname convention: cellar_ + first 16 chars of address (without .onion)
ADDR_PREFIX=$(echo "$CONTENT_ADDRESS" | sed 's/\.onion$//' | cut -c1-16)
NICKNAME="cellar_${ADDR_PREFIX}"

# Keystore directory for this service
KEYSTORE_DIR="${ARTI_KEYSTORE}/${NICKNAME}"

# Decrypt an .enc file using the master key (AES-256-CBC).
# Format: [16-byte IV][ciphertext with PKCS7 padding]
# Writes decrypted output to $2.
decrypt_file() {
    local enc_file="$1"
    local out_file="$2"
    local master_key_hex

    master_key_hex=$(od -A n -t x1 "$CELLAR_UNLOCKED_FILE" | tr -d ' \n')

    # Extract IV (first 16 bytes)
    local iv_hex
    iv_hex=$(dd if="$enc_file" bs=1 count=16 2>/dev/null | od -A n -t x1 | tr -d ' \n')

    # Ciphertext starts at byte 16
    local file_size ct_size
    file_size=$(wc -c < "$enc_file")
    ct_size=$((file_size - 16))
    dd if="$enc_file" bs=1 skip=16 count="$ct_size" 2>/dev/null | \
        openssl enc -d -aes-256-cbc \
            -K "$master_key_hex" \
            -iv "$iv_hex" \
            -out "$out_file" 2>/dev/null

    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to decrypt ${enc_file}"
        return 1
    fi
    return 0
}

do_takeover() {
    local keys_src="${CELLAR_KEYS_DIR}/${CONTENT_ADDRESS}"

    # Check for encrypted Arti PEM key
    if [ ! -f "${keys_src}/ks_hs_id.ed25519_expanded_private.enc" ]; then
        echo "ERROR: No Arti key found for ${CONTENT_ADDRESS}"
        exit 1
    fi

    # Encrypted key — need master key to be unlocked
    if [ ! -f "$CELLAR_UNLOCKED_FILE" ]; then
        echo "ERROR: Cellar is locked — cannot decrypt keys"
        exit 2
    fi

    # Create the Arti keystore directory for this service
    mkdir -p "$KEYSTORE_DIR"

    # Decrypt the PEM key to the keystore
    if ! decrypt_file "${keys_src}/ks_hs_id.ed25519_expanded_private.enc" "${KEYSTORE_DIR}/ks_hs_id.ed25519_expanded_private"; then
        echo "ERROR: Failed to decrypt key for ${CONTENT_ADDRESS}"
        rm -rf "$KEYSTORE_DIR"
        exit 1
    fi

    # Set correct ownership and permissions
    chown -R arti:arti "$KEYSTORE_DIR" || echo "ERROR: Failed to chown keystore directory $KEYSTORE_DIR"
    chmod 700 "$KEYSTORE_DIR" || echo "ERROR: Failed to chmod keystore directory $KEYSTORE_DIR"
    chmod 600 "${KEYSTORE_DIR}/ks_hs_id.ed25519_expanded_private" || echo "ERROR: Failed to chmod keystore key file"

    # Add onion service config to arti.toml if not already present
    local marker="# cellar:${CONTENT_ADDRESS}"
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

    # Signal Arti to reload configuration
    local arti_pid
    arti_pid=$(pgrep arti)
    if [ -n "$arti_pid" ]; then
        kill -HUP "$arti_pid"
        echo "Sent SIGHUP to Arti (pid $arti_pid)"
    else
        echo "WARNING: Arti process not found, cannot send SIGHUP"
    fi

    echo "Takeover complete for ${CONTENT_ADDRESS}"
}

do_release() {
    # Remove onion service config from arti.toml
    local marker="# cellar:${CONTENT_ADDRESS}"
    if grep -q "$marker" "$ARTI_TOML"; then
        # Remove the marker line and the 3 config lines that follow it
        # (section header, enabled, proxy_ports)
        # Also remove the blank line before the marker if present
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

    # Signal Arti to reload configuration
    local arti_pid
    arti_pid=$(pgrep arti)
    if [ -n "$arti_pid" ]; then
        kill -HUP "$arti_pid"
        echo "Sent SIGHUP to Arti (pid $arti_pid)"
    else
        echo "WARNING: Arti process not found, cannot send SIGHUP"
    fi

    echo "Release complete for ${CONTENT_ADDRESS}"
}

case "$ACTION" in
    takeover)
        do_takeover
        ;;
    release)
        do_release
        ;;
    *)
        usage
        ;;
esac
