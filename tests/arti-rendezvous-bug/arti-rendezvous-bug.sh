#!/usr/bin/env bash
# Arti Multi-Service Rendezvous Reliability Test
#
# Tests whether a single Arti instance can reliably publish and serve
# multiple onion services simultaneously.
#
# Observations with Arti 2.1.0:
#
# - Descriptor upload to HSDirs works reliably (8/8 per time period),
#   though with 20 services the second time-period uploads sometimes stall.
#
# - With 10 services: consistently 10/10 reachable.
#
# - With 20 services: typically 17-19/20 on first probe pass. Failures
#   are transient rendezvous timeouts that usually recover on retry.
#   Occasionally (possibly under Tor network congestion) failures are
#   persistent — we observed one run at 4/20 with 16 services unreachable
#   even after 5 minutes of retries.
#
# - Publisher-side errors when failures occur:
#     "Problem while accepting rendezvous request: Could not connect
#      rendezvous circuit: Circuit took too long to build"
#
# - Client-side errors:
#     "Rendezvous at <relay> using introduction point #N took too long"
#
# Tested with Arti 2.1.0 on macOS (ARM64) and Linux (x86_64).
#
# Usage:
#   docker build -t arti-test .
#   ./arti-rendezvous-bug.sh [NUM_SERVICES]   (default: 10)
#
# Requires: Docker, bash 3+

set -euo pipefail

NUM="${1:-10}"
IMG="arti-test"
NET="arti-bug-net"
PUB="arti-bug-pub"
CLI="arti-bug-cli"
BASE_PORT=9100

log() { echo "[$(date +%H:%M:%S)] $*"; }

cleanup() {
    log "Cleaning up..."
    docker rm -f "$PUB" "$CLI" 2>/dev/null || true
    docker network rm "$NET" 2>/dev/null || true
    [ -n "${WORK:-}" ] && rm -rf "$WORK"
}
trap cleanup EXIT

if ! docker image inspect "$IMG" > /dev/null 2>&1; then
    log "ERROR: Image '$IMG' not found. Run: docker build -t $IMG ."
    exit 1
fi

WORK=$(mktemp -d)

log "=== Arti Rendezvous Failure Reproducer ==="
log "Services: $NUM   Image: $IMG"
log "Arti version: $(docker run --rm "$IMG" arti --version 2>/dev/null | head -1)"

# ── Publisher config ──────────────────────────────────────────────────────────

cat > "$WORK/pub.toml" << 'EOF'
[proxy]
socks_listen = "127.0.0.1:9050"
[storage]
cache_dir = "/home/arti/.local/share/arti/cache"
state_dir = "/home/arti/.local/share/arti/state"
[storage.keystore]
enabled = true
[logging]
console = "info"
[[logging.files]]
path = "/home/arti/arti.log"
filter = "info,tor_hsservice=debug,tor_circmgr=debug"
EOF

for i in $(seq 0 $((NUM - 1))); do
    cat >> "$WORK/pub.toml" << EOF

[onion_services."svc${i}"]
enabled = true
proxy_ports = [["80", "127.0.0.1:$((BASE_PORT + i))"]]
EOF
done

# ── Client config ─────────────────────────────────────────────────────────────

cat > "$WORK/cli.toml" << 'EOF'
[proxy]
socks_listen = "0.0.0.0:9050"
[storage]
cache_dir = "/home/arti/.local/share/arti/cache"
state_dir = "/home/arti/.local/share/arti/state"
[logging]
console = "info"
[[logging.files]]
path = "/home/arti/arti.log"
filter = "info,tor_hsclient=debug"
EOF

# ── HTTP backend (one listener per service) ───────────────────────────────────

cat > "$WORK/server.py" << EOFS
import http.server, socketserver, threading

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        port = self.server.server_address[1]
        idx = port - $BASE_PORT
        self.wfile.write(f"OK svc{idx}\n".encode())
    def log_message(self, *a):
        pass

for i in range($NUM):
    port = $BASE_PORT + i
    s = socketserver.TCPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()

import time
while True:
    time.sleep(3600)
EOFS

# ── Start containers ──────────────────────────────────────────────────────────

docker rm -f "$PUB" "$CLI" 2>/dev/null || true
docker network rm "$NET" 2>/dev/null || true
docker network create "$NET" > /dev/null

start_container() {
    local name="$1" conf="$2"
    docker run -d --name "$name" --network "$NET" "$IMG" > /dev/null
    docker cp "$conf" "$name:/tmp/arti.toml"
    docker exec "$name" sh -c '
        mv /tmp/arti.toml /home/arti/arti.toml
        chown arti:arti /home/arti/arti.toml
        chmod 700 /home/arti
        mkdir -p /home/arti/.local/share/arti/cache /home/arti/.local/share/arti/state
        chown -R arti:arti /home/arti/.local
        chmod 700 /home/arti/.local/share/arti/state /home/arti/.local/share/arti/cache
    '
    docker exec -d --user arti "$name" arti proxy -c /home/arti/arti.toml
}

log "Starting publisher ($NUM services)..."
start_container "$PUB" "$WORK/pub.toml"

# Start HTTP backends
docker cp "$WORK/server.py" "$PUB:/tmp/server.py"
docker exec "$PUB" sh -c 'mv /tmp/server.py /home/arti/server.py && chown arti:arti /home/arti/server.py'
docker exec -d --user arti "$PUB" python3 /home/arti/server.py

log "Starting client..."
start_container "$CLI" "$WORK/cli.toml"

# ── Wait for bootstrap ────────────────────────────────────────────────────────

log "Waiting for publisher to bootstrap..."
for i in $(seq 1 60); do
    addr=$(docker exec --user arti "$PUB" \
        arti hss --nickname svc0 onion-address -c /home/arti/arti.toml \
        2>/dev/null | tr -d '[:space:]') || true
    if [ -n "$addr" ] && echo "$addr" | grep -q ".onion"; then
        log "  Publisher ready (${i}0s)"
        break
    fi
    [ "$i" -eq 60 ] && { log "ERROR: Publisher did not bootstrap after 600s"; exit 1; }
    sleep 10
done

log "Waiting for client to bootstrap..."
for i in $(seq 1 60); do
    if docker exec "$CLI" curl -sf -o /dev/null \
        --socks5-hostname 127.0.0.1:9050 --max-time 10 \
        http://example.com/ 2>/dev/null; then
        log "  Client ready (${i}0s)"
        break
    fi
    [ "$i" -eq 60 ] && { log "WARNING: Client may not be ready"; }
    sleep 10
done

# ── Collect .onion addresses ──────────────────────────────────────────────────

log "Collecting .onion addresses..."
for i in $(seq 0 $((NUM - 1))); do
    for try in $(seq 1 30); do
        a=$(docker exec --user arti "$PUB" \
            arti hss --nickname "svc${i}" onion-address -c /home/arti/arti.toml \
            2>/dev/null | tr -d '[:space:]') || true
        if [ -n "$a" ] && echo "$a" | grep -q ".onion"; then
            echo "$a" > "$WORK/addr_${i}"
            log "  svc${i}: $a"
            break
        fi
        sleep 2
    done
    if [ ! -f "$WORK/addr_${i}" ]; then
        log "  svc${i}: FAILED to get address"
        echo "FAILED" > "$WORK/addr_${i}"
    fi
done

# ── Wait for descriptor upload ────────────────────────────────────────────────

log "Checking descriptor upload status..."
for attempt in $(seq 1 18); do
    uploaded=$(docker exec "$PUB" cat /home/arti/arti.log 2>/dev/null \
        | grep -c "descriptor uploaded successfully" || true)
    expected=$((NUM * 2))
    if [ "$uploaded" -ge "$expected" ]; then
        log "  All descriptors uploaded ($uploaded confirmations for $NUM services)"
        break
    fi
    log "  $uploaded/$expected upload confirmations so far..."
    [ "$attempt" -eq 18 ] && log "  Proceeding with $uploaded/$expected uploaded"
    sleep 10
done

log "Waiting 90s for descriptor propagation across HSDirs..."
sleep 90

# ── Probe each service ────────────────────────────────────────────────────────

log "Probing $NUM services (one at a time, 10s between each)..."
reachable=0
unreachable=0

for i in $(seq 0 $((NUM - 1))); do
    addr=$(cat "$WORK/addr_${i}")
    [ "$addr" = "FAILED" ] && { echo "no-addr" > "$WORK/result_${i}"; continue; }

    body=$(docker exec "$CLI" curl -s \
        --socks5-hostname 127.0.0.1:9050 \
        --max-time 30 \
        "http://${addr}/" 2>/dev/null) || true

    if echo "$body" | grep -q "OK svc${i}"; then
        echo "ok" > "$WORK/result_${i}"
        reachable=$((reachable + 1))
        log "  svc${i}: REACHABLE"
    else
        echo "fail" > "$WORK/result_${i}"
        unreachable=$((unreachable + 1))
        log "  svc${i}: UNREACHABLE"
    fi
    sleep 10
done

log "First pass: ${reachable}/${NUM} reachable"

# ── Retry unreachable (5 min) ─────────────────────────────────────────────────

if [ "$unreachable" -gt 0 ]; then
    log "Retrying unreachable services for up to 5 minutes..."
    t0=$(date +%s)
    while [ $(( $(date +%s) - t0 )) -lt 300 ]; do
        remaining=0
        for i in $(seq 0 $((NUM - 1))); do
            result=$(cat "$WORK/result_${i}" 2>/dev/null) || true
            [ "$result" != "fail" ] && continue
            addr=$(cat "$WORK/addr_${i}")

            body=$(docker exec "$CLI" curl -s \
                --socks5-hostname 127.0.0.1:9050 \
                --max-time 30 \
                "http://${addr}/" 2>/dev/null) || true

            if echo "$body" | grep -q "OK svc${i}"; then
                echo "ok" > "$WORK/result_${i}"
                reachable=$((reachable + 1))
                unreachable=$((unreachable - 1))
                log "  svc${i}: reachable after $(( $(date +%s) - t0 ))s"
            else
                remaining=$((remaining + 1))
            fi
        done
        [ "$remaining" -eq 0 ] && break
        log "  $(( $(date +%s) - t0 ))s elapsed, $remaining still unreachable"
        sleep 15
    done
fi

# ── Results ───────────────────────────────────────────────────────────────────

echo ""
log "================================================================"
log "RESULTS: $NUM services, $reachable reachable, $unreachable unreachable"
[ "$NUM" -gt 0 ] && log "Failure rate: $(( unreachable * 100 / NUM ))%"
log "================================================================"

if [ "$unreachable" -gt 0 ]; then
    echo ""
    log "Unreachable services:"
    for i in $(seq 0 $((NUM - 1))); do
        result=$(cat "$WORK/result_${i}" 2>/dev/null) || true
        addr=$(cat "$WORK/addr_${i}" 2>/dev/null) || true
        [ "$result" = "fail" ] && log "  svc${i}: ${addr}"
    done

    echo ""
    log "Publisher errors (last 50 WARN/ERROR lines):"
    docker exec "$PUB" cat /home/arti/arti.log 2>/dev/null \
        | grep -E "WARN|ERROR" | tail -50 || log "  (none)"

    echo ""
    log "Client errors (last 50 WARN/ERROR/failure lines):"
    docker exec "$CLI" cat /home/arti/arti.log 2>/dev/null \
        | grep -E "WARN|ERROR|failure" | tail -50 || log "  (none)"
fi

echo ""
log "Done."
