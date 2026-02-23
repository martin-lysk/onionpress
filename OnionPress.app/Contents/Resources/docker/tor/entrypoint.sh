#!/bin/sh
# OnionPress Arti entrypoint
# Creates state directories, starts healthcheck server, launches Arti,
# and writes compat hostname files for existing scripts to read.

# Create Arti state directories with strict permissions (Arti requires o-rx)
mkdir -p /var/lib/arti/cache /var/lib/arti/state
chown -R arti:arti /var/lib/arti
chmod 700 /var/lib/arti /var/lib/arti/cache /var/lib/arti/state

# Create compat directories for hostname files
mkdir -p /var/lib/tor/hidden_service/wordpress
mkdir -p /var/lib/tor/hidden_service/healthcheck

# Write version for healthcheck server
echo "${ONIONPRESS_VERSION:-unknown}" > /var/lib/tor/healthcheck-version

# Forward 127.0.0.1:8080 → wordpress:80 (Arti requires IP, not hostname)
socat TCP-LISTEN:8080,reuseaddr,fork TCP:wordpress:80 &

# Start healthcheck HTTP server in background (port 8081)
/healthcheck-server.sh &

# Start cellar redirect service if cellar mode
if [ "${ONIONPRESS_CELLAR}" = "1" ]; then
    echo "OnionCellar mode: starting redirect service..."
    /cellar-redirect.sh &
fi

# Start Arti in background
su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti.toml" &
ARTI_PID=$!

# Wait for Arti to generate keys, then write compat hostname files
# so existing scripts (healthcheck-server.sh, launcher, menubar.py)
# can read onion addresses from the same paths as before.
write_compat_hostnames() {
    for nickname in wordpress healthcheck; do
        while true; do
            # --nickname must come before the subcommand; run as arti user (not root)
            addr=$(su -s /bin/sh arti -c "arti hss --nickname $nickname onion-address -c /etc/arti/arti.toml" 2>/dev/null)
            if [ -n "$addr" ]; then
                echo "$addr" > "/var/lib/tor/hidden_service/$nickname/hostname"
                echo "Onion address for $nickname: $addr"
                break
            fi
            sleep 2
        done
    done
}
write_compat_hostnames &

# Wait for Arti process
wait $ARTI_PID
