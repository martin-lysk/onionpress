#!/bin/sh
# OnionPress Tor entrypoint
# Supports both Arti (default) and C Tor via TOR_IMPL env var.
# Creates state directories, starts healthcheck server, launches Tor,
# and writes compat hostname files for existing scripts to read.

# Which Tor implementation to use: "tor" (C Tor, default) or "arti"
TOR_IMPL="${TOR_IMPL:-tor}"

# Create Arti state directories with strict permissions (Arti requires o-rx)
mkdir -p /var/lib/arti/cache /var/lib/arti/state

# Persistent Arti log — survives container restarts (on arti-state volume)
ARTI_LOG="/var/lib/arti/arti.log"

# Rotate log if >10MB
rotate_log() {
    if [ -f "$ARTI_LOG" ]; then
        size=$(stat -c%s "$ARTI_LOG" 2>/dev/null || wc -c < "$ARTI_LOG")
        if [ "$size" -gt 10485760 ] 2>/dev/null; then
            mv "$ARTI_LOG" "${ARTI_LOG}.1"
        fi
    fi
}
rotate_log
echo "=== Arti starting at $(date -u '+%Y-%m-%dT%H:%M:%SZ') ===" >> "$ARTI_LOG"

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

# Takeover worker mode — runs in onionheaven-takeover-N containers
if [ "${TAKEOVER_WORKER}" = "1" ]; then
    echo "Takeover worker mode: starting ${TOR_IMPL} (SOCKS + keystore), redirect service, and takeover worker..."
    CONTAINER_NAME="${CONTAINER_NAME:-onionheaven-takeover-unknown}"

    # Start OnionHeaven redirect service in background (port 8082)
    /onionheaven-redirect.sh &
    ONIONHEAVEN_REDIRECT_PID=$!
    sleep 1
    if ! kill -0 $ONIONHEAVEN_REDIRECT_PID 2>/dev/null; then
        echo "ERROR: onionheaven-redirect.sh failed to start"
    fi

    if [ "$TOR_IMPL" = "tor" ]; then
        # C Tor with SOCKS + control port for ADD_ONION/DEL_ONION
        mkdir -p /var/lib/tor
        chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || chown -R tor:tor /var/lib/tor 2>/dev/null || true
        chmod 700 /var/lib/tor
        cat > /etc/tor/torrc << TORRC_EOF
SocksPort 0.0.0.0:9050
ControlPort 127.0.0.1:9051
DataDirectory /var/lib/tor
Log notice stdout
TORRC_EOF
        chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true
        su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc" &
        TOR_PID=$!
        sleep 2
        if ! kill -0 $TOR_PID 2>/dev/null; then
            echo "ERROR: C Tor failed to start"
        fi
    else
        # Start Arti with OnionHeaven config (SOCKS + keystore for takeover services)
        su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti-onionheaven.toml" &
        TOR_PID=$!
        sleep 2
        if ! kill -0 $TOR_PID 2>/dev/null; then
            echo "ERROR: Arti failed to start — check config at /etc/arti/arti-onionheaven.toml"
        fi
    fi

    # Start takeover worker (log to shared volume)
    LOG_FILE="/var/lib/onionpress/onionheaven/takeover-worker-${CONTAINER_NAME}.log"
    CONTAINER_NAME="${CONTAINER_NAME}" python3 /onionheaven-takeover-worker.py 2>"$LOG_FILE" &
    WORKER_PID=$!
    sleep 1
    if ! kill -0 $WORKER_PID 2>/dev/null; then
        echo "ERROR: onionheaven-takeover-worker.py failed to start"
    fi

    # Wait on Tor (main process)
    wait $TOR_PID
    exit $?
fi

# No-onion-service mode (tor-client = SOCKS only, onionheaven = heartbeat/takeover)
if [ "${NO_ONION_SERVICE}" = "1" ]; then
    if [ "${ONIONHEAVEN}" = "1" ]; then
        # OnionHeaven heartbeat/takeover mode: Tor with takeover keystore +
        # heartbeat monitor + redirect. The API server runs in the main tor
        # container — this container only handles monitoring and takeover duties.
        echo "OnionHeaven mode: starting ${TOR_IMPL} (SOCKS + keystore), redirect service, and heartbeat monitor..."

        # Start OnionHeaven redirect service in background (port 8082)
        /onionheaven-redirect.sh &
        ONIONHEAVEN_REDIRECT_PID=$!
        sleep 1
        if ! kill -0 $ONIONHEAVEN_REDIRECT_PID 2>/dev/null; then
            echo "ERROR: onionheaven-redirect.sh failed to start"
        fi

        if [ "$TOR_IMPL" = "tor" ]; then
            # C Tor with control port for ADD_ONION/DEL_ONION (no SIGHUP needed)
            mkdir -p /var/lib/tor
            chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || chown -R tor:tor /var/lib/tor 2>/dev/null || true
            chmod 700 /var/lib/tor
            cat > /etc/tor/torrc << TORRC_EOF
SocksPort 0.0.0.0:9050
ControlPort 127.0.0.1:9051
DataDirectory /var/lib/tor
Log notice stdout
TORRC_EOF
            chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true
        su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc" &
            TOR_PID=$!
            sleep 2
            if ! kill -0 $TOR_PID 2>/dev/null; then
                echo "ERROR: C Tor failed to start"
            fi
        else
            # Start Arti with OnionHeaven config (SOCKS + keystore)
            su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti-onionheaven.toml" &
            TOR_PID=$!
            sleep 2
            if ! kill -0 $TOR_PID 2>/dev/null; then
                echo "ERROR: Arti failed to start — check config at /etc/arti/arti-onionheaven.toml"
            fi
        fi

        # Start onionheaven heartbeat monitor in background (log to shared volume)
        HEARTBEAT_LOG="/var/lib/onionpress/onionheaven/heartbeat.log"
        python3 /onionheaven-heartbeat.py 2>>"$HEARTBEAT_LOG" &
        HEARTBEAT_PID=$!
        sleep 1
        if ! kill -0 $HEARTBEAT_PID 2>/dev/null; then
            echo "ERROR: onionheaven-heartbeat.py failed to start"
        fi

        # Watchdog: restart heartbeat if its log goes stale (stuck/crashed process)
        WATCHDOG_STALE_SECS=300
        WATCHDOG_CHECK_INTERVAL=60
        (
            while true; do
                sleep $WATCHDOG_CHECK_INTERVAL

                # If heartbeat log doesn't exist yet, skip
                [ -f "$HEARTBEAT_LOG" ] || continue

                # Get log file age in seconds
                log_mtime=$(stat -c %Y "$HEARTBEAT_LOG" 2>/dev/null) || continue
                now=$(date +%s)
                age=$(( now - log_mtime ))

                if [ "$age" -gt "$WATCHDOG_STALE_SECS" ]; then
                    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: heartbeat log stale for ${age}s (threshold: ${WATCHDOG_STALE_SECS}s)" >> "$HEARTBEAT_LOG"

                    # Log diagnostics before killing
                    if kill -0 $HEARTBEAT_PID 2>/dev/null; then
                        echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: heartbeat PID $HEARTBEAT_PID is alive but not writing logs" >> "$HEARTBEAT_LOG"
                        wchan=$(cat /proc/$HEARTBEAT_PID/wchan 2>/dev/null || echo "unknown")
                        fdcount=$(ls /proc/$HEARTBEAT_PID/fd 2>/dev/null | wc -l || echo "unknown")
                        echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: PID $HEARTBEAT_PID wchan=$wchan open_fds=$fdcount" >> "$HEARTBEAT_LOG"
                        echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: killing stale heartbeat PID $HEARTBEAT_PID" >> "$HEARTBEAT_LOG"
                        kill $HEARTBEAT_PID 2>/dev/null
                        sleep 2
                        kill -9 $HEARTBEAT_PID 2>/dev/null
                    else
                        echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: heartbeat PID $HEARTBEAT_PID is dead (silent crash)" >> "$HEARTBEAT_LOG"
                    fi

                    # Restart
                    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: restarting heartbeat monitor" >> "$HEARTBEAT_LOG"
                    python3 /onionheaven-heartbeat.py 2>>"$HEARTBEAT_LOG" &
                    HEARTBEAT_PID=$!
                    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WATCHDOG: heartbeat restarted as PID $HEARTBEAT_PID" >> "$HEARTBEAT_LOG"
                fi
            done
        ) &

        # Wait on Tor process (main process)
        wait $TOR_PID
        exit $?
    else
        # SOCKS-only mode (tor-client): just a proxy, no onion services
        if [ "$TOR_IMPL" = "tor" ]; then
            echo "SOCKS-only mode: starting C Tor SOCKS proxy (no onion services)..."
            mkdir -p /var/lib/tor
            chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || chown -R tor:tor /var/lib/tor 2>/dev/null || true
            chmod 700 /var/lib/tor
            # Minimal torrc for SOCKS-only
            cat > /etc/tor/torrc << TORRC_EOF
SocksPort 0.0.0.0:9050
DataDirectory /var/lib/tor
Log notice stdout
TORRC_EOF
            chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true
            su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc"
        else
            echo "SOCKS-only mode: starting Arti SOCKS proxy (no onion services)..."
            su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti-polling.toml" 2>&1 | tee -a "$ARTI_LOG"
        fi
    fi
fi

# Create compat directories for hostname files
mkdir -p /var/lib/tor/hidden_service/wordpress
mkdir -p /var/lib/tor/hidden_service/healthcheck

# Write version for healthcheck server
echo "${ONIONPRESS_VERSION:-unknown}" > /var/lib/tor/healthcheck-version

# Forward 127.0.0.1:8080 → wordpress:80 (both Arti and C Tor need IP targets)
socat TCP-LISTEN:8080,reuseaddr,fork TCP:wordpress:80 &
SOCAT_PID=$!
sleep 1
if ! kill -0 $SOCAT_PID 2>/dev/null; then
    echo "ERROR: socat (port 8080 forward) failed to start"
fi

# OnionHeaven API server — runs on EVERY node so any OnionPress instance
# can accept registrations. The onionheaven container (heartbeat monitor +
# takeover Arti) starts lazily when the first registration arrives.
mkdir -p /var/lib/onionpress/onionheaven/keys
python3 /onionheaven-server.py &
ONIONHEAVEN_SERVER_PID=$!
sleep 1
if ! kill -0 $ONIONHEAVEN_SERVER_PID 2>/dev/null; then
    echo "ERROR: onionheaven-server.py failed to start"
fi

# Start healthcheck HTTP server in background (port 8081)
/healthcheck-server.sh &
HC_PID=$!
sleep 1
if ! kill -0 $HC_PID 2>/dev/null; then
    echo "ERROR: healthcheck-server.sh failed to start"
fi

if [ "$TOR_IMPL" = "tor" ]; then
    # ==================== C Tor mode ====================
    echo "Starting C Tor (TOR_IMPL=tor)..."

    # Create C Tor data directory
    mkdir -p /var/lib/tor
    chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || chown -R tor:tor /var/lib/tor 2>/dev/null || true
    chmod 700 /var/lib/tor

    # Convert Arti keys to C Tor format if Arti keystore exists but C Tor keys don't
    for nickname in wordpress healthcheck; do
        ARTI_KEY="/var/lib/arti/state/keystore/hss/${nickname}/ks_hs_id.ed25519_expanded_private"
        CTOR_DIR="/var/lib/tor/hidden_service/${nickname}"
        CTOR_SECRET="${CTOR_DIR}/hs_ed25519_secret_key"
        if [ -f "$ARTI_KEY" ] && [ ! -f "$CTOR_SECRET" ]; then
            echo "Converting Arti key for $nickname to C Tor format..."
            python3 /key-convert.py arti-to-ctor "$ARTI_KEY" "$CTOR_DIR"
        fi
    done

    # Set ownership on hidden service dirs (C Tor is strict about this)
    for dir in /var/lib/tor/hidden_service/wordpress /var/lib/tor/hidden_service/healthcheck; do
        chown -R debian-tor:debian-tor "$dir" 2>/dev/null || chown -R tor:tor "$dir" 2>/dev/null || true
        chmod 700 "$dir"
    done

    # Generate torrc from template
    cp /etc/tor/torrc.template /etc/tor/torrc

    # Add OnionHeaven API port to WordPress service
    sed -i 's/# __WORDPRESS_API_PORT__/HiddenServicePort 8083 127.0.0.1:8083/' /etc/tor/torrc

    # Every node runs the OnionHeaven API — use max intro points to handle heartbeat traffic
    sed -i 's/HiddenServiceNumIntroductionPoints 3/HiddenServiceNumIntroductionPoints 10/' /etc/tor/torrc

    # Ensure all of /var/lib/tor is owned by debian-tor (C Tor checks this)
    chown -R debian-tor:debian-tor /var/lib/tor 2>/dev/null || true

    # Start C Tor as debian-tor user (log to persistent file + docker logs)
    TOR_LOG="/var/lib/tor/tor.log"
    su -s /bin/sh debian-tor -c "tor -f /etc/tor/torrc" 2>&1 | tee -a "$TOR_LOG" &
    TOR_PID=$!
    sleep 2
    if ! kill -0 $TOR_PID 2>/dev/null; then
        echo "ERROR: C Tor failed to start — check config at /etc/tor/torrc"
    fi

    # C Tor writes hostname files directly — wait for them, then log
    write_ctor_hostnames() {
        for nickname in wordpress healthcheck; do
            local hfile="/var/lib/tor/hidden_service/${nickname}/hostname"
            while [ ! -f "$hfile" ] || [ ! -s "$hfile" ]; do
                sleep 2
            done
            echo "Onion address for $nickname: $(cat "$hfile")"
        done
    }
    write_ctor_hostnames &

    # Wait for C Tor process
    wait $TOR_PID
else
    # ==================== Arti mode (default) ====================

    # Expose port 8083 through the onion service so other nodes can reach the API
    sed -i 's/proxy_ports = \[\["80", "127.0.0.1:8080"\]\]/proxy_ports = [["80", "127.0.0.1:8080"], ["8083", "127.0.0.1:8083"]]/' /etc/arti/arti.toml

    # Every node runs the OnionHeaven API — use max intro points to handle heartbeat traffic
    sed -i 's/num_intro_points = 3/num_intro_points = 10/' /etc/arti/arti.toml

    # Convert C Tor keys to Arti format if switching back from C Tor
    for nickname in wordpress healthcheck; do
        CTOR_SECRET="/var/lib/tor/hidden_service/${nickname}/hs_ed25519_secret_key"
        ARTI_KEY="/var/lib/arti/state/keystore/hss/${nickname}/ks_hs_id.ed25519_expanded_private"
        if [ -f "$CTOR_SECRET" ] && [ ! -f "$ARTI_KEY" ]; then
            echo "Converting C Tor key for $nickname to Arti format..."
            mkdir -p "/var/lib/arti/state/keystore/hss/${nickname}"
            python3 /key-convert.py ctor-to-arti "$CTOR_SECRET" "$ARTI_KEY"
            chown -R arti:arti "/var/lib/arti/state/keystore/hss/${nickname}"
            chmod 700 "/var/lib/arti/state/keystore/hss/${nickname}"
            chmod 600 "$ARTI_KEY"
        fi
    done

    # Start Arti in background (log to persistent file + docker logs)
    su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti.toml" 2>&1 | tee -a "$ARTI_LOG" &
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
fi
