#!/bin/bash
# OnionCellar Stress Test — Real Arti Onion Services
#
# Architecture:
#   - Worker containers: each runs Arti with N real onion services + a single
#     Python HTTP server handling all ports (replaces per-worker socat processes).
#   - Each worker self-registers with the cellar over Tor, just like a real
#     OnionPress instance.
#   - Cellar Tor container: never modified by this script.
#   - This script: orchestrates containers, monitors dashboard, controls failures.
#
# Scaling:
#   - Each worker container handles --per-ctr workers (default 50).
#   - For 1000 workers: 20 containers × 50 workers each.
#   - Only 1 docker exec -d per container (vs 2 per worker in old architecture).
#   - macOS process limit is no longer a bottleneck.
#
# Usage:
#   # Quick test — 5 workers in 1 container
#   ./cellar-stress-test.sh --total 5
#
#   # Scale test — 100 workers across 2 containers
#   ./cellar-stress-test.sh --total 100 --per-ctr 50
#
#   # Big test — 1000 workers across 20 containers, start 5 at a time
#   ./cellar-stress-test.sh --total 1000 --per-ctr 50 --batch-size 5
#
#   # Monitor dashboard
#   ./cellar-stress-test.sh --mode coordinator
#
#   # Clean up
#   ./cellar-stress-test.sh --cleanup

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
MODE="worker"
TOTAL=5           # default 5 workers
HEALTHY=""        # auto: half of total
FAILING=""        # auto: half of total
CELLAR_ADDR=""    # auto-detect from local tor container
OUTPUT_DIR="./cellar-stress-results"
CLEANUP=false
PER_CTR=100       # workers per container
BATCH_SIZE=0      # 0 = start all containers at once
STRESS_VERSION="stress-test"
BASE_PORT=9100    # port range start inside each container

DATA_DIR="$HOME/.onionpress"
DOCKER_HOST_SOCK="unix://${DATA_DIR}/colima/default/docker.sock"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Parse args ────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --mode)        MODE="$2"; shift 2 ;;
        --total)       TOTAL="$2"; shift 2 ;;
        --healthy)     HEALTHY="$2"; shift 2 ;;
        --failing)     FAILING="$2"; shift 2 ;;
        --per-ctr)     PER_CTR="$2"; shift 2 ;;
        --cellar-addr) CELLAR_ADDR="$2"; shift 2 ;;
        --output-dir)  OUTPUT_DIR="$2"; shift 2 ;;
        --batch-size)  BATCH_SIZE="$2"; shift 2 ;;
        --cleanup)     CLEANUP=true; shift ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Compute healthy/failing split
if [ -z "$HEALTHY" ] && [ -z "$FAILING" ]; then
    FAILING=$((TOTAL / 2))
    HEALTHY=$((TOTAL - FAILING))
elif [ -z "$HEALTHY" ]; then
    HEALTHY=$((TOTAL - FAILING))
elif [ -z "$FAILING" ]; then
    FAILING=$((TOTAL - HEALTHY))
else
    TOTAL=$((HEALTHY + FAILING))
fi

# Compute number of containers
NUM_CONTAINERS=$(( (TOTAL + PER_CTR - 1) / PER_CTR ))

if [ "$TOTAL" -lt 1 ]; then
    echo "ERROR: --total must be at least 1"
    exit 1
fi

# ── Docker helper ─────────────────────────────────────────────────────────────
DOCKER_BIN="docker"
if [ -x "/Applications/OnionPress.app/Contents/Resources/bin/docker" ]; then
    DOCKER_BIN="/Applications/OnionPress.app/Contents/Resources/bin/docker"
fi

docker_cmd() {
    DOCKER_HOST="$DOCKER_HOST_SOCK" "$DOCKER_BIN" "$@"
}

# ── Logging ───────────────────────────────────────────────────────────────────
log() {
    echo "[$(date '+%H:%M:%S')] $*"
}

log_json() {
    local ts
    ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    echo "{\"ts\":\"$ts\",$1}" >> "$OUTPUT_DIR/metrics.jsonl"
}

# ── Preflight checks ─────────────────────────────────────────────────────────
preflight() {
    log "Preflight checks..."

    if ! docker_cmd info >/dev/null 2>&1; then
        echo "ERROR: Cannot reach Docker (is Colima running?)"
        echo "  Expected socket: $DOCKER_HOST_SOCK"
        exit 1
    fi

    for ctr in onionpress-tor onionpress-wordpress; do
        if ! docker_cmd inspect --format='{{.State.Running}}' "$ctr" 2>/dev/null | grep -q true; then
            echo "ERROR: Container $ctr is not running"
            exit 1
        fi
    done

    if docker_cmd inspect --format='{{.State.Running}}' onioncellar 2>/dev/null | grep -q true; then
        log "  onioncellar is running (dedicated polling Tor)"
    else
        log "  WARNING: onioncellar is not running"
    fi

    # Get the Arti image from the running tor container
    ARTI_IMAGE=$(docker_cmd inspect --format='{{.Config.Image}}' onionpress-tor 2>/dev/null)
    if [ -z "$ARTI_IMAGE" ]; then
        echo "ERROR: Cannot determine Arti image from onionpress-tor container"
        exit 1
    fi
    log "  Arti image: $ARTI_IMAGE"

    # Check if cellar registration API is responding
    local status_check
    status_check=$(docker_cmd exec onioncellar curl -s --max-time 5 http://localhost:8083/status 2>/dev/null || echo "")
    if [ -z "$status_check" ]; then
        echo "ERROR: Cellar registration API is not responding"
        echo "  Check onioncellar container logs"
        exit 1
    fi
    log "  Cellar registration API is ready"

    # Ensure sqlite3 is available in onioncellar (image may not include it yet)
    if ! docker_cmd exec onioncellar sqlite3 --version >/dev/null 2>&1; then
        log "  Installing sqlite3 in onioncellar..."
        docker_cmd exec onioncellar sh -c "apt-get update -qq && apt-get install -y -qq sqlite3 >/dev/null 2>&1"
    fi
    log "  sqlite3 available in onioncellar"

    log "Preflight OK"
}

# ── Auto-detect cellar address ────────────────────────────────────────────────
KNOWN_CELLAR_ADDR="oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"

detect_cellar_addr() {
    if [ -n "$CELLAR_ADDR" ]; then
        return
    fi
    CELLAR_ADDR="$KNOWN_CELLAR_ADDR"
    log "Cellar address: $CELLAR_ADDR"
}

# ── Docker network ────────────────────────────────────────────────────────────
get_onionpress_network() {
    docker_cmd inspect onionpress-tor --format='{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' 2>/dev/null
}

# ── Worker container management ───────────────────────────────────────────────

# Start a single worker container with Arti + Python HTTP server.
# Each container handles $workers_in_ctr onion service pairs.
start_worker_container() {
    local idx="$1"
    local workers_in_ctr="$2"
    local ctr_name="stress-worker-${idx}"
    local network="$3"

    log "  Starting container ${ctr_name} (${workers_in_ctr} workers)..."

    # Remove leftover
    docker_cmd rm -f "$ctr_name" 2>/dev/null || true

    # Generate arti.toml for this container
    local arti_conf="${OUTPUT_DIR}/${ctr_name}-arti.toml"
    cat > "$arti_conf" << 'TOML_HEAD'
[proxy]
socks_listen = "127.0.0.1:9050"

[storage]
cache_dir = "/var/lib/arti/cache"
state_dir = "/var/lib/arti/state"

[storage.keystore]
enabled = true
TOML_HEAD

    for i in $(seq 0 $((workers_in_ctr - 1))); do
        local cp=$((BASE_PORT + i * 2))
        local hp=$((BASE_PORT + i * 2 + 1))
        cat >> "$arti_conf" << EOF

[onion_services."w${idx}_${i}_content"]
enabled = true
proxy_ports = [["80", "127.0.0.1:${cp}"]]

[onion_services."w${idx}_${i}_hc"]
enabled = true
proxy_ports = [["80", "127.0.0.1:${hp}"]]
EOF
    done

    # Start container with sleep (we'll exec the real startup after copying files)
    docker_cmd run -d \
        --name "$ctr_name" \
        --network "$network" \
        --entrypoint sh \
        "$ARTI_IMAGE" \
        -c "sleep infinity" >/dev/null 2>&1

    # Copy files into container
    docker_cmd cp "$arti_conf" "${ctr_name}:/etc/arti/arti.toml"
    docker_cmd cp "${SCRIPT_DIR}/stress/worker-server.py" "${ctr_name}:/worker-server.py"
    docker_cmd cp "${SCRIPT_DIR}/stress/worker-bootstrap.py" "${ctr_name}:/worker-bootstrap.py"

    # Generate startup script
    local startup="${OUTPUT_DIR}/${ctr_name}-start.sh"
    cat > "$startup" << STARTEOF
#!/bin/sh
set -e

# Install Python + curl (Arti image is Debian trixie-slim)
apt-get update -qq && apt-get install -y -qq python3-minimal curl >/dev/null 2>&1

# Fix ownership on config file (docker cp sets host UID)
chown root:root /etc/arti/arti.toml
chmod 644 /etc/arti/arti.toml

# Prepare Arti state dirs
mkdir -p /var/lib/arti/cache /var/lib/arti/state
chown -R arti:arti /var/lib/arti
chmod 700 /var/lib/arti /var/lib/arti/cache /var/lib/arti/state

# Start Python HTTP server (single process handles all ${workers_in_ctr} workers)
python3 /worker-server.py ${BASE_PORT} ${workers_in_ctr} &

# Start Arti
su -s /bin/sh arti -c "arti proxy -c /etc/arti/arti.toml" &
ARTI_PID=\$!

# Wait for Arti keys, then self-register with cellar over Tor
python3 /worker-bootstrap.py "${CELLAR_ADDR}" ${idx} ${workers_in_ctr} ${BASE_PORT} &

wait \$ARTI_PID
STARTEOF
    chmod +x "$startup"
    docker_cmd cp "$startup" "${ctr_name}:/start.sh"

    # Launch the startup script (single docker exec -d per container)
    docker_cmd exec -d "$ctr_name" sh /start.sh
}

# Start all worker containers, optionally in batches.
start_all_workers() {
    local network
    network=$(get_onionpress_network)
    if [ -z "$network" ]; then
        log "ERROR: Could not determine OnionPress Docker network"
        exit 1
    fi
    log "Docker network: $network"

    local remaining=$TOTAL
    local idx=0

    while [ "$remaining" -gt 0 ]; do
        local workers_in_ctr=$PER_CTR
        if [ "$workers_in_ctr" -gt "$remaining" ]; then
            workers_in_ctr=$remaining
        fi

        start_worker_container "$idx" "$workers_in_ctr" "$network"

        remaining=$((remaining - workers_in_ctr))
        idx=$((idx + 1))

        # Batch staggering: wait between batches of containers
        if [ "$BATCH_SIZE" -gt 0 ] && [ $((idx % BATCH_SIZE)) -eq 0 ] && [ "$remaining" -gt 0 ]; then
            log "  Batch of ${BATCH_SIZE} containers started, waiting 30s before next batch..."
            sleep 30
        fi
    done

    log "Started ${NUM_CONTAINERS} worker containers"
}

# Wait for all workers to bootstrap (register with cellar over Tor).
wait_for_bootstrap() {
    local timeout_secs="${1:-900}"
    log "Waiting for all workers to bootstrap and register (timeout: ${timeout_secs}s)..."

    local deadline=$(($(date +%s) + timeout_secs))
    local last_status=0

    while [ "$(date +%s)" -lt "$deadline" ]; do
        local all_ready=true
        local ready_count=0
        local registered_count=0

        for idx in $(seq 0 $((NUM_CONTAINERS - 1))); do
            local ctr_name="stress-worker-${idx}"
            if docker_cmd exec "$ctr_name" test -f /worker-info.json 2>/dev/null; then
                ready_count=$((ready_count + 1))
                # Count registered workers in this container
                local reg
                reg=$(docker_cmd exec "$ctr_name" python3 -c "
import json
with open('/worker-info.json') as f:
    w = json.load(f)
print(sum(1 for x in w if x.get('registered')))
" 2>/dev/null || echo 0)
                registered_count=$((registered_count + reg))
            else
                all_ready=false
            fi
        done

        # Also check cellar registry for real-time registration count
        local db_count
        db_count=$(docker_cmd exec onioncellar \
            sqlite3 "$CELLAR_DB_PATH" "SELECT COUNT(*) FROM registry WHERE version='stress-test'" 2>/dev/null | tr -d ' \n\r')
        [ -n "$db_count" ] && registered_count=$db_count

        local now
        now=$(date +%s)
        if [ $((now - last_status)) -ge 10 ]; then
            log "  Bootstrap: ${ready_count}/${NUM_CONTAINERS} containers done, ${registered_count}/${TOTAL} workers registered"
            last_status=$now
        fi

        if [ "$all_ready" = true ]; then
            log "All containers bootstrapped: ${registered_count} workers registered"
            return 0
        fi

        sleep 5
    done

    log "WARNING: Bootstrap timed out — some containers not ready"
    return 1
}

# Extract all worker info from containers into local files.
extract_all_worker_info() {
    log "Extracting worker info from containers..."
    local total_registered=0

    for idx in $(seq 0 $((NUM_CONTAINERS - 1))); do
        local ctr_name="stress-worker-${idx}"
        docker_cmd exec "$ctr_name" cat /worker-info.json > "${OUTPUT_DIR}/worker-${idx}-info.json" 2>/dev/null || true

        # Count registered
        local reg
        reg=$(python3 -c "
import json, sys
try:
    with open('${OUTPUT_DIR}/worker-${idx}-info.json') as f:
        w = json.load(f)
    print(sum(1 for x in w if x.get('registered')))
except: print(0)
" 2>/dev/null || echo 0)
        total_registered=$((total_registered + reg))
    done

    log "Total registered workers: ${total_registered}"
}

# ── Metrics collection ────────────────────────────────────────────────────────

CELLAR_DB_PATH="/var/lib/onionpress/cellar/registry.db"

get_registry_count() {
    docker_cmd exec onioncellar \
        sqlite3 "$CELLAR_DB_PATH" "SELECT COUNT(*) FROM registry" 2>/dev/null | tr -d ' \n\r' || echo "0"
}

get_stress_fail_count() {
    docker_cmd exec onioncellar \
        sqlite3 "$CELLAR_DB_PATH" "SELECT COUNT(*) FROM registry WHERE version='stress-test' AND status='failing'" 2>/dev/null | tr -d ' \n\r' || echo "0"
}

get_takeover_count() {
    docker_cmd exec onioncellar \
        sqlite3 "$CELLAR_DB_PATH" "SELECT COUNT(*) FROM registry WHERE takeover_active=1" 2>/dev/null | tr -d ' \n\r' || echo "0"
}

get_healthy_count() {
    docker_cmd exec onioncellar \
        sqlite3 "$CELLAR_DB_PATH" "SELECT COUNT(*) FROM registry WHERE version='stress-test' AND status='healthy' AND last_contact IS NOT NULL" 2>/dev/null | tr -d ' \n\r' || echo "0"
}

get_container_mem_mb() {
    local ctr="$1"
    local mem_bytes
    mem_bytes=$(docker_cmd stats --no-stream --format '{{.MemUsage}}' "$ctr" 2>/dev/null | awk '{print $1}' || echo "0")
    if echo "$mem_bytes" | grep -qi gib; then
        echo "$mem_bytes" | sed 's/[Gg][Ii][Bb]//' | awk '{printf "%.0f", $1 * 1024}'
    elif echo "$mem_bytes" | grep -qi mib; then
        echo "$mem_bytes" | sed 's/[Mm][Ii][Bb]//' | awk '{printf "%.0f", $1}'
    elif echo "$mem_bytes" | grep -qi kib; then
        echo "$mem_bytes" | sed 's/[Kk][Ii][Bb]//' | awk '{printf "%.0f", $1 / 1024}'
    else
        echo "0"
    fi
}

get_last_poll_duration() {
    docker_cmd logs --tail 100 onioncellar 2>/dev/null | grep 'poll pass complete' | tail -1 | sed 's/.*in //;s/s$//' | tr -d ' \n\r' || echo "0"
}

get_system_mem_pct() {
    local avail total used
    avail=$(docker_cmd exec onionpress-tor sh -c "awk '/MemAvailable/{print \$2}' /proc/meminfo 2>/dev/null" | tr -d ' \n\r')
    total=$(docker_cmd exec onionpress-tor sh -c "awk '/MemTotal/{print \$2}' /proc/meminfo 2>/dev/null" | tr -d ' \n\r')
    if [ -n "$total" ] && [ "$total" -gt 0 ] 2>/dev/null; then
        used=$((total - avail))
        echo "$((used * 100 / total))"
    else
        echo "0"
    fi
}

print_dashboard() {
    local reg_count tor_mem wp_mem fail_count takeover_count healthy_count mem_pct poll_dur
    reg_count=$(get_registry_count)
    tor_mem=$(get_container_mem_mb onionpress-tor)
    wp_mem=$(get_container_mem_mb onionpress-wordpress)
    fail_count=$(get_stress_fail_count)
    takeover_count=$(get_takeover_count)
    healthy_count=$(get_healthy_count)
    mem_pct=$(get_system_mem_pct)
    poll_dur=$(get_last_poll_duration)

    log "Registry: ${reg_count} entries | Tor mem: ${tor_mem}MB | WP mem: ${wp_mem}MB"
    echo "           Healthy: ${healthy_count} | Failing: ${fail_count} | Taken over: ${takeover_count} | VM mem: ${mem_pct}%"
    echo "           Last poll pass: ${poll_dur}s"

    log_json "\"registry_count\":${reg_count:-0},\"tor_mem_mb\":${tor_mem:-0},\"wp_mem_mb\":${wp_mem:-0},\"healthy\":${healthy_count:-0},\"failing\":${fail_count:-0},\"takeovers\":${takeover_count:-0},\"vm_mem_pct\":${mem_pct:-0},\"poll_duration\":\"${poll_dur}\""
}

# ── Phase: Wait for healthy ──────────────────────────────────────────────────

wait_for_healthy() {
    local target="$1"
    local phase_name="$2"
    local timeout_secs="${3:-600}"

    log "${phase_name}: Waiting for ${target} healthy workers (timeout: ${timeout_secs}s)..."

    local deadline=$(($(date +%s) + timeout_secs))
    local last_dashboard=0

    while [ "$(date +%s)" -lt "$deadline" ]; do
        local current_healthy
        current_healthy=$(get_healthy_count)

        local now
        now=$(date +%s)
        if [ $((now - last_dashboard)) -ge 10 ]; then
            print_dashboard
            last_dashboard=$now
        fi

        if [ "$current_healthy" -ge "$target" ] 2>/dev/null; then
            log "${phase_name}: Reached ${current_healthy} healthy workers"
            print_dashboard
            return 0
        fi

        sleep 10
    done

    log "${phase_name}: Timed out — only $(get_healthy_count) healthy (wanted ${target})"
    print_dashboard
    return 1
}

# ── Phase: Trigger failures ──────────────────────────────────────────────────
# Disable HTTP responders via the Python control API in each worker container.

disable_workers() {
    local fail_start="$1"
    local fail_count="$2"

    log "Disabling responders for workers ${fail_start}..$(( fail_start + fail_count - 1 ))..."

    for i in $(seq "$fail_start" $((fail_start + fail_count - 1))); do
        # Figure out which container and local index
        local ctr_idx=$((i / PER_CTR))
        local local_idx=$((i % PER_CTR))
        local ctr_name="stress-worker-${ctr_idx}"
        local cp=$((BASE_PORT + local_idx * 2))
        local hp=$((BASE_PORT + local_idx * 2 + 1))

        # Disable both content and healthcheck ports
        docker_cmd exec "$ctr_name" \
            curl -s -X POST http://127.0.0.1:9000/disable \
            -H "Content-Type: application/json" \
            -d "{\"ports\": [${cp}, ${hp}]}" >/dev/null 2>&1 || true
    done

    log "Disabled ${fail_count} workers"
}

enable_workers() {
    local start="$1"
    local count="$2"

    log "Re-enabling responders and re-registering workers ${start}..$(( start + count - 1 ))..."

    for i in $(seq "$start" $((start + count - 1))); do
        local ctr_idx=$((i / PER_CTR))
        local local_idx=$((i % PER_CTR))
        local ctr_name="stress-worker-${ctr_idx}"
        local cp=$((BASE_PORT + local_idx * 2))
        local hp=$((BASE_PORT + local_idx * 2 + 1))

        # Re-enable HTTP responders
        docker_cmd exec "$ctr_name" \
            curl -s -X POST http://127.0.0.1:9000/enable \
            -H "Content-Type: application/json" \
            -d "{\"ports\": [${cp}, ${hp}]}" >/dev/null 2>&1 || true

        # Re-register with cellar over Tor (like a real OnionPress restart).
        # This triggers immediate release of the taken-over address.
        docker_cmd exec -d "$ctr_name" \
            python3 -c "
import json, subprocess, sys, time
with open('/worker-info.json') as f:
    workers = json.load(f)
w = next((x for x in workers if x.get('local_index') == ${local_idx}), None)
if not w or not w.get('content_address'):
    sys.exit(0)
# Stagger to avoid overwhelming Tor circuit builder (2s per worker)
time.sleep(${local_idx} * 2)
# Re-read PEM from keystore
import base64
nick = 'w${ctr_idx}_${local_idx}_content'
pem_path = f'/var/lib/arti/state/keystore/hss/{nick}/ks_hs_id.ed25519_expanded_private'
try:
    with open(pem_path, 'rb') as f:
        pem_b64 = base64.b64encode(f.read()).decode()
except:
    pem_b64 = ''
# Parse raw keys from worker-info (bootstrap already extracted them)
import struct
with open(pem_path, 'rb') as f:
    pem_data = f.read()
lines = pem_data.decode().strip().splitlines()
b64 = ''.join(l for l in lines if not l.startswith('-----'))
blob = base64.b64decode(b64)
pos = 15
def rs(d, o):
    ln = struct.unpack_from('!I', d, o)[0]
    return d[o+4:o+4+ln], o+4+ln
_, pos = rs(blob, pos)
_, pos = rs(blob, pos)
_, pos = rs(blob, pos)
pos += 4
pub_blob, pos = rs(blob, pos)
_, pp = rs(pub_blob, 0)
pubkey, _ = rs(pub_blob, pp)
priv_blob, pos = rs(blob, pos)
pp = 8
_, pp = rs(priv_blob, pp)
_, pp = rs(priv_blob, pp)
privkey, _ = rs(priv_blob, pp)
payload = json.dumps({
    'content_address': w['content_address'],
    'healthcheck_address': w['healthcheck_address'],
    'secret_key': base64.b64encode(privkey).decode(),
    'public_key': base64.b64encode(pubkey).decode(),
    'arti_key_pem': pem_b64,
    'version': 'stress-test',
})
subprocess.run([
    'curl', '-s', '-X', 'POST',
    '--socks5-hostname', '127.0.0.1:9050',
    '-H', 'Content-Type: application/json',
    '-d', payload,
    '--max-time', '60',
    'http://${CELLAR_ADDR}:8083/register',
], capture_output=True, timeout=75)
print(f'Re-registered {w[\"content_address\"]}')
" 2>/dev/null &
    done

    # Don't wait for re-registrations — they stagger themselves inside the container
    log "Re-enabled ${count} workers, re-registrations staggering over Tor (2s apart)"
}

wait_for_takeover() {
    local expected="$1"
    local timeout_secs="${2:-600}"

    log "Waiting for ${expected} takeovers (timeout: ${timeout_secs}s)..."

    local deadline=$(($(date +%s) + timeout_secs))
    local last_dashboard=0

    while [ "$(date +%s)" -lt "$deadline" ]; do
        local current_takeover
        current_takeover=$(get_takeover_count)

        local now
        now=$(date +%s)
        if [ $((now - last_dashboard)) -ge 10 ]; then
            print_dashboard
            last_dashboard=$now
        fi

        if [ "$current_takeover" -ge "$expected" ] 2>/dev/null; then
            log "Takeover: ${current_takeover} active (target: ${expected})"
            print_dashboard
            return 0
        fi

        sleep 10
    done

    log "Takeover: Timed out — $(get_takeover_count) active (wanted ${expected})"
    print_dashboard
    return 1
}

wait_for_recovery() {
    local expected_healthy="$1"
    local timeout_secs="${2:-600}"

    log "Waiting for recovery — expecting ${expected_healthy} healthy, 0 takeovers (timeout: ${timeout_secs}s)..."

    local deadline=$(($(date +%s) + timeout_secs))
    local last_dashboard=0

    while [ "$(date +%s)" -lt "$deadline" ]; do
        local current_healthy current_takeover
        current_healthy=$(get_healthy_count)
        current_takeover=$(get_takeover_count)

        local now
        now=$(date +%s)
        if [ $((now - last_dashboard)) -ge 10 ]; then
            print_dashboard
            last_dashboard=$now
        fi

        if [ "$current_healthy" -ge "$expected_healthy" ] 2>/dev/null && [ "$current_takeover" -eq 0 ] 2>/dev/null; then
            log "Recovery complete — ${current_healthy} healthy, ${current_takeover} takeovers"
            print_dashboard
            return 0
        fi

        sleep 10
    done

    log "Recovery: Timed out — $(get_healthy_count) healthy, $(get_takeover_count) takeovers"
    print_dashboard
    return 1
}

# ── Graceful offline/online notification ─────────────────────────────────────

# POST /offline to cellar for a range of workers (instant fail_count=10).
# This simulates a real OnionPress instance calling /offline before sleeping/quitting.
notify_offline() {
    local start="$1"
    local count="$2"

    log "Sending /offline notifications for workers ${start}..$(( start + count - 1 ))..."

    local notified=0
    for i in $(seq "$start" $((start + count - 1))); do
        local ctr_idx=$((i / PER_CTR))
        local local_idx=$((i % PER_CTR))
        local info_file="${OUTPUT_DIR}/worker-${ctr_idx}-info.json"

        # Extract content_address from local worker info
        local content_addr
        content_addr=$(python3 -c "
import json, sys
try:
    with open('${info_file}') as f:
        workers = json.load(f)
    w = next((x for x in workers if x.get('local_index') == ${local_idx}), None)
    if w and w.get('content_address'):
        print(w['content_address'])
    else:
        sys.exit(1)
except:
    sys.exit(1)
" 2>/dev/null) || continue

        # POST /offline via localhost inside onioncellar (no auth needed)
        docker_cmd exec onioncellar \
            curl -s -X POST http://localhost:8083/offline \
            -H "Content-Type: application/json" \
            -d "{\"content_address\": \"${content_addr}\"}" >/dev/null 2>&1 || true

        notified=$((notified + 1))
    done

    log "Sent /offline for ${notified} workers"
    log_json "\"event\":\"offline_notify\",\"start\":${start},\"count\":${count},\"notified\":${notified}"
}

# POST /online to cellar for a range of workers (instant reset to healthy).
# This simulates a real OnionPress instance calling /online after waking up.
notify_online() {
    local start="$1"
    local count="$2"

    log "Sending /online notifications for workers ${start}..$(( start + count - 1 ))..."

    local notified=0
    for i in $(seq "$start" $((start + count - 1))); do
        local ctr_idx=$((i / PER_CTR))
        local local_idx=$((i % PER_CTR))
        local info_file="${OUTPUT_DIR}/worker-${ctr_idx}-info.json"

        # Extract content_address from local worker info
        local content_addr
        content_addr=$(python3 -c "
import json, sys
try:
    with open('${info_file}') as f:
        workers = json.load(f)
    w = next((x for x in workers if x.get('local_index') == ${local_idx}), None)
    if w and w.get('content_address'):
        print(w['content_address'])
    else:
        sys.exit(1)
except:
    sys.exit(1)
" 2>/dev/null) || continue

        # POST /online via localhost inside onioncellar (no auth needed)
        docker_cmd exec onioncellar \
            curl -s -X POST http://localhost:8083/online \
            -H "Content-Type: application/json" \
            -d "{\"content_address\": \"${content_addr}\"}" >/dev/null 2>&1 || true

        notified=$((notified + 1))
    done

    log "Sent /online for ${notified} workers"
    log_json "\"event\":\"online_notify\",\"start\":${start},\"count\":${count},\"notified\":${notified}"
}

# Get content addresses that have active takeovers (one per line).
get_taken_over_addresses() {
    docker_cmd exec onioncellar \
        sqlite3 "$CELLAR_DB_PATH" "SELECT content_address FROM registry WHERE takeover_active=1" 2>/dev/null || true
}

# Verify that taken-over addresses serve 302 redirects to the Wayback Machine.
# Samples $sample_size random addresses to keep timing reasonable.
verify_redirects() {
    local phase_label="$1"
    local sample_size="${2:-5}"

    log "${phase_label}: Verifying 302 redirects on sample of taken-over addresses..."

    # Wait for descriptor propagation after takeover
    log "  Waiting 60s for onion descriptor propagation..."
    sleep 60

    local addrs
    addrs=$(get_taken_over_addresses)

    if [ -z "$addrs" ]; then
        log "${phase_label}: No taken-over addresses found — skipping redirect verification"
        log_json "\"event\":\"redirect_verify\",\"phase\":\"${phase_label}\",\"sampled\":0,\"passed\":0,\"failed\":0,\"skipped\":true"
        return 0
    fi

    # Count total taken-over
    local total_taken=0
    while IFS= read -r line; do
        line=$(echo "$line" | tr -d '\r\n ')
        [ -n "$line" ] && total_taken=$((total_taken + 1))
    done <<< "$addrs"

    # Portable random sampling (no sort -R on macOS)
    local sampled_addrs
    sampled_addrs=$(echo "$addrs" | awk 'BEGIN{srand()}{print rand()"\t"$0}' | sort -n | cut -f2 | head -n "$sample_size")

    local sampled=0
    local passed=0
    local failed=0

    while IFS= read -r addr; do
        addr=$(echo "$addr" | tr -d '\r\n ')
        [ -z "$addr" ] && continue
        sampled=$((sampled + 1))

        # Make HTTP request over Tor via onioncellar's SOCKS proxy.
        # This simulates a real external Tor user visiting the taken-over address.
        # Retry up to 3 times with 30s gaps — descriptor propagation through
        # Tor's DHT can take longer than the initial 60s wait.
        local http_response="000"
        local attempt
        for attempt in 1 2 3; do
            http_response=$(docker_cmd exec onioncellar \
                curl -s -o /dev/null -w "%{http_code} %{redirect_url}" \
                --socks5-hostname 127.0.0.1:9050 \
                --max-time 60 \
                "http://${addr}" 2>/dev/null) || http_response="000"
            local code
            code=$(echo "$http_response" | awk '{print $1}')
            [ "$code" != "000" ] && break
            [ "$attempt" -lt 3 ] && { log "  Retry ${attempt}/3 for ${addr} (descriptor not yet available)..."; sleep 30; }
        done

        local http_code redirect_url
        http_code=$(echo "$http_response" | awk '{print $1}')
        redirect_url=$(echo "$http_response" | awk '{print $2}')

        if [ "$http_code" = "302" ]; then
            # Check that redirect points to Wayback Machine
            if echo "$redirect_url" | grep -qi 'web.archive.org\|archivep75mbjunhxcn6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion'; then
                log "  PASS: ${addr} → 302 → ${redirect_url}"
                passed=$((passed + 1))
            else
                log "  FAIL: ${addr} → 302 but redirect to unexpected: ${redirect_url}"
                failed=$((failed + 1))
            fi
        else
            log "  FAIL: ${addr} → HTTP ${http_code} (expected 302)"
            failed=$((failed + 1))
        fi
    done <<< "$sampled_addrs"

    log "${phase_label}: Redirect verification — ${passed}/${sampled} passed, ${failed} failed (${total_taken} total taken over)"
    log_json "\"event\":\"redirect_verify\",\"phase\":\"${phase_label}\",\"total_taken\":${total_taken},\"sampled\":${sampled},\"passed\":${passed},\"failed\":${failed}"
}

# ── Cleanup ───────────────────────────────────────────────────────────────────

cleanup_stress_test() {
    log "Cleaning up stress test artifacts..."

    # Remove all worker containers
    for idx in $(seq 0 $((NUM_CONTAINERS - 1))); do
        docker_cmd rm -f "stress-worker-${idx}" 2>/dev/null || true
    done
    # Also catch any extras
    docker_cmd ps -a --format '{{.Names}}' 2>/dev/null | grep '^stress-worker-' | while read -r ctr; do
        docker_cmd rm -f "$ctr" 2>/dev/null || true
    done || true
    log "  Removed worker containers"

    # Get stress-test addresses before unregistering
    local stress_addrs
    stress_addrs=$(docker_cmd exec onioncellar \
        sqlite3 "$CELLAR_DB_PATH" "SELECT DISTINCT content_address FROM registry WHERE version='stress-test'" 2>/dev/null) || true

    local count=0
    local released_arti=0
    if [ -n "$stress_addrs" ]; then
        for addr in $stress_addrs; do
            addr=$(echo "$addr" | tr -d '\r\n ')
            [ -z "$addr" ] && continue

            # POST /unregister via cellar-server API
            local resp
            resp=$(docker_cmd exec onioncellar \
                curl -s -X POST http://localhost:8083/unregister \
                -H "Content-Type: application/json" \
                -d "{\"content_address\": \"${addr}\"}" 2>/dev/null) || true

            # If takeover was active, release Arti config in tor container
            if echo "$resp" | grep -q '"takeover_was_active":true'; then
                docker_cmd exec onionpress-tor \
                    /cellar-tor-manager.sh release "$addr" 2>/dev/null || true
                released_arti=$((released_arti + 1))
            fi

            count=$((count + 1))
        done
    fi

    log "  Unregistered ${count} entries"

    if [ "$released_arti" -gt 0 ]; then
        log "  Released ${released_arti} Arti takeover entries"
    fi

    log "Cleanup complete"
}

# ── Worker mode (full test) ──────────────────────────────────────────────────

run_worker() {
    preflight
    detect_cellar_addr
    mkdir -p "$OUTPUT_DIR"

    log "=== OnionCellar Stress Test (Arti) ==="
    log "Cellar: ${CELLAR_ADDR}"
    log "Workers: ${TOTAL} total (${HEALTHY} stay healthy, ${FAILING} will fail)"
    log "Containers: ${NUM_CONTAINERS} × ${PER_CTR} workers/container"
    if [ "$BATCH_SIZE" -gt 0 ] 2>/dev/null; then
        log "Container batch size: ${BATCH_SIZE}"
    fi
    log "Output: ${OUTPUT_DIR}"
    echo ""

    trap 'log "Cleaning up before exit..."; cleanup_stress_test' EXIT
    trap 'log "Interrupted..."; exit 130' INT TERM

    # Phase 1: Start worker containers (Arti + Python HTTP server)
    log "Phase 1: Starting ${NUM_CONTAINERS} worker containers..."
    start_all_workers
    echo ""

    # Phase 2: Wait for all workers to bootstrap and self-register over Tor
    log "Phase 2: Waiting for workers to bootstrap and register over Tor..."
    if ! wait_for_bootstrap 900; then
        log "WARNING: Not all workers bootstrapped"
    fi
    extract_all_worker_info
    echo ""

    # Phase 3: Wait for cellar poller to confirm workers are healthy
    if ! wait_for_healthy "$TOTAL" "Phase 3" 600; then
        log "WARNING: Not all workers became healthy, continuing anyway..."
    fi
    echo ""

    if [ "$FAILING" -gt 0 ]; then
        local fail_start=$((TOTAL - FAILING))

        # ── Graceful offline/online lifecycle ──

        # Phase 4: Graceful offline — POST /offline + disable responders
        # This matches real behavior: instance calls /offline then shuts down
        log "Phase 4: Graceful offline — sending /offline + disabling responders for ${FAILING} workers..."
        notify_offline "$fail_start" "$FAILING"
        disable_workers "$fail_start" "$FAILING"
        echo ""

        # Phase 5: Wait for cellar to takeover (from graceful offline)
        log "Phase 5: Waiting for takeovers from graceful offline..."
        if ! wait_for_takeover "$FAILING" 600; then
            log "WARNING: Not all expected takeovers happened"
        fi
        echo ""

        # Phase 5b: Verify 302 redirects on taken-over addresses
        verify_redirects "Phase 5b" 5
        echo ""

        # Phase 6: Graceful recovery — re-enable responders + POST /online
        # Re-enable HTTP first (so healthchecks pass), then /online (instant reset)
        log "Phase 6: Graceful recovery — re-enabling responders + sending /online..."
        enable_workers "$fail_start" "$FAILING"
        notify_online "$fail_start" "$FAILING"
        echo ""

        # Phase 6b: Wait for recovery from graceful offline
        log "Phase 6b: Waiting for recovery from graceful offline..."
        if ! wait_for_recovery "$TOTAL" 600; then
            log "WARNING: Not all workers recovered from graceful offline"
        fi
        echo ""

        # ── Crash simulation (existing behavior, renumbered) ──

        # Phase 7: Crash simulation — disable responders only (no /offline)
        log "Phase 7: Crash simulation — disabling responders for ${FAILING} workers (no /offline)..."
        disable_workers "$fail_start" "$FAILING"
        echo ""

        # Phase 8: Wait for cellar to detect failures and takeover
        log "Phase 8: Waiting for takeovers from crash simulation..."
        if ! wait_for_takeover "$FAILING" 600; then
            log "WARNING: Not all expected takeovers happened"
        fi
        echo ""

        # Phase 8b: Verify 302 redirects again (after crash takeover)
        verify_redirects "Phase 8b" 5
        echo ""

        # Phase 9: Recovery — workers come back online and re-register over Tor
        # (just like a real OnionPress instance restarting)
        log "Phase 9: Workers coming back online — re-enabling + re-registering over Tor..."
        enable_workers "$fail_start" "$FAILING"
        echo ""

        if ! wait_for_recovery "$TOTAL" 600; then
            log "WARNING: Not all workers recovered"
        fi
        echo ""
    else
        log "No failing workers configured — skipping failure/recovery test"
        echo ""
    fi

    # Phase 10: Final dashboard + cleanup
    log "=== Phase 10: Final metrics ==="
    print_dashboard
    echo ""

    # Cleanup
    trap - EXIT
    cleanup_stress_test
    echo ""

    log "=== Stress test complete ==="
    log "Results saved to: ${OUTPUT_DIR}/metrics.jsonl"
}

# ── Coordinator mode ──────────────────────────────────────────────────────────
run_coordinator() {
    preflight
    mkdir -p "$OUTPUT_DIR"

    log "=== OnionCellar Stress Test (coordinator — read-only monitor) ==="
    log "Output: ${OUTPUT_DIR}"
    log "Press Ctrl-C to stop"
    echo ""

    while true; do
        print_dashboard
        sleep 10
    done
}

# ── Cleanup mode ──────────────────────────────────────────────────────────────
run_cleanup() {
    log "=== OnionCellar Stress Test Cleanup ==="

    if ! docker_cmd info >/dev/null 2>&1; then
        echo "ERROR: Cannot reach Docker"
        exit 1
    fi

    # Remove all stress-worker containers
    docker_cmd ps -a --format '{{.Names}}' 2>/dev/null | grep '^stress-worker-' | while read -r ctr; do
        docker_cmd rm -f "$ctr" 2>/dev/null || true
        log "Removed container: $ctr"
    done || true

    # Also remove old-style container
    docker_cmd rm -f stress-worker-tor 2>/dev/null || true

    # Get stress-test addresses
    local stress_addrs
    stress_addrs=$(docker_cmd exec onioncellar \
        sqlite3 "$CELLAR_DB_PATH" "SELECT DISTINCT content_address FROM registry WHERE version='stress-test'" 2>/dev/null) || true

    local count
    count=$(echo "$stress_addrs" | grep -c '\.onion' 2>/dev/null || true)
    [ -z "$count" ] && count=0
    log "Found ${count} stress-test entries to clean up"

    if [ "$count" -eq 0 ]; then
        log "Nothing to clean up"
        return
    fi

    # Unregister each entry via cellar-server API
    local unregistered=0
    local released_arti=0
    for addr in $stress_addrs; do
        addr=$(echo "$addr" | tr -d '\r\n ')
        [ -z "$addr" ] && continue

        # POST /unregister via cellar-server API
        local resp
        resp=$(docker_cmd exec onioncellar \
            curl -s -X POST http://localhost:8083/unregister \
            -H "Content-Type: application/json" \
            -d "{\"content_address\": \"${addr}\"}" 2>/dev/null) || true

        # If takeover was active, release Arti config in tor container
        if echo "$resp" | grep -q '"takeover_was_active":true'; then
            docker_cmd exec onionpress-tor \
                /cellar-tor-manager.sh release "$addr" 2>/dev/null || true
            released_arti=$((released_arti + 1))
        fi

        unregistered=$((unregistered + 1))
    done

    log "Unregistered ${unregistered} entries"

    if [ "$released_arti" -gt 0 ]; then
        log "Released ${released_arti} Arti takeover entries"
    fi

    echo ""
    log "Cleanup complete: ${count} stress-test entries removed"
}

# ── Main dispatch ─────────────────────────────────────────────────────────────
if [ "$CLEANUP" = true ]; then
    run_cleanup
    exit 0
fi

case "$MODE" in
    worker)      run_worker ;;
    coordinator) run_coordinator ;;
    *)
        echo "Unknown mode: $MODE (use 'worker' or 'coordinator')"
        exit 1
        ;;
esac
