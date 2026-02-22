#!/bin/bash
# OnionCellar Stress Test — Real Onion Services
# Creates real Tor onion services, registers them with the cellar using real keys,
# then tests the healthy→failing→takeover→recovery cycle.
#
# Usage:
#   # Quick test — 5 workers (10 onion services: 5 content + 5 healthcheck)
#   ./cellar-stress-test.sh --total 5
#
#   # Custom split: 3 stay healthy, 2 will fail mid-test
#   ./cellar-stress-test.sh --healthy 3 --failing 2
#
#   # Specify cellar address explicitly
#   ./cellar-stress-test.sh --total 5 --cellar-addr abc...xyz.onion
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
STRESS_VERSION="stress-test"   # marker for cleanup
BASE_PORT=9100                 # port range start inside tor container

DATA_DIR="$HOME/.onionpress"
DOCKER_HOST_SOCK="unix://${DATA_DIR}/colima/default/docker.sock"

# ── Parse args ────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --mode)        MODE="$2"; shift 2 ;;
        --total)       TOTAL="$2"; shift 2 ;;
        --healthy)     HEALTHY="$2"; shift 2 ;;
        --failing)     FAILING="$2"; shift 2 ;;
        --cellar-addr) CELLAR_ADDR="$2"; shift 2 ;;
        --output-dir)  OUTPUT_DIR="$2"; shift 2 ;;
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

    # Check socat is available in tor container
    if ! docker_cmd exec onionpress-tor which socat >/dev/null 2>&1; then
        log "Installing socat in tor container..."
        docker_cmd exec onionpress-tor apk add --no-cache socat >/dev/null 2>&1
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

# ── Metrics collection ────────────────────────────────────────────────────────
get_container_mem_mb() {
    local ctr="$1"
    local mem_bytes
    mem_bytes=$(docker_cmd stats --no-stream --format '{{.MemUsage}}' "$ctr" 2>/dev/null | awk '{print $1}')
    # mem_bytes is like "45.2MiB" or "1.2GiB"
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

get_registry_size() {
    docker_cmd exec onionpress-wordpress \
        sh -c "wc -c < /var/lib/onionpress/cellar/registry.json 2>/dev/null || echo 0" | tr -d ' \n\r'
}

get_registry_count() {
    docker_cmd exec onionpress-wordpress \
        sh -c "grep -c 'content_address' /var/lib/onionpress/cellar/registry.json 2>/dev/null || echo 0" | tr -d ' \n\r'
}

get_torrc_service_count() {
    docker_cmd exec onionpress-tor \
        sh -c "grep -c '^HiddenServiceDir.*/cellar/' /etc/tor/torrc 2>/dev/null || echo 0" | tr -d ' \n\r'
}

get_stress_fail_count() {
    docker_cmd exec onionpress-wordpress \
        sh -c "php -r '
\$r = json_decode(file_get_contents(\"/var/lib/onionpress/cellar/registry.json\"), true);
if (!is_array(\$r)) { echo 0; exit; }
\$c = 0;
foreach (\$r as \$e) { if ((\$e[\"version\"] ?? \"\") === \"stress-test\" && (\$e[\"status\"] ?? \"\") === \"failing\") \$c++; }
echo \$c;
' 2>/dev/null || echo 0" | tr -d ' \n\r'
}

get_takeover_count() {
    docker_cmd exec onionpress-wordpress \
        sh -c "php -r '
\$r = json_decode(file_get_contents(\"/var/lib/onionpress/cellar/registry.json\"), true);
if (!is_array(\$r)) { echo 0; exit; }
\$c = 0;
foreach (\$r as \$e) { if (!empty(\$e[\"takeover_active\"])) \$c++; }
echo \$c;
' 2>/dev/null || echo 0" | tr -d ' \n\r'
}

get_healthy_count() {
    docker_cmd exec onionpress-wordpress \
        sh -c "php -r '
\$r = json_decode(file_get_contents(\"/var/lib/onionpress/cellar/registry.json\"), true);
if (!is_array(\$r)) { echo 0; exit; }
\$c = 0;
foreach (\$r as \$e) { if ((\$e[\"version\"] ?? \"\") === \"stress-test\" && (\$e[\"status\"] ?? \"\") === \"healthy\") \$c++; }
echo \$c;
' 2>/dev/null || echo 0" | tr -d ' \n\r'
}

get_last_poll_duration() {
    # Extract last poll pass duration from onionpress log (on host, not in container)
    grep 'poll pass complete' "$DATA_DIR/onionpress.log" 2>/dev/null | tail -1 | sed 's/.*in //;s/s$//' | tr -d ' \n\r'
}

get_system_mem_pct() {
    # Colima VM memory usage as percentage
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

# ── Dashboard ─────────────────────────────────────────────────────────────────
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

    # JSON log
    log_json "\"registry_count\":${reg_count},\"tor_mem_mb\":${tor_mem},\"wp_mem_mb\":${wp_mem},\"torrc_services\":${torrc_svcs},\"healthy\":${healthy_count},\"failing\":${fail_count},\"takeovers\":${takeover_count},\"vm_mem_pct\":${mem_pct},\"poll_duration\":\"${poll_dur}\""
}

# ── Phase 1: Create real onion services ──────────────────────────────────────
# Creates HiddenServiceDir entries in torrc, SIGHUPs Tor, waits for hostname files.
# Each worker gets 2 services: workerN-content (port 80) and workerN-healthcheck (port 80).
# The socat responders will listen on unique ports that Tor maps to port 80.

STRESS_DIR="/var/lib/tor/hidden_service/stress-test"

setup_onion_services() {
    log "Phase 1: Creating ${TOTAL} workers (${TOTAL} content + ${TOTAL} healthcheck onion services)..."

    # Clean any leftover stress-test entries from torrc (prevents duplicates)
    docker_cmd exec onionpress-tor sh -c "
        awk '
        /^# stress-test:/ { skip=1; next }
        /^HiddenServiceDir.*stress-test/ { skip=1; next }
        skip && /^(HiddenServicePort|HiddenServiceNumIntroductionPoints)/ { next }
        { skip=0; print }
        ' /etc/tor/torrc > /etc/tor/torrc.tmp && mv /etc/tor/torrc.tmp /etc/tor/torrc
    " 2>/dev/null || true

    # Remove old stress-test service directories
    docker_cmd exec onionpress-tor rm -rf "$STRESS_DIR" 2>/dev/null || true

    # Create fresh stress-test directory
    docker_cmd exec onionpress-tor mkdir -p "$STRESS_DIR"
    docker_cmd exec onionpress-tor chown -R tor:nogroup "$STRESS_DIR" 2>/dev/null \
        || docker_cmd exec onionpress-tor chown -R tor:tor "$STRESS_DIR" 2>/dev/null \
        || true

    # Build all HiddenServiceDir entries in one batch
    local torrc_additions=""
    for i in $(seq 0 $((TOTAL - 1))); do
        local content_port=$((BASE_PORT + i * 2))
        local hc_port=$((BASE_PORT + i * 2 + 1))
        local content_dir="${STRESS_DIR}/worker${i}-content"
        local hc_dir="${STRESS_DIR}/worker${i}-healthcheck"

        torrc_additions="${torrc_additions}
# stress-test: worker${i}-content
HiddenServiceDir ${content_dir}
HiddenServicePort 80 127.0.0.1:${content_port}
HiddenServiceNumIntroductionPoints 3
# stress-test: worker${i}-healthcheck
HiddenServiceDir ${hc_dir}
HiddenServicePort 80 127.0.0.1:${hc_port}
HiddenServiceNumIntroductionPoints 3"
    done

    # Append to torrc
    docker_cmd exec onionpress-tor sh -c "cat >> /etc/tor/torrc << 'TORRC_EOF'
${torrc_additions}
TORRC_EOF"

    # SIGHUP Tor to pick up new services
    log "Sending SIGHUP to Tor (generating keys for ${TOTAL} workers)..."
    docker_cmd exec onionpress-tor sh -c "kill -HUP \$(pgrep -x tor)"

    # Wait for all hostname files to appear (up to 5 minutes)
    log "Waiting for Tor to generate onion addresses (up to 5 min)..."
    local deadline=$(($(date +%s) + 300))
    local all_ready=false
    while [ "$(date +%s)" -lt "$deadline" ]; do
        all_ready=true
        for i in $(seq 0 $((TOTAL - 1))); do
            if ! docker_cmd exec onionpress-tor test -f "${STRESS_DIR}/worker${i}-content/hostname" 2>/dev/null; then
                all_ready=false
                break
            fi
            if ! docker_cmd exec onionpress-tor test -f "${STRESS_DIR}/worker${i}-healthcheck/hostname" 2>/dev/null; then
                all_ready=false
                break
            fi
        done
        if [ "$all_ready" = true ]; then
            break
        fi
        sleep 2
    done

    if [ "$all_ready" != true ]; then
        log "ERROR: Timed out waiting for Tor to generate all onion addresses"
        log "Check Tor logs: docker logs onionpress-tor --tail 20"
        exit 1  # EXIT trap will run cleanup
    fi

    log "All ${TOTAL} workers have onion addresses"

    # Extract addresses and keys into arrays
    # We store them in files in OUTPUT_DIR for later phases
    for i in $(seq 0 $((TOTAL - 1))); do
        local content_addr hc_addr secret_key_b64 public_key_b64

        content_addr=$(docker_cmd exec onionpress-tor cat "${STRESS_DIR}/worker${i}-content/hostname" | tr -d '\r\n ')
        hc_addr=$(docker_cmd exec onionpress-tor cat "${STRESS_DIR}/worker${i}-healthcheck/hostname" | tr -d '\r\n ')

        # Extract keys: secret key is 96 bytes (32-byte header + 64-byte key), we send only the 64-byte key
        # Public key is 64 bytes (32-byte header + 32-byte key), we send the full 64 bytes (server accepts both)
        secret_key_b64=$(docker_cmd exec onionpress-tor sh -c "cat '${STRESS_DIR}/worker${i}-content/hs_ed25519_secret_key' | tail -c 64 | base64 -w0 2>/dev/null || cat '${STRESS_DIR}/worker${i}-content/hs_ed25519_secret_key' | tail -c 64 | base64")
        public_key_b64=$(docker_cmd exec onionpress-tor sh -c "cat '${STRESS_DIR}/worker${i}-content/hs_ed25519_public_key' | base64 -w0 2>/dev/null || cat '${STRESS_DIR}/worker${i}-content/hs_ed25519_public_key' | base64")

        # Save worker info
        echo "${content_addr}" > "${OUTPUT_DIR}/worker${i}.content_addr"
        echo "${hc_addr}" > "${OUTPUT_DIR}/worker${i}.hc_addr"
        echo "${secret_key_b64}" > "${OUTPUT_DIR}/worker${i}.secret_key"
        echo "${public_key_b64}" > "${OUTPUT_DIR}/worker${i}.public_key"

        log "  Worker ${i}: content=${content_addr} hc=${hc_addr}"
    done
}

# ── Phase 2: Start HTTP responders ───────────────────────────────────────────
# socat responders that return HTTP 200 on the ports Tor maps to.

start_responders() {
    log "Phase 2: Starting HTTP responders for all ${TOTAL} workers..."

    for i in $(seq 0 $((TOTAL - 1))); do
        local content_port=$((BASE_PORT + i * 2))
        local hc_port=$((BASE_PORT + i * 2 + 1))

        # Content responder
        docker_cmd exec -d onionpress-tor sh -c "
            while true; do
                echo -e 'HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n<html><body>stress-test worker${i}</body></html>' | \
                socat - TCP-LISTEN:${content_port},reuseaddr 2>/dev/null
            done
        "

        # Healthcheck responder
        docker_cmd exec -d onionpress-tor sh -c "
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
        if docker_cmd exec onionpress-tor sh -c "echo | socat - TCP:127.0.0.1:${content_port},connect-timeout=2" >/dev/null 2>&1; then
            listening=$((listening + 1))
        fi
    done
    log "Verified ${listening}/${TOTAL} content responders are listening"
}

# ── Phase 3: Register with cellar ────────────────────────────────────────────

register_workers() {
    log "Phase 3: Registering ${TOTAL} workers with cellar..."

    local registered=0
    local errors=0

    for i in $(seq 0 $((TOTAL - 1))); do
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
    done

    log "Registration complete: ${registered} OK, ${errors} errors"
    if [ "$errors" -gt 0 ] && [ "$registered" -eq 0 ]; then
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

        # Dashboard every 30 seconds
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

stop_responders_for_workers() {
    local start="$1"
    local count="$2"

    log "Phase 5: Stopping healthcheck responders for workers ${start}..$(( start + count - 1 ))..."

    for i in $(seq "$start" $((start + count - 1))); do
        local hc_port=$((BASE_PORT + i * 2 + 1))
        local content_port=$((BASE_PORT + i * 2))

        # Kill socat processes listening on these ports
        docker_cmd exec onionpress-tor sh -c "
            for pid in \$(pgrep -f 'TCP-LISTEN:${hc_port}' 2>/dev/null); do kill \$pid 2>/dev/null; done
            for pid in \$(pgrep -f 'TCP-LISTEN:${content_port}' 2>/dev/null); do kill \$pid 2>/dev/null; done
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
        local current_takeover current_failing
        current_takeover=$(get_takeover_count)
        current_failing=$(get_stress_fail_count)

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
# After takeover, the cellar serves 302 redirects to the Wayback Machine.
# Access each taken-over .onion address and verify the redirect.

WAYBACK_ONION="archivep75mbjunhxcn6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"

verify_takeover_redirects() {
    local fail_start="$1"
    local fail_count="$2"

    log "Phase 5b: Verifying takeover redirects to Wayback Machine..."
    log "  (Waiting 60s for Tor descriptor publication...)"
    sleep 60

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

            # Use wget inside tor container to fetch the taken-over address
            # --max-redirect=0 prevents following the redirect so we can inspect it
            # wget returns exit code 8 for server error responses (3xx counts)
            local output
            output=$(docker_cmd exec onionpress-tor \
                wget -q -S --max-redirect=0 -O /dev/null \
                "http://${content_addr}/" 2>&1) || true

            # Check for 302 redirect
            if echo "$output" | grep -q "302 Found"; then
                # Verify Location header points to Wayback Machine
                local location
                location=$(echo "$output" | grep -i "Location:" | tr -d '\r' | sed 's/.*Location: *//')

                if echo "$location" | grep -q "$WAYBACK_ONION"; then
                    log "  Worker ${i}: 302 → Wayback Machine OK"
                    log "    Location: ${location}"
                    verified=$((verified + 1))
                    success=true
                    log_json "\"event\":\"verify_redirect\",\"worker\":${i},\"address\":\"${content_addr}\",\"ok\":true,\"location\":\"${location}\""
                    break
                else
                    log "  Worker ${i}: 302 but wrong Location: ${location}"
                fi
            else
                log "  Worker ${i}: attempt ${attempt}/${retries} — no 302 yet (descriptor may not be published)"
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

restart_responders_for_workers() {
    local start="$1"
    local count="$2"

    log "Phase 6: Restarting responders for workers ${start}..$(( start + count - 1 ))..."

    for i in $(seq "$start" $((start + count - 1))); do
        local content_port=$((BASE_PORT + i * 2))
        local hc_port=$((BASE_PORT + i * 2 + 1))

        # Content responder
        docker_cmd exec -d onionpress-tor sh -c "
            while true; do
                echo -e 'HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n<html><body>stress-test worker${i}</body></html>' | \
                socat - TCP-LISTEN:${content_port},reuseaddr 2>/dev/null
            done
        "

        # Healthcheck responder
        docker_cmd exec -d onionpress-tor sh -c "
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

cleanup_stress_test() {
    log "Phase 7: Cleaning up stress test artifacts..."

    # Kill all socat responders for our port range
    local max_port=$((BASE_PORT + TOTAL * 2))
    for port in $(seq $BASE_PORT $max_port); do
        docker_cmd exec onionpress-tor sh -c "
            for pid in \$(pgrep -f 'TCP-LISTEN:${port}' 2>/dev/null); do kill \$pid 2>/dev/null; done
        " 2>/dev/null || true
    done
    log "  Killed socat responders"

    # Remove stress-test entries from registry
    docker_cmd exec onionpress-wordpress sh -c "php -r '
\$f = \"/var/lib/onionpress/cellar/registry.json\";
\$r = json_decode(file_get_contents(\$f), true);
if (!is_array(\$r)) { echo \"No registry found\"; exit; }
\$cleaned = array_values(array_filter(\$r, function(\$e) { return (\$e[\"version\"] ?? \"\") !== \"stress-test\"; }));
file_put_contents(\$f, json_encode(\$cleaned, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));
echo \"Kept \" . count(\$cleaned) . \" entries, removed \" . (count(\$r) - count(\$cleaned));
'" 2>/dev/null || true
    log "  Cleaned registry"

    # Remove key directories for stress-test addresses
    for i in $(seq 0 $((TOTAL - 1))); do
        if [ -f "${OUTPUT_DIR}/worker${i}.content_addr" ]; then
            local addr
            addr=$(cat "${OUTPUT_DIR}/worker${i}.content_addr" | tr -d '\r\n ')
            docker_cmd exec onionpress-wordpress \
                rm -rf "/var/lib/onionpress/cellar/keys/${addr}" 2>/dev/null || true
        fi
    done
    log "  Removed key directories"

    # Remove stress-test torrc entries and service directories
    docker_cmd exec onionpress-tor sh -c "
        # Remove all stress-test lines from torrc
        sed -i '/# stress-test:/d' /etc/tor/torrc
        sed -i '\|HiddenServiceDir.*/stress-test/|d' /etc/tor/torrc
        sed -i '/^HiddenServicePort.*/{N;s/\n//;};' /etc/tor/torrc 2>/dev/null || true
    " 2>/dev/null || true

    # More precise torrc cleanup: remove orphaned HiddenServicePort lines
    # that follow a deleted HiddenServiceDir (stress-test)
    docker_cmd exec onionpress-tor sh -c "
        awk '
        /^# stress-test:/ { skip=1; next }
        /^HiddenServiceDir.*stress-test/ { skip=1; next }
        skip && /^(HiddenServicePort|HiddenServiceNumIntroductionPoints)/ { next }
        { skip=0; print }
        ' /etc/tor/torrc > /etc/tor/torrc.tmp && mv /etc/tor/torrc.tmp /etc/tor/torrc
    " 2>/dev/null || true

    # Remove stress-test service directories
    docker_cmd exec onionpress-tor rm -rf "$STRESS_DIR" 2>/dev/null || true
    log "  Removed torrc entries and service directories"

    # SIGHUP Tor to reload
    docker_cmd exec onionpress-tor sh -c "kill -HUP \$(pgrep -x tor)" 2>/dev/null || true
    log "  Tor reloaded"

    log "Cleanup complete"
}

# ── Worker mode (real services test) ──────────────────────────────────────────

run_worker() {
    preflight
    detect_cellar_addr
    mkdir -p "$OUTPUT_DIR"

    log "=== OnionCellar Stress Test (real onion services) ==="
    log "Cellar: ${CELLAR_ADDR}"
    log "Workers: ${TOTAL} total (${HEALTHY} stay healthy, ${FAILING} will fail)"
    log "Port range: ${BASE_PORT}–$((BASE_PORT + TOTAL * 2 - 1))"
    log "Output: ${OUTPUT_DIR}"
    echo ""

    # Trap to ensure cleanup on exit (INT/TERM for Ctrl-C, EXIT for errors)
    trap 'log "Cleaning up before exit..."; cleanup_stress_test' EXIT
    trap 'log "Interrupted..."; exit 130' INT TERM

    # Phase 1: Create onion services
    setup_onion_services
    echo ""

    # Phase 2: Start responders
    start_responders
    echo ""

    # Phase 3: Register with cellar
    register_workers
    echo ""

    # Phase 4: Wait for healthy baseline
    if ! wait_for_healthy "$TOTAL" "Phase 4" 600; then
        log "WARNING: Not all workers became healthy, continuing anyway..."
    fi
    echo ""

    if [ "$FAILING" -gt 0 ]; then
        # Phase 5: Trigger failures (fail the last N workers)
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

# ── Coordinator mode (monitor-only dashboard) ────────────────────────────────
run_coordinator() {
    preflight
    mkdir -p "$OUTPUT_DIR"

    log "=== OnionCellar Stress Test (coordinator — monitor only) ==="
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

    # Check Docker is reachable
    if ! docker_cmd info >/dev/null 2>&1; then
        echo "ERROR: Cannot reach Docker"
        exit 1
    fi

    # 1. Kill all socat stress-test responders
    log "Killing socat responders..."
    docker_cmd exec onionpress-tor sh -c "pkill -f 'TCP-LISTEN:9[0-9][0-9][0-9]' 2>/dev/null" || true

    # 2. Get list of stress-test addresses from registry
    local stress_addrs
    stress_addrs=$(docker_cmd exec onionpress-wordpress sh -c "php -r '
\$r = json_decode(file_get_contents(\"/var/lib/onionpress/cellar/registry.json\"), true);
if (!is_array(\$r)) exit;
foreach (\$r as \$e) { if ((\$e[\"version\"] ?? \"\") === \"stress-test\") echo \$e[\"content_address\"] . \"\\n\"; }
'" 2>/dev/null) || true

    local count
    count=$(echo "$stress_addrs" | grep -c '\.onion' 2>/dev/null || true)
    [ -z "$count" ] && count=0
    log "Found ${count} stress-test entries to clean up"

    if [ "$count" -eq 0 ] && ! docker_cmd exec onionpress-tor test -d "$STRESS_DIR" 2>/dev/null; then
        log "Nothing to clean up"
        return
    fi

    # 3. Filter registry.json — remove stress-test entries
    log "Filtering registry.json..."
    docker_cmd exec onionpress-wordpress sh -c "php -r '
\$f = \"/var/lib/onionpress/cellar/registry.json\";
\$r = json_decode(file_get_contents(\$f), true);
if (!is_array(\$r)) { echo \"No registry found\"; exit; }
\$cleaned = array_values(array_filter(\$r, function(\$e) { return (\$e[\"version\"] ?? \"\") !== \"stress-test\"; }));
file_put_contents(\$f, json_encode(\$cleaned, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));
echo \"Kept \" . count(\$cleaned) . \" entries, removed \" . (count(\$r) - count(\$cleaned));
'"

    # 4. Remove encrypted key directories for stress-test addresses
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

    # 5. Remove torrc entries for stress-test addresses (individual cellar entries)
    log "Cleaning torrc entries..."
    local removed_torrc=0
    for addr in $stress_addrs; do
        addr=$(echo "$addr" | tr -d '\r\n ')
        [ -z "$addr" ] && continue

        # Check if this address has a torrc entry (cellar takeover entries)
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
    done

    # Also remove stress-test service entries from torrc
    docker_cmd exec onionpress-tor sh -c "
        awk '
        /^# stress-test:/ { skip=1; next }
        /^HiddenServiceDir.*stress-test/ { skip=1; next }
        skip && /^(HiddenServicePort|HiddenServiceNumIntroductionPoints)/ { next }
        { skip=0; print }
        ' /etc/tor/torrc > /etc/tor/torrc.tmp && mv /etc/tor/torrc.tmp /etc/tor/torrc
    " 2>/dev/null || true
    log "Removed ${removed_torrc} cellar torrc entries + stress-test service entries"

    # 6. Remove cellar service directories for stress-test addresses
    log "Removing cellar service directories..."
    for addr in $stress_addrs; do
        addr=$(echo "$addr" | tr -d '\r\n ')
        [ -z "$addr" ] && continue
        docker_cmd exec onionpress-tor \
            rm -rf "/var/lib/tor/hidden_service/cellar/${addr}" 2>/dev/null || true
    done

    # 7. Remove stress-test service directories
    docker_cmd exec onionpress-tor rm -rf "$STRESS_DIR" 2>/dev/null || true
    log "Removed stress-test service directories"

    # 8. Single SIGHUP to Tor to reload config
    log "Sending SIGHUP to Tor..."
    docker_cmd exec onionpress-tor sh -c "kill -HUP \$(pgrep -x tor)" 2>/dev/null || true

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
