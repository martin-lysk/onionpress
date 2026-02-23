#!/bin/bash
# OnionCellar Stress Test — Real Onion Services
# Spins up a separate "worker Tor" container with its own hidden services,
# registers them with the cellar, then tests healthy→failing→takeover→recovery.
#
# Architecture:
#   - Worker Tor container: runs worker hidden services + socat responders
#   - Cellar Tor container: never modified by this script (only cellar-tor-manager.sh
#     writes to its torrc during takeover/release)
#   - This script: reads cellar registry + torrc counts, never writes cellar torrc
#
# Usage:
#   # Quick test — 5 workers (10 onion services: 5 content + 5 healthcheck)
#   ./cellar-stress-test.sh --total 5
#
#   # Custom split: 3 stay healthy, 2 will fail mid-test
#   ./cellar-stress-test.sh --healthy 3 --failing 2
#
#   # Large test — register in batches of 10 to avoid overwhelming Tor
#   ./cellar-stress-test.sh --total 50 --batch-size 10
#
#   # Monitor dashboard (run on the cellar machine)
#   ./cellar-stress-test.sh --mode coordinator
#
#   # Clean up stress-test entries
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
BATCH_SIZE=0          # 0 = register all at once (no batching)
STRESS_VERSION="stress-test"   # marker for cleanup
BASE_PORT=9100                 # port range start inside worker-tor container

DATA_DIR="$HOME/.onionpress"
DOCKER_HOST_SOCK="unix://${DATA_DIR}/colima/default/docker.sock"

# Worker Tor container name
WORKER_TOR_CTR="stress-worker-tor"
WORKER_TOR_IMAGE="alpine:latest"

# ── Parse args ────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --mode)        MODE="$2"; shift 2 ;;
        --total)       TOTAL="$2"; shift 2 ;;
        --healthy)     HEALTHY="$2"; shift 2 ;;
        --failing)     FAILING="$2"; shift 2 ;;
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

if [ "$TOTAL" -lt 1 ]; then
    echo "ERROR: --total must be at least 1"
    exit 1
fi

# ── Docker helper ─────────────────────────────────────────────────────────────
# Prefer the docker bundled with OnionPress.app
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

    # Docker reachable?
    if ! docker_cmd info >/dev/null 2>&1; then
        echo "ERROR: Cannot reach Docker (is Colima running?)"
        echo "  Expected socket: $DOCKER_HOST_SOCK"
        exit 1
    fi

    # Required containers running?
    for ctr in onionpress-tor onionpress-wordpress; do
        if ! docker_cmd inspect --format='{{.State.Running}}' "$ctr" 2>/dev/null | grep -q true; then
            echo "ERROR: Container $ctr is not running"
            exit 1
        fi
    done

    # Cellar polling container should be running on the cellar machine
    if docker_cmd inspect --format='{{.State.Running}}' onionpress-tor-polling 2>/dev/null | grep -q true; then
        log "  onionpress-tor-polling is running (dedicated polling Tor)"
    else
        log "  WARNING: onionpress-tor-polling is not running — cellar polls will use main Tor instance"
    fi

    log "Preflight OK"
}

# ── Auto-detect cellar address ────────────────────────────────────────────────
# Known cellar address (must match CELLAR_ADDRESS in src/cellar.py)
KNOWN_CELLAR_ADDR="ocellarg3xj7hpw25etw34glkjsels5q6knyxe6rmomsjplckwnexdqd.onion"

detect_cellar_addr() {
    if [ -n "$CELLAR_ADDR" ]; then
        return
    fi
    # On the cellar machine itself, auto-detect confirms we're the cellar
    local local_addr
    local_addr=$(docker_cmd exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname 2>/dev/null | tr -d '\n\r ')
    if [ "$local_addr" = "$KNOWN_CELLAR_ADDR" ]; then
        CELLAR_ADDR="$KNOWN_CELLAR_ADDR"
    else
        # On a worker machine, use the known cellar address
        CELLAR_ADDR="$KNOWN_CELLAR_ADDR"
        log "Worker machine detected — targeting cellar at $CELLAR_ADDR"
    fi
    log "Cellar address: $CELLAR_ADDR"
}

# ── Worker Tor container management ──────────────────────────────────────────
# Spins up a separate Tor container for worker hidden services.
# This keeps the cellar's torrc completely untouched.

# Find the OnionPress Docker network
get_onionpress_network() {
    docker_cmd inspect onionpress-tor --format='{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' 2>/dev/null
}

start_worker_tor() {
    log "Starting worker Tor container (${WORKER_TOR_CTR})..."

    # Remove any leftover container
    docker_cmd rm -f "$WORKER_TOR_CTR" 2>/dev/null || true

    # Get the OnionPress network so worker-tor can reach the Tor network
    local network
    network=$(get_onionpress_network)
    if [ -z "$network" ]; then
        log "ERROR: Could not determine OnionPress Docker network"
        exit 1
    fi
    log "  Docker network: $network"

    # Build the worker torrc with all worker hidden services
    local worker_torrc="${OUTPUT_DIR}/worker-torrc"
    cat > "$worker_torrc" << 'TORRC_HEAD'
SocksPort 0
DataDirectory /var/lib/tor/data
TORRC_HEAD

    for i in $(seq 0 $((TOTAL - 1))); do
        local content_port=$((BASE_PORT + i * 2))
        local hc_port=$((BASE_PORT + i * 2 + 1))
        cat >> "$worker_torrc" << EOF

HiddenServiceDir /var/lib/tor/worker${i}-content
HiddenServicePort 80 127.0.0.1:${content_port}
HiddenServiceNumIntroductionPoints 3
HiddenServiceDir /var/lib/tor/worker${i}-healthcheck
HiddenServicePort 80 127.0.0.1:${hc_port}
HiddenServiceNumIntroductionPoints 3
EOF
    done

    # Read the torrc content for embedding in the run command
    local torrc_content
    torrc_content=$(cat "$worker_torrc")

    # Start the container: install tor+socat, write torrc, then run Tor
    docker_cmd run -d \
        --name "$WORKER_TOR_CTR" \
        --network "$network" \
        "$WORKER_TOR_IMAGE" \
        sh -c "
            apk add --no-cache tor socat >/dev/null 2>&1
            mkdir -p /etc/tor /var/lib/tor /var/lib/tor/data
            cat > /etc/tor/torrc << 'EMBEDDED_TORRC'
${torrc_content}
EMBEDDED_TORRC
            chown -R tor:tor /var/lib/tor
            chmod 700 /var/lib/tor
            exec tor -f /etc/tor/torrc --User tor
        " >/dev/null 2>&1

    # Wait for Tor to bootstrap
    log "  Waiting for worker Tor to bootstrap..."
    local deadline=$(($(date +%s) + 120))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if docker_cmd logs "$WORKER_TOR_CTR" 2>&1 | grep -q "Bootstrapped 100%"; then
            log "  Worker Tor bootstrapped"
            break
        fi
        sleep 2
    done

    if ! docker_cmd logs "$WORKER_TOR_CTR" 2>&1 | grep -q "Bootstrapped 100%"; then
        log "ERROR: Worker Tor failed to bootstrap within 120s"
        docker_cmd logs --tail 20 "$WORKER_TOR_CTR" 2>&1
        exit 1
    fi
}

# Wait for all worker hostname files to appear
wait_for_worker_hostnames() {
    log "  Waiting for worker hostname files..."
    local deadline=$(($(date +%s) + 120))

    while [ "$(date +%s)" -lt "$deadline" ]; do
        local all_ready=true
        for i in $(seq 0 $((TOTAL - 1))); do
            if ! docker_cmd exec "$WORKER_TOR_CTR" test -f "/var/lib/tor/worker${i}-content/hostname" 2>/dev/null; then
                all_ready=false
                break
            fi
            if ! docker_cmd exec "$WORKER_TOR_CTR" test -f "/var/lib/tor/worker${i}-healthcheck/hostname" 2>/dev/null; then
                all_ready=false
                break
            fi
        done

        if [ "$all_ready" = true ]; then
            return 0
        fi
        sleep 2
    done

    log "ERROR: Timed out waiting for worker hostname files"
    return 1
}

# Extract worker addresses and keys
extract_worker_info() {
    for i in $(seq 0 $((TOTAL - 1))); do
        local content_addr hc_addr secret_key_b64 public_key_b64
        content_addr=$(docker_cmd exec "$WORKER_TOR_CTR" cat "/var/lib/tor/worker${i}-content/hostname" | tr -d '\r\n ')
        hc_addr=$(docker_cmd exec "$WORKER_TOR_CTR" cat "/var/lib/tor/worker${i}-healthcheck/hostname" | tr -d '\r\n ')
        secret_key_b64=$(docker_cmd exec "$WORKER_TOR_CTR" sh -c "cat '/var/lib/tor/worker${i}-content/hs_ed25519_secret_key' | tail -c 64 | base64 -w0 2>/dev/null || cat '/var/lib/tor/worker${i}-content/hs_ed25519_secret_key' | tail -c 64 | base64")
        public_key_b64=$(docker_cmd exec "$WORKER_TOR_CTR" sh -c "cat '/var/lib/tor/worker${i}-content/hs_ed25519_public_key' | base64 -w0 2>/dev/null || cat '/var/lib/tor/worker${i}-content/hs_ed25519_public_key' | base64")

        echo "${content_addr}" > "${OUTPUT_DIR}/worker${i}.content_addr"
        echo "${hc_addr}" > "${OUTPUT_DIR}/worker${i}.hc_addr"
        echo "${secret_key_b64}" > "${OUTPUT_DIR}/worker${i}.secret_key"
        echo "${public_key_b64}" > "${OUTPUT_DIR}/worker${i}.public_key"

        log "  Worker ${i}: content=${content_addr} hc=${hc_addr}"
    done
}

# ── Register a single address with the cellar ────────────────────────────────
# Uses curl inside the wordpress container through tor's SOCKS proxy
register_address() {
    local content_addr="$1"
    local hc_addr="$2"
    local secret_key="$3"
    local public_key="$4"

    local payload
    payload=$(printf '{"content_address":"%s","healthcheck_address":"%s","secret_key":"%s","public_key":"%s","version":"%s"}' \
        "$content_addr" "$hc_addr" "$secret_key" "$public_key" "$STRESS_VERSION")

    # POST via curl in wordpress container through tor's SOCKS proxy
    local output
    output=$(docker_cmd exec onionpress-wordpress \
        curl -s -X POST \
        --socks5-hostname onionpress-tor:9050 \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --max-time 30 \
        "http://${CELLAR_ADDR}/register" 2>&1) || true

    if echo "$output" | grep -q '"registered".*true'; then
        return 0
    else
        echo "$output" >&2
        return 1
    fi
}

# ── Metrics collection (read-only from cellar) ──────────────────────────────
get_container_mem_mb() {
    local ctr="$1"
    local mem_bytes
    mem_bytes=$(docker_cmd stats --no-stream --format '{{.MemUsage}}' "$ctr" 2>/dev/null | awk '{print $1}')
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

CELLAR_DB_PHP="/var/www/html/wp-content/mu-plugins/onionpress-cellar-db.php"

get_registry_count() {
    docker_cmd exec onionpress-wordpress \
        php "$CELLAR_DB_PHP" count "" 2>/dev/null | tr -d ' \n\r'
}

get_torrc_service_count() {
    docker_cmd exec onionpress-tor \
        sh -c "grep -c '^HiddenServiceDir.*/cellar/' /etc/tor/torrc 2>/dev/null || echo 0" | tr -d ' \n\r'
}

get_stress_fail_count() {
    docker_cmd exec onionpress-wordpress \
        php "$CELLAR_DB_PHP" count "WHERE version='stress-test' AND status='failing'" 2>/dev/null | tr -d ' \n\r'
}

get_takeover_count() {
    docker_cmd exec onionpress-wordpress \
        php "$CELLAR_DB_PHP" count "WHERE takeover_active=1" 2>/dev/null | tr -d ' \n\r'
}

get_healthy_count() {
    docker_cmd exec onionpress-wordpress \
        php "$CELLAR_DB_PHP" count "WHERE version='stress-test' AND status='healthy' AND last_healthcheck IS NOT NULL" 2>/dev/null | tr -d ' \n\r'
}

get_last_poll_duration() {
    grep 'poll pass complete' "$DATA_DIR/onionpress.log" 2>/dev/null | tail -1 | sed 's/.*in //;s/s$//' | tr -d ' \n\r'
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

# ── Dashboard (read-only from cellar) ────────────────────────────────────────
print_dashboard() {
    local reg_count tor_mem wp_mem torrc_svcs fail_count takeover_count healthy_count mem_pct poll_dur
    reg_count=$(get_registry_count)
    tor_mem=$(get_container_mem_mb onionpress-tor)
    wp_mem=$(get_container_mem_mb onionpress-wordpress)
    torrc_svcs=$(get_torrc_service_count)
    fail_count=$(get_stress_fail_count)
    takeover_count=$(get_takeover_count)
    healthy_count=$(get_healthy_count)
    mem_pct=$(get_system_mem_pct)
    poll_dur=$(get_last_poll_duration)

    log "Registry: ${reg_count} entries | Tor mem: ${tor_mem}MB | WP mem: ${wp_mem}MB"
    echo "           Healthy: ${healthy_count} | Failing: ${fail_count} | Taken over: ${takeover_count} | Torrc services: ${torrc_svcs} | VM mem: ${mem_pct}%"
    echo "           Last poll pass: ${poll_dur}s"

    log_json "\"registry_count\":${reg_count},\"tor_mem_mb\":${tor_mem},\"wp_mem_mb\":${wp_mem},\"torrc_services\":${torrc_svcs},\"healthy\":${healthy_count},\"failing\":${fail_count},\"takeovers\":${takeover_count},\"vm_mem_pct\":${mem_pct},\"poll_duration\":\"${poll_dur}\""
}

# ── Phase 1: Create worker onion services ────────────────────────────────────
# Starts a separate Tor container for worker hidden services.
# The cellar's torrc is never touched.

setup_onion_services() {
    log "Phase 1: Creating ${TOTAL} workers (${TOTAL} content + ${TOTAL} healthcheck onion services)..."
    log "  Using separate worker Tor container (cellar torrc untouched)"

    start_worker_tor

    if ! wait_for_worker_hostnames; then
        exit 1
    fi

    extract_worker_info

    log "All ${TOTAL} workers have onion addresses"
}

# ── Phase 2: Start HTTP responders ───────────────────────────────────────────
# socat responders run inside the worker Tor container.

start_responders() {
    log "Phase 2: Starting HTTP responders for all ${TOTAL} workers..."

    for i in $(seq 0 $((TOTAL - 1))); do
        local content_port=$((BASE_PORT + i * 2))
        local hc_port=$((BASE_PORT + i * 2 + 1))

        # Content responder
        docker_cmd exec -d "$WORKER_TOR_CTR" sh -c "
            while true; do
                echo -e 'HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n<html><body>stress-test worker${i}</body></html>' | \
                socat - TCP-LISTEN:${content_port},reuseaddr 2>/dev/null
            done
        "

        # Healthcheck responder
        docker_cmd exec -d "$WORKER_TOR_CTR" sh -c "
            while true; do
                echo -e 'HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n<html><body>OK</body></html>' | \
                socat - TCP-LISTEN:${hc_port},reuseaddr 2>/dev/null
            done
        "
    done

    # Give responders a moment to bind
    sleep 2

    # Verify responders are listening
    local listening=0
    for i in $(seq 0 $((TOTAL - 1))); do
        local content_port=$((BASE_PORT + i * 2))
        if docker_cmd exec "$WORKER_TOR_CTR" sh -c "echo | socat - TCP:127.0.0.1:${content_port},connect-timeout=2" >/dev/null 2>&1; then
            listening=$((listening + 1))
        fi
    done
    log "Verified ${listening}/${TOTAL} content responders are listening"
}

# ── Phase 3: Register with cellar ────────────────────────────────────────────

# Register a range of workers [start, start+count)
register_worker_range() {
    local start="$1"
    local count="$2"
    local registered=0
    local errors=0

    for i in $(seq "$start" $((start + count - 1))); do
        local content_addr hc_addr secret_key public_key
        content_addr=$(cat "${OUTPUT_DIR}/worker${i}.content_addr" | tr -d '\r\n ')
        hc_addr=$(cat "${OUTPUT_DIR}/worker${i}.hc_addr" | tr -d '\r\n ')
        secret_key=$(cat "${OUTPUT_DIR}/worker${i}.secret_key" | tr -d '\r\n ')
        public_key=$(cat "${OUTPUT_DIR}/worker${i}.public_key" | tr -d '\r\n ')

        if register_address "$content_addr" "$hc_addr" "$secret_key" "$public_key"; then
            registered=$((registered + 1))
            log "  Worker ${i}: registered OK"
            log_json "\"event\":\"register\",\"worker\":${i},\"address\":\"${content_addr}\",\"ok\":true"
        else
            errors=$((errors + 1))
            log "  Worker ${i}: registration FAILED"
            log_json "\"event\":\"register\",\"worker\":${i},\"address\":\"${content_addr}\",\"ok\":false"
        fi

        # Brief delay between registrations to avoid overwhelming Tor SOCKS
        if [ "$i" -lt $((start + count - 1)) ]; then
            sleep 2
        fi
    done

    log "  Batch registered: ${registered} OK, ${errors} errors"
    if [ "$errors" -gt 0 ] && [ "$registered" -eq 0 ]; then
        return 1
    fi
    return 0
}

register_workers() {
    log "Phase 3: Registering ${TOTAL} workers with cellar..."
    register_worker_range 0 "$TOTAL"
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        log "ERROR: All registrations failed — aborting"
        exit 1
    fi
}

# ── Phase 4: Wait for healthy baseline ───────────────────────────────────────

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

# ── Phase 5: Trigger failures ─────────────────────────────────────────────────
# Kills responders in the worker Tor container (not the cellar).

stop_responders_for_workers() {
    local start="$1"
    local count="$2"

    log "Phase 5: Stopping responders for workers ${start}..$(( start + count - 1 ))..."

    for i in $(seq "$start" $((start + count - 1))); do
        local hc_port=$((BASE_PORT + i * 2 + 1))
        local content_port=$((BASE_PORT + i * 2))

        # Kill -9 all processes whose command line contains the port number.
        # Must use ps+grep because BusyBox pgrep -f truncates long command lines.
        # Kill parent sh loops AND socat children — parent respawns if not killed.
        docker_cmd exec "$WORKER_TOR_CTR" sh -c "
            ps aux | grep 'TCP-LISTEN:${hc_port}' | grep -v grep | awk '{print \$1}' | xargs kill -9 2>/dev/null
            ps aux | grep 'TCP-LISTEN:${content_port}' | grep -v grep | awk '{print \$1}' | xargs kill -9 2>/dev/null
        " 2>/dev/null || true

        log "  Worker ${i}: responders stopped (ports ${content_port}, ${hc_port})"
    done
}

wait_for_takeover() {
    local expected="$1"
    local timeout_secs="${2:-600}"

    log "Phase 5: Waiting for ${expected} takeovers (timeout: ${timeout_secs}s)..."
    log "  (Cellar needs to detect failures and initiate takeover — this takes several poll cycles)"

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
            log "Phase 5: ${current_takeover} takeovers active (target: ${expected})"
            print_dashboard
            return 0
        fi

        sleep 10
    done

    log "Phase 5: Timed out waiting for takeovers — $(get_takeover_count) active (wanted ${expected})"
    print_dashboard
    return 1
}

# ── Phase 5b: Verify takeover redirects to Wayback Machine ────────────────────
# Uses curl in wordpress container through cellar's SOCKS proxy to check redirects.

WAYBACK_ONION="web.archivep75mbjunhxcn6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"

verify_takeover_redirects() {
    local fail_start="$1"
    local fail_count="$2"

    log "Phase 5b: Verifying takeover redirects to Wayback Machine..."
    log "  (Waiting 120s for Tor descriptor publication...)"
    sleep 120

    local verified=0
    local errors=0
    local retries=3

    for i in $(seq "$fail_start" $((fail_start + fail_count - 1))); do
        local content_addr
        content_addr=$(cat "${OUTPUT_DIR}/worker${i}.content_addr" | tr -d '\r\n ')

        local attempt=0
        local success=false

        while [ "$attempt" -lt "$retries" ]; do
            attempt=$((attempt + 1))

            # Use curl in wordpress container through SOCKS proxy
            local http_code
            http_code=$(docker_cmd exec onionpress-wordpress \
                curl -s -o /dev/null -w "%{http_code}" \
                --socks5-hostname onionpress-tor:9050 \
                --max-time 30 \
                "http://${content_addr}/" 2>/dev/null) || true

            if [ "$http_code" = "302" ]; then
                # Verify Location header
                local headers
                headers=$(docker_cmd exec onionpress-wordpress \
                    curl -s -D - -o /dev/null \
                    --socks5-hostname onionpress-tor:9050 \
                    --max-time 30 \
                    "http://${content_addr}/" 2>/dev/null) || true

                local location
                location=$(echo "$headers" | grep -i "^Location:" | tr -d '\r' | sed 's/^[Ll]ocation: *//')

                if echo "$location" | grep -q "$WAYBACK_ONION"; then
                    log "  Worker ${i}: 302 → Wayback Machine OK"
                    verified=$((verified + 1))
                    success=true
                    log_json "\"event\":\"verify_redirect\",\"worker\":${i},\"address\":\"${content_addr}\",\"ok\":true"
                    break
                else
                    log "  Worker ${i}: 302 but wrong Location: ${location}"
                fi
            else
                log "  Worker ${i}: attempt ${attempt}/${retries} — got HTTP ${http_code} (expected 302)"
                if [ "$attempt" -lt "$retries" ]; then
                    sleep 30
                fi
            fi
        done

        if [ "$success" != true ]; then
            errors=$((errors + 1))
            log "  Worker ${i}: FAILED to verify redirect after ${retries} attempts"
            log_json "\"event\":\"verify_redirect\",\"worker\":${i},\"address\":\"${content_addr}\",\"ok\":false"
        fi
    done

    log "Phase 5b: Redirect verification: ${verified} OK, ${errors} failed"
    return 0
}

# ── Phase 6: Recovery test ────────────────────────────────────────────────────
# Restarts responders in the worker Tor container.

restart_responders_for_workers() {
    local start="$1"
    local count="$2"

    log "Phase 6: Restarting responders for workers ${start}..$(( start + count - 1 ))..."

    for i in $(seq "$start" $((start + count - 1))); do
        local content_port=$((BASE_PORT + i * 2))
        local hc_port=$((BASE_PORT + i * 2 + 1))

        docker_cmd exec -d "$WORKER_TOR_CTR" sh -c "
            while true; do
                echo -e 'HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n<html><body>stress-test worker${i}</body></html>' | \
                socat - TCP-LISTEN:${content_port},reuseaddr 2>/dev/null
            done
        "

        docker_cmd exec -d "$WORKER_TOR_CTR" sh -c "
            while true; do
                echo -e 'HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n<html><body>OK</body></html>' | \
                socat - TCP-LISTEN:${hc_port},reuseaddr 2>/dev/null
            done
        "

        log "  Worker ${i}: responders restarted (ports ${content_port}, ${hc_port})"
    done
}

wait_for_recovery() {
    local expected_healthy="$1"
    local timeout_secs="${2:-600}"

    log "Phase 6: Waiting for recovery — expecting ${expected_healthy} healthy, 0 takeovers (timeout: ${timeout_secs}s)..."

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
            log "Phase 6: Recovery complete — ${current_healthy} healthy, ${current_takeover} takeovers"
            print_dashboard
            return 0
        fi

        sleep 10
    done

    log "Phase 6: Timed out — $(get_healthy_count) healthy, $(get_takeover_count) takeovers"
    print_dashboard
    return 1
}

# ── Phase 7: Cleanup ─────────────────────────────────────────────────────────
# Removes the worker Tor container and cleans cellar registry/keys.
# Cellar torrc takeover entries are cleaned by cellar-tor-manager.sh release,
# which the cellar poller calls when workers recover.

cleanup_stress_test() {
    log "Phase 7: Cleaning up stress test artifacts..."

    # Remove the worker Tor container entirely
    docker_cmd rm -f "$WORKER_TOR_CTR" 2>/dev/null || true
    log "  Removed worker Tor container"

    # Remove stress-test entries from cellar registry
    docker_cmd exec onionpress-wordpress \
        php "$CELLAR_DB_PHP" delete-by-version stress-test 2>/dev/null || true
    log "  Cleaned registry"

    # Remove encrypted key directories for stress-test addresses
    for i in $(seq 0 $((TOTAL - 1))); do
        if [ -f "${OUTPUT_DIR}/worker${i}.content_addr" ]; then
            local addr
            addr=$(cat "${OUTPUT_DIR}/worker${i}.content_addr" | tr -d '\r\n ')
            docker_cmd exec onionpress-wordpress \
                rm -rf "/var/lib/onionpress/cellar/keys/${addr}" 2>/dev/null || true
        fi
    done
    log "  Removed key directories"

    # Remove any cellar takeover entries from cellar's torrc
    # (these were added by cellar-tor-manager.sh during takeover)
    for i in $(seq 0 $((TOTAL - 1))); do
        if [ -f "${OUTPUT_DIR}/worker${i}.content_addr" ]; then
            local addr
            addr=$(cat "${OUTPUT_DIR}/worker${i}.content_addr" | tr -d '\r\n ')
            if docker_cmd exec onionpress-tor grep -q "# cellar:${addr}" /etc/tor/torrc 2>/dev/null; then
                docker_cmd exec onionpress-tor sh -c "
                    awk -v marker='# cellar:${addr}' '
                    BEGIN { skip = 0 }
                    \$0 == marker { skip = 4; next }
                    skip > 0 { skip--; next }
                    { print }
                    ' /etc/tor/torrc > /etc/tor/torrc.tmp && mv /etc/tor/torrc.tmp /etc/tor/torrc
                " 2>/dev/null || true
            fi
            # Remove cellar service directory
            docker_cmd exec onionpress-tor \
                rm -rf "/var/lib/tor/hidden_service/cellar/${addr}" 2>/dev/null || true
        fi
    done

    # SIGHUP cellar Tor to reload (only if we removed takeover entries)
    docker_cmd exec onionpress-tor sh -c "kill -HUP \$(pgrep -x tor)" 2>/dev/null || true
    log "  Cleaned cellar takeover entries and reloaded Tor"

    log "Cleanup complete"
}

# ── Worker mode (full test) ──────────────────────────────────────────────────

run_worker() {
    preflight
    detect_cellar_addr
    mkdir -p "$OUTPUT_DIR"

    log "=== OnionCellar Stress Test ==="
    log "Cellar: ${CELLAR_ADDR}"
    log "Workers: ${TOTAL} total (${HEALTHY} stay healthy, ${FAILING} will fail)"
    if [ "$BATCH_SIZE" -gt 0 ] 2>/dev/null; then
        log "Batch size: ${BATCH_SIZE} (register+stabilize in waves)"
    fi
    log "Worker Tor container: ${WORKER_TOR_CTR}"
    log "Output: ${OUTPUT_DIR}"
    echo ""

    # Trap to ensure cleanup on exit
    trap 'log "Cleaning up before exit..."; cleanup_stress_test' EXIT
    trap 'log "Interrupted..."; exit 130' INT TERM

    # Phase 1: Create worker onion services (separate Tor container)
    setup_onion_services
    echo ""

    # Phase 2: Start responders (inside worker Tor container)
    start_responders
    echo ""

    # Phase 3+4: Register with cellar (batched if --batch-size set)
    if [ "$BATCH_SIZE" -gt 0 ] 2>/dev/null; then
        local batch_start=0
        local batch_num=1
        local total_registered=0
        while [ "$batch_start" -lt "$TOTAL" ]; do
            local batch_count=$BATCH_SIZE
            if [ $((batch_start + batch_count)) -gt "$TOTAL" ]; then
                batch_count=$((TOTAL - batch_start))
            fi

            log "Phase 3: Batch ${batch_num} — registering workers ${batch_start}..$(( batch_start + batch_count - 1 ))"
            if ! register_worker_range "$batch_start" "$batch_count"; then
                log "ERROR: Batch ${batch_num} all registrations failed — aborting"
                exit 1
            fi
            total_registered=$((total_registered + batch_count))
            echo ""

            # Wait for this batch to become healthy before registering next
            log "Phase 4: Waiting for batch ${batch_num} (${total_registered} total) to stabilize..."
            if ! wait_for_healthy "$total_registered" "Phase 4 (batch ${batch_num})" 600; then
                log "WARNING: Batch ${batch_num} not fully healthy, continuing to next batch..."
            fi
            echo ""

            batch_start=$((batch_start + batch_count))
            batch_num=$((batch_num + 1))
        done
        log "All ${TOTAL} workers registered in $((batch_num - 1)) batches"
    else
        # Register all at once
        register_workers
        echo ""

        # Phase 4: Wait for healthy baseline
        if ! wait_for_healthy "$TOTAL" "Phase 4" 600; then
            log "WARNING: Not all workers became healthy, continuing anyway..."
        fi
    fi
    echo ""

    if [ "$FAILING" -gt 0 ]; then
        # Phase 5: Trigger failures (kill responders in worker Tor container)
        local fail_start=$((TOTAL - FAILING))
        stop_responders_for_workers "$fail_start" "$FAILING"
        echo ""

        # Wait for cellar to detect failures and initiate takeover
        if ! wait_for_takeover "$FAILING" 600; then
            log "WARNING: Not all expected takeovers happened, continuing anyway..."
        fi
        echo ""

        # Phase 5b: Verify takeover serves Wayback Machine redirects
        verify_takeover_redirects "$fail_start" "$FAILING"
        echo ""

        # Phase 6: Recovery — restart the failed workers' responders
        restart_responders_for_workers "$fail_start" "$FAILING"
        echo ""

        if ! wait_for_recovery "$TOTAL" 600; then
            log "WARNING: Not all workers recovered"
        fi
        echo ""
    else
        log "No failing workers configured — skipping failure/recovery test"
        echo ""
    fi

    # Final dashboard
    log "=== Final metrics ==="
    print_dashboard
    echo ""

    # Phase 7: Cleanup (disable EXIT trap since we're cleaning up explicitly)
    trap - EXIT
    cleanup_stress_test
    echo ""

    log "=== Stress test complete ==="
    log "Results saved to: ${OUTPUT_DIR}/metrics.jsonl"
}

# ── Coordinator mode (read-only dashboard) ────────────────────────────────────
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

    # Remove worker Tor container if it exists
    if docker_cmd inspect "$WORKER_TOR_CTR" >/dev/null 2>&1; then
        docker_cmd rm -f "$WORKER_TOR_CTR" 2>/dev/null || true
        log "Removed worker Tor container"
    fi

    # Get list of stress-test addresses from registry
    local stress_addrs
    stress_addrs=$(docker_cmd exec onionpress-wordpress \
        php "$CELLAR_DB_PHP" query-addresses "WHERE version='stress-test'" 2>/dev/null) || true

    local count
    count=$(echo "$stress_addrs" | grep -c '\.onion' 2>/dev/null || true)
    [ -z "$count" ] && count=0
    log "Found ${count} stress-test entries to clean up"

    if [ "$count" -eq 0 ]; then
        log "Nothing to clean up"
        return
    fi

    # Delete stress-test entries from SQLite registry
    log "Cleaning registry..."
    docker_cmd exec onionpress-wordpress \
        php "$CELLAR_DB_PHP" delete-by-version stress-test 2>/dev/null || true

    # Remove encrypted key directories
    log "Removing key directories..."
    local removed_keys=0
    for addr in $stress_addrs; do
        addr=$(echo "$addr" | tr -d '\r\n ')
        [ -z "$addr" ] && continue
        docker_cmd exec onionpress-wordpress \
            rm -rf "/var/lib/onionpress/cellar/keys/${addr}" 2>/dev/null || true
        removed_keys=$((removed_keys + 1))
    done
    log "Removed ${removed_keys} key directories"

    # Remove cellar takeover torrc entries (added by cellar-tor-manager.sh)
    log "Cleaning cellar takeover torrc entries..."
    local removed_torrc=0
    for addr in $stress_addrs; do
        addr=$(echo "$addr" | tr -d '\r\n ')
        [ -z "$addr" ] && continue
        if docker_cmd exec onionpress-tor grep -q "# cellar:${addr}" /etc/tor/torrc 2>/dev/null; then
            docker_cmd exec onionpress-tor sh -c "
                awk -v marker='# cellar:${addr}' '
                BEGIN { skip = 0 }
                \$0 == marker { skip = 4; next }
                skip > 0 { skip--; next }
                { print }
                ' /etc/tor/torrc > /etc/tor/torrc.tmp && mv /etc/tor/torrc.tmp /etc/tor/torrc
            " 2>/dev/null || true
            removed_torrc=$((removed_torrc + 1))
        fi
        # Remove cellar service directory
        docker_cmd exec onionpress-tor \
            rm -rf "/var/lib/tor/hidden_service/cellar/${addr}" 2>/dev/null || true
    done

    if [ "$removed_torrc" -gt 0 ]; then
        docker_cmd exec onionpress-tor sh -c "kill -HUP \$(pgrep -x tor)" 2>/dev/null || true
        log "Removed ${removed_torrc} cellar takeover torrc entries, reloaded Tor"
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
