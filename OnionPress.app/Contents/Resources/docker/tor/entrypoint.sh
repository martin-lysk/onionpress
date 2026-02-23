#!/bin/sh
# OnionPress Arti entrypoint
# Creates state directories, starts healthcheck server, launches Arti,
# and writes compat hostname files for existing scripts to read.

# Create Arti state directories with strict permissions (Arti requires o-rx)
mkdir -p /var/lib/arti/cache /var/lib/arti/state
chown -R arti:arti /var/lib/arti
chmod 700 /var/lib/arti /var/lib/arti/cache /var/lib/arti/state

# Polling-only mode
if [ "${POLLING_ONLY}" = "1" ]; then
    if [ "${ONIONPRESS_CELLAR}" = "1" ]; then
        # Cellar polling mode: Arti with keystore (for takeover) + cellar-poller + redirect
        echo "Cellar polling mode: starting Arti (SOCKS + keystore), redirect service, and poller..."

        # Start cellar redirect service in background (port 8082)
        /cellar-redirect.sh &
        CELLAR_REDIRECT_PID=$!
        sleep 1
        if ! kill -0 $CELLAR_REDIRECT_PID 2>/dev/null; then
            echo "ERROR: cellar-redirect.sh failed to start"
        fi

        # Start Arti with cellar config (SOCKS + keystore)
        su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti-cellar.toml" &
        ARTI_PID=$!
        sleep 2
        if ! kill -0 $ARTI_PID 2>/dev/null; then
            echo "ERROR: Arti failed to start — check config at /etc/arti/arti-cellar.toml"
        fi

        # Start cellar poller in background
        python3 /cellar-poller.py &
        POLLER_PID=$!
        sleep 1
        if ! kill -0 $POLLER_PID 2>/dev/null; then
            echo "ERROR: cellar-poller.py failed to start"
        fi

        # Wait on Arti (main process)
        wait $ARTI_PID
        exit $?
    else
        # Plain polling mode: just SOCKS proxy, no onion services
        echo "Polling-only mode: starting Arti SOCKS proxy (no onion services)..."
        exec su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti-polling.toml"
    fi
fi

# Create compat directories for hostname files
mkdir -p /var/lib/tor/hidden_service/wordpress
mkdir -p /var/lib/tor/hidden_service/healthcheck

# Write version for healthcheck server
echo "${ONIONPRESS_VERSION:-unknown}" > /var/lib/tor/healthcheck-version

# Forward 127.0.0.1:8080 → wordpress:80 (Arti requires IP, not hostname)
socat TCP-LISTEN:8080,reuseaddr,fork TCP:wordpress:80 &
SOCAT_PID=$!
sleep 1
if ! kill -0 $SOCAT_PID 2>/dev/null; then
    echo "ERROR: socat (port 8080 forward) failed to start"
fi

# Start healthcheck HTTP server in background (port 8081)
/healthcheck-server.sh &
HC_PID=$!
sleep 1
if ! kill -0 $HC_PID 2>/dev/null; then
    echo "ERROR: healthcheck-server.sh failed to start"
fi

# Start Arti in background
su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti.toml" &
ARTI_PID=$!
sleep 2
if ! kill -0 $ARTI_PID 2>/dev/null; then
    echo "ERROR: Arti failed to start — check config at /etc/arti/arti.toml"
fi

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
