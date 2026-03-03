#!/bin/sh
# OnionPress Arti entrypoint
# Creates state directories, starts healthcheck server, launches Arti,
# and writes compat hostname files for existing scripts to read.

# Create Arti state directories with strict permissions (Arti requires o-rx)
mkdir -p /var/lib/arti/cache /var/lib/arti/state

# Clean ephemeral state that causes "Too many preemptive onion service circuits
# failed" after container restarts. The keystore (identity keys) must survive,
# but guards, circuit timeouts, and intro point state become stale/poisoned
# across restarts and should be rebuilt fresh.
rm -rf /var/lib/arti/cache/*
rm -f /var/lib/arti/state/state/guards.json
rm -f /var/lib/arti/state/state/circuit_timeouts.json
rm -rf /var/lib/arti/state/hss/*/iptreplay/
rm -rf /var/lib/arti/state/hss/*/ipts.json
rm -rf /var/lib/arti/state/hss/*/iptpub.json
rm -rf /var/lib/arti/state/keystore/hss/*/ipts/

chown -R arti:arti /var/lib/arti
chmod 700 /var/lib/arti /var/lib/arti/cache /var/lib/arti/state

# Polling-only mode
if [ "${POLLING_ONLY}" = "1" ]; then
    if [ "${ONIONHEAVEN}" = "1" ]; then
        # OnionHeaven polling mode: Arti with keystore (for takeover) + onionheaven-server + onionheaven-poller + redirect
        echo "OnionHeaven polling mode: starting Arti (SOCKS + keystore), registration server, redirect service, and poller..."

        # Start OnionHeaven redirect service in background (port 8082)
        /onionheaven-redirect.sh &
        ONIONHEAVEN_REDIRECT_PID=$!
        sleep 1
        if ! kill -0 $ONIONHEAVEN_REDIRECT_PID 2>/dev/null; then
            echo "ERROR: onionheaven-redirect.sh failed to start"
        fi

        # Start onionheaven registration API server (port 8083)
        python3 /onionheaven-server.py &
        ONIONHEAVEN_SERVER_PID=$!
        sleep 1
        if ! kill -0 $ONIONHEAVEN_SERVER_PID 2>/dev/null; then
            echo "ERROR: onionheaven-server.py failed to start"
        fi

        # Start Arti with OnionHeaven config (SOCKS + keystore)
        su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti-onionheaven.toml" &
        ARTI_PID=$!
        sleep 2
        if ! kill -0 $ARTI_PID 2>/dev/null; then
            echo "ERROR: Arti failed to start — check config at /etc/arti/arti-onionheaven.toml"
        fi

        # Start onionheaven poller in background
        python3 /onionheaven-poller.py &
        POLLER_PID=$!
        sleep 1
        if ! kill -0 $POLLER_PID 2>/dev/null; then
            echo "ERROR: onionheaven-poller.py failed to start"
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

# OnionHeaven mode: forward port 8083 to onionheaven container's registration API
# and add port 8083 to the onion service config in arti.toml
if [ "${ONIONHEAVEN}" = "1" ]; then
    socat TCP-LISTEN:8083,reuseaddr,fork TCP:onionheaven:8083 &
    SOCAT_API_PID=$!
    sleep 1
    if ! kill -0 $SOCAT_API_PID 2>/dev/null; then
        echo "ERROR: socat (port 8083 forward to onionheaven) failed to start"
    fi
    # Add port 8083 to the wordpress onion service proxy_ports
    sed -i 's/proxy_ports = \[\["80", "127.0.0.1:8080"\]\]/proxy_ports = [["80", "127.0.0.1:8080"], ["8083", "127.0.0.1:8083"]]/' /etc/arti/arti.toml
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
