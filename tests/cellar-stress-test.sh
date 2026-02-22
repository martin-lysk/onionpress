#!/bin/bash
# OnionCellar Stress Test
# Tests how many onion addresses a single OnionCellar instance can handle.
#
# Two modes:
#   --mode worker       (default) Generates fake addresses locally and registers them with the cellar
#   --mode coordinator  Monitor-only dashboard — reads registry.json and prints metrics
#
# Usage:
#   # Quick test — register 10 addresses
#   ./cellar-stress-test.sh --total 10
#
#   # Ramp-up until failure (unlimited)
#   ./cellar-stress-test.sh
#
#   # Specify cellar address explicitly
#   ./cellar-stress-test.sh --total 50 --cellar-addr abc...xyz.onion
#
#   # Monitor dashboard (run on the cellar machine)
#   ./cellar-stress-test.sh --mode coordinator
#
#   # Clean up stress-test entries
#   ./cellar-stress-test.sh --cleanup

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
MODE="worker"
TOTAL=0           # 0 = unlimited ramp-up
CELLAR_ADDR=""    # auto-detect from local tor container
DELAY=1           # seconds between registrations (worker mode)
OUTPUT_DIR="./cellar-stress-results"
CLEANUP=false
STRESS_VERSION="stress-test"   # marker for cleanup

DATA_DIR="$HOME/.onionpress"
DOCKER_HOST_SOCK="unix://${DATA_DIR}/colima/default/docker.sock"

# ── Parse args ────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --mode)        MODE="$2"; shift 2 ;;
        --total)       TOTAL="$2"; shift 2 ;;
        --cellar-addr) CELLAR_ADDR="$2"; shift 2 ;;
        --delay)       DELAY="$2"; shift 2 ;;
        --output-dir)  OUTPUT_DIR="$2"; shift 2 ;;
        --cleanup)     CLEANUP=true; shift ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

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

# ── Generate fake .onion address + keys ───────────────────────────────────────
# Produces a valid-format but unreachable address (random base32 + random keys).
generate_fake_address() {
    # 56 chars of base32 (a-z, 2-7) + .onion
    local addr
    addr=$(LC_ALL=C tr -dc 'a-z2-7' < /dev/urandom | head -c 56)
    echo "${addr}.onion"
}

generate_fake_keys() {
    # secret_key: 64 random bytes, base64-encoded
    local secret_key
    secret_key=$(openssl rand -base64 64 | tr -d '\n')

    # public_key: 32 random bytes, base64-encoded
    local public_key
    public_key=$(openssl rand -base64 32 | tr -d '\n')

    echo "$secret_key" "$public_key"
}

# ── Register a single address with the cellar ────────────────────────────────
# Uses wget inside the tor container (per CLAUDE.md: docker exec for all Tor comms)
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
        sh -c "python3 -c \"
import json,sys
try:
    r=json.load(open('/var/lib/onionpress/cellar/registry.json'))
    print(sum(1 for e in r if e.get('version')=='stress-test' and e.get('status')=='failing'))
except: print(0)
\" 2>/dev/null || echo 0" | tr -d ' \n\r'
}

get_takeover_count() {
    docker_cmd exec onionpress-wordpress \
        sh -c "python3 -c \"
import json,sys
try:
    r=json.load(open('/var/lib/onionpress/cellar/registry.json'))
    print(sum(1 for e in r if e.get('takeover_active')))
except: print(0)
\" 2>/dev/null || echo 0" | tr -d ' \n\r'
}

get_healthy_count() {
    docker_cmd exec onionpress-wordpress \
        sh -c "python3 -c \"
import json,sys
try:
    r=json.load(open('/var/lib/onionpress/cellar/registry.json'))
    print(sum(1 for e in r if e.get('status')=='healthy'))
except: print(0)
\" 2>/dev/null || echo 0" | tr -d ' \n\r'
}

get_last_poll_duration() {
    # Extract last poll pass duration from onionpress log
    docker_cmd exec onionpress-wordpress \
        sh -c "grep 'poll pass complete' /var/log/onionpress.log 2>/dev/null | tail -1 | sed 's/.*in //;s/s$//' || echo '?'" | tr -d ' \n\r'
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
    local reg_size reg_count tor_mem wp_mem torrc_svcs fail_count takeover_count healthy_count mem_pct poll_dur
    reg_size=$(get_registry_size)
    reg_count=$(get_registry_count)
    tor_mem=$(get_container_mem_mb onionpress-tor)
    wp_mem=$(get_container_mem_mb onionpress-wordpress)
    torrc_svcs=$(get_torrc_service_count)
    fail_count=$(get_stress_fail_count)
    takeover_count=$(get_takeover_count)
    healthy_count=$(get_healthy_count)
    mem_pct=$(get_system_mem_pct)
    poll_dur=$(get_last_poll_duration)

    # Human-readable registry size
    local reg_size_h
    if [ "$reg_size" -gt 1048576 ] 2>/dev/null; then
        reg_size_h="$((reg_size / 1048576))MB"
    elif [ "$reg_size" -gt 1024 ] 2>/dev/null; then
        reg_size_h="$((reg_size / 1024))KB"
    else
        reg_size_h="${reg_size}B"
    fi

    log "Registry: ${reg_count} entries (${reg_size_h}) | Tor mem: ${tor_mem}MB | WP mem: ${wp_mem}MB"
    echo "           Healthy: ${healthy_count} | Failing: ${fail_count} | Taken over: ${takeover_count} | Torrc services: ${torrc_svcs} | VM mem: ${mem_pct}%"
    echo "           Last poll pass: ${poll_dur}s"

    # JSON log
    log_json "\"registry_count\":${reg_count},\"registry_bytes\":${reg_size},\"tor_mem_mb\":${tor_mem},\"wp_mem_mb\":${wp_mem},\"torrc_services\":${torrc_svcs},\"healthy\":${healthy_count},\"failing\":${fail_count},\"takeovers\":${takeover_count},\"vm_mem_pct\":${mem_pct},\"poll_duration\":\"${poll_dur}\""
}

# ── Worker mode (default) ────────────────────────────────────────────────────
run_worker() {
    preflight
    detect_cellar_addr
    mkdir -p "$OUTPUT_DIR"

    local total_label
    if [ "$TOTAL" -gt 0 ] 2>/dev/null; then
        total_label="$TOTAL"
    else
        total_label="unlimited"
    fi

    log "=== OnionCellar Stress Test (worker) ==="
    log "Cellar: ${CELLAR_ADDR} | Total: ${total_label} | Delay: ${DELAY}s"
    log "Output: ${OUTPUT_DIR}"
    echo ""

    local registered=0
    local errors=0
    local last_dashboard=0

    while true; do
        # Check total limit
        if [ "$TOTAL" -gt 0 ] && [ "$registered" -ge "$TOTAL" ]; then
            log "Reached target of ${TOTAL} registrations"
            break
        fi

        # Generate fake address + keys
        local content_addr hc_addr keys secret_key public_key
        content_addr=$(generate_fake_address)
        hc_addr=$(generate_fake_address)
        keys=$(generate_fake_keys)
        secret_key=$(echo "$keys" | awk '{print $1}')
        public_key=$(echo "$keys" | awk '{print $2}')

        # Register with cellar
        local start_ts end_ts
        start_ts=$(date +%s%N 2>/dev/null || date +%s)

        if register_address "$content_addr" "$hc_addr" "$secret_key" "$public_key"; then
            registered=$((registered + 1))
            end_ts=$(date +%s%N 2>/dev/null || date +%s)
            log "Registered ${content_addr} (${registered}/${total_label})"
            log_json "\"event\":\"register\",\"address\":\"${content_addr}\",\"ok\":true,\"elapsed_ns\":$((end_ts - start_ts))"
            errors=0  # reset consecutive error count on success
        else
            end_ts=$(date +%s%N 2>/dev/null || date +%s)
            errors=$((errors + 1))
            log "ERROR registering ${content_addr} (${errors} consecutive errors)"
            log_json "\"event\":\"register\",\"address\":\"${content_addr}\",\"ok\":false,\"elapsed_ns\":$((end_ts - start_ts))"

            if [ "$errors" -ge 5 ] && [ "$TOTAL" -eq 0 ]; then
                log "Too many consecutive errors — stopping ramp-up"
                break
            fi
        fi

        # Dashboard every 30 seconds
        local now
        now=$(date +%s)
        if [ $((now - last_dashboard)) -ge 30 ]; then
            print_dashboard
            last_dashboard=$now
        fi

        # Safety valve: check Tor container is still running (unlimited mode)
        if [ "$TOTAL" -eq 0 ] && [ $((registered % 10)) -eq 0 ]; then
            if ! docker_cmd inspect --format='{{.State.Running}}' onionpress-tor 2>/dev/null | grep -q true; then
                log "ERROR: Tor container crashed — stopping ramp-up"
                break
            fi
        fi

        sleep "$DELAY"
    done

    echo ""
    log "=== Final metrics ==="
    print_dashboard
    echo ""
    log "Total registered: ${registered} | Errors: ${errors}"
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
        sleep 30
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

    # 1. Get list of stress-test addresses from registry
    local stress_addrs
    stress_addrs=$(docker_cmd exec onionpress-wordpress sh -c "
        python3 -c '
import json
try:
    r = json.load(open(\"/var/lib/onionpress/cellar/registry.json\"))
    for e in r:
        if e.get(\"version\") == \"stress-test\":
            print(e[\"content_address\"])
except: pass
' 2>/dev/null") || true

    local count
    count=$(echo "$stress_addrs" | grep -c '.onion' || echo 0)
    log "Found ${count} stress-test entries to clean up"

    if [ "$count" -eq 0 ]; then
        log "Nothing to clean up"
        return
    fi

    # 2. Filter registry.json — remove stress-test entries
    log "Filtering registry.json..."
    docker_cmd exec onionpress-wordpress sh -c "
        python3 -c '
import json
try:
    r = json.load(open(\"/var/lib/onionpress/cellar/registry.json\"))
    cleaned = [e for e in r if e.get(\"version\") != \"stress-test\"]
    with open(\"/var/lib/onionpress/cellar/registry.json\", \"w\") as f:
        json.dump(cleaned, f, indent=2)
    print(f\"Kept {len(cleaned)} entries, removed {len(r) - len(cleaned)}\")
except Exception as ex:
    print(f\"Error: {ex}\")
'"

    # 3. Remove encrypted key directories for stress-test addresses
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

    # 4. Remove torrc entries for stress-test addresses
    log "Cleaning torrc entries..."
    local removed_torrc=0
    for addr in $stress_addrs; do
        addr=$(echo "$addr" | tr -d '\r\n ')
        [ -z "$addr" ] && continue

        # Check if this address has a torrc entry
        if docker_cmd exec onionpress-tor grep -q "# cellar:${addr}" /etc/tor/torrc 2>/dev/null; then
            # Use awk to remove the marker + 4 config lines (matches fixed cellar-tor-manager.sh)
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
    log "Removed ${removed_torrc} torrc entries"

    # 5. Remove cellar service directories for stress-test addresses
    log "Removing cellar service directories..."
    for addr in $stress_addrs; do
        addr=$(echo "$addr" | tr -d '\r\n ')
        [ -z "$addr" ] && continue
        docker_cmd exec onionpress-tor \
            rm -rf "/var/lib/tor/hidden_service/cellar/${addr}" 2>/dev/null || true
    done

    # 6. Single SIGHUP to Tor to reload config
    if [ "$removed_torrc" -gt 0 ]; then
        log "Sending SIGHUP to Tor..."
        docker_cmd exec onionpress-tor sh -c "kill -HUP \$(pgrep -x tor)" 2>/dev/null || true
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
