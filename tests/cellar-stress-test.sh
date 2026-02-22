#!/bin/bash
# OnionCellar Stress Test
# Tests how many onion addresses a single OnionCellar instance can handle.
#
# Two modes:
#   --mode coordinator  (default) Generates fake addresses, distributes to workers or registers directly
#   --mode worker       Receives address batches from coordinator, registers them with the cellar
#
# Usage:
#   # Quick test — register 10 addresses directly (no workers needed)
#   ./cellar-stress-test.sh --total 10
#
#   # Ramp-up until failure
#   ./cellar-stress-test.sh
#
#   # With remote workers
#   ./cellar-stress-test.sh --workers abc...xyz.onion,def...uvw.onion --total 100
#
#   # Worker mode (run on worker machines)
#   ./cellar-stress-test.sh --mode worker --cellar-addr abc...xyz.onion
#
#   # Clean up stress-test entries
#   ./cellar-stress-test.sh --cleanup

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
MODE="coordinator"
TOTAL=0           # 0 = unlimited ramp-up
BATCH_SIZE=10
WORKERS=""        # comma-separated healthcheck .onion addresses
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
        --mode)       MODE="$2"; shift 2 ;;
        --total)      TOTAL="$2"; shift 2 ;;
        --batch-size) BATCH_SIZE="$2"; shift 2 ;;
        --workers)    WORKERS="$2"; shift 2 ;;
        --cellar-addr) CELLAR_ADDR="$2"; shift 2 ;;
        --delay)      DELAY="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --cleanup)    CLEANUP=true; shift ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Docker helper ─────────────────────────────────────────────────────────────
docker_cmd() {
    DOCKER_HOST="$DOCKER_HOST_SOCK" docker "$@"
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

    # Cellar unlocked?
    if ! docker_cmd exec onionpress-tor test -f /var/lib/onionpress/cellar/.master-key-unlocked 2>/dev/null; then
        echo "ERROR: Cellar is locked — log in to WordPress to unlock it first"
        exit 1
    fi

    log "Preflight OK"
}

# ── Auto-detect cellar address ────────────────────────────────────────────────
detect_cellar_addr() {
    if [ -n "$CELLAR_ADDR" ]; then
        return
    fi
    CELLAR_ADDR=$(docker_cmd exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname 2>/dev/null | tr -d '\n\r ')
    if [ -z "$CELLAR_ADDR" ]; then
        echo "ERROR: Could not auto-detect cellar address"
        exit 1
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

    # POST via wget in the tor container — talks to local WordPress on port 80
    local output
    output=$(docker_cmd exec onionpress-tor \
        wget -q -O - --timeout=15 \
        --header="Content-Type: application/json" \
        --post-data="$payload" \
        "http://wordpress:80/register" 2>&1) || true

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

get_system_mem_pct() {
    # Colima VM memory usage as percentage (via docker info)
    local total used
    total=$(docker_cmd info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
    # docker stats gives container-level; use /proc/meminfo inside tor container for VM-level
    local avail
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
    local registered="$1"
    local total_label="$2"

    local reg_size reg_count tor_mem wp_mem torrc_svcs fail_count takeover_count mem_pct
    reg_size=$(get_registry_size)
    reg_count=$(get_registry_count)
    tor_mem=$(get_container_mem_mb onionpress-tor)
    wp_mem=$(get_container_mem_mb onionpress-wordpress)
    torrc_svcs=$(get_torrc_service_count)
    fail_count=$(get_stress_fail_count)
    takeover_count=$(get_takeover_count)
    mem_pct=$(get_system_mem_pct)

    # Human-readable registry size
    local reg_size_h
    if [ "$reg_size" -gt 1048576 ] 2>/dev/null; then
        reg_size_h="$((reg_size / 1048576))MB"
    elif [ "$reg_size" -gt 1024 ] 2>/dev/null; then
        reg_size_h="$((reg_size / 1024))KB"
    else
        reg_size_h="${reg_size}B"
    fi

    printf "\r\033[K"
    log "Registered: ${registered}/${total_label} | Registry: ${reg_size_h} (${reg_count} entries) | Tor mem: ${tor_mem}MB | WP mem: ${wp_mem}MB"
    echo "           Failing: ${fail_count} | Takeovers: ${takeover_count} | Torrc services: ${torrc_svcs} | VM mem: ${mem_pct}%"

    # JSON log
    log_json "\"registered\":${registered},\"total\":\"${total_label}\",\"registry_bytes\":${reg_size},\"registry_count\":${reg_count},\"tor_mem_mb\":${tor_mem},\"wp_mem_mb\":${wp_mem},\"torrc_services\":${torrc_svcs},\"failing\":${fail_count},\"takeovers\":${takeover_count},\"vm_mem_pct\":${mem_pct}"

    # Return non-zero if memory exceeds 80%
    if [ "$mem_pct" -gt 80 ] 2>/dev/null; then
        log "WARNING: VM memory usage at ${mem_pct}% — stopping ramp-up"
        return 1
    fi
    return 0
}

# ── Send batch to a worker via its healthcheck endpoint ───────────────────────
send_batch_to_worker() {
    local worker_addr="$1"
    shift
    # Remaining args are JSON address objects
    local addresses_json="$1"

    local payload
    payload=$(printf '{"type":"stress_test_batch","addresses":%s}' "$addresses_json")

    # POST to the worker's healthcheck endpoint via Tor
    docker_cmd exec onionpress-tor \
        wget -q -O /dev/null --timeout=30 \
        --header="Content-Type: application/json" \
        --post-data="$payload" \
        "http://${worker_addr}/" 2>/dev/null
}

# ── Coordinator mode ──────────────────────────────────────────────────────────
run_coordinator() {
    preflight
    detect_cellar_addr
    mkdir -p "$OUTPUT_DIR"

    local total_label
    if [ "$TOTAL" -gt 0 ] 2>/dev/null; then
        total_label="$TOTAL"
    else
        total_label="unlimited"
    fi

    log "=== OnionCellar Stress Test (coordinator) ==="
    log "Total: ${total_label} | Batch size: ${BATCH_SIZE} | Workers: ${WORKERS:-none (direct)}"
    log "Output: ${OUTPUT_DIR}"
    echo ""

    # Split workers into array
    local -a worker_list=()
    if [ -n "$WORKERS" ]; then
        IFS=',' read -ra worker_list <<< "$WORKERS"
        log "Workers: ${#worker_list[@]} — ${worker_list[*]}"
    fi

    local registered=0
    local errors=0
    local batch_num=0
    local last_dashboard=0
    local worker_idx=0

    while true; do
        # Check total limit
        if [ "$TOTAL" -gt 0 ] && [ "$registered" -ge "$TOTAL" ]; then
            log "Reached target of ${TOTAL} registrations"
            break
        fi

        # Build a batch
        local -a batch_addrs=()
        local batch_json="["
        local first=true
        local i=0
        while [ "$i" -lt "$BATCH_SIZE" ]; do
            if [ "$TOTAL" -gt 0 ] && [ "$((registered + i))" -ge "$TOTAL" ]; then
                break
            fi

            local content_addr hc_addr keys secret_key public_key
            content_addr=$(generate_fake_address)
            hc_addr=$(generate_fake_address)
            keys=$(generate_fake_keys)
            secret_key=$(echo "$keys" | awk '{print $1}')
            public_key=$(echo "$keys" | awk '{print $2}')

            if [ "$first" = true ]; then
                first=false
            else
                batch_json="${batch_json},"
            fi
            batch_json="${batch_json}{\"content_address\":\"${content_addr}\",\"healthcheck_address\":\"${hc_addr}\",\"secret_key\":\"${secret_key}\",\"public_key\":\"${public_key}\"}"

            batch_addrs+=("${content_addr}|${hc_addr}|${secret_key}|${public_key}")
            i=$((i + 1))
        done
        batch_json="${batch_json}]"
        batch_num=$((batch_num + 1))

        if [ ${#batch_addrs[@]} -eq 0 ]; then
            break
        fi

        # Distribute batch
        if [ ${#worker_list[@]} -gt 0 ]; then
            # Send to next worker (round-robin)
            local target_worker="${worker_list[$worker_idx]}"
            worker_idx=$(( (worker_idx + 1) % ${#worker_list[@]} ))

            log "Sending batch #${batch_num} (${#batch_addrs[@]} addrs) to worker ${target_worker}"
            if send_batch_to_worker "$target_worker" "$batch_json"; then
                registered=$((registered + ${#batch_addrs[@]}))
            else
                log "ERROR: Failed to send batch to worker ${target_worker}"
                errors=$((errors + 1))
                if [ "$errors" -ge 5 ] && [ "$TOTAL" -eq 0 ]; then
                    log "Too many errors — stopping ramp-up"
                    break
                fi
            fi
        else
            # Direct registration (no workers)
            for entry in "${batch_addrs[@]}"; do
                IFS='|' read -r ca ha sk pk <<< "$entry"
                if register_address "$ca" "$ha" "$sk" "$pk"; then
                    registered=$((registered + 1))
                else
                    log "ERROR: Registration failed for ${ca}"
                    errors=$((errors + 1))
                    if [ "$errors" -ge 5 ] && [ "$TOTAL" -eq 0 ]; then
                        log "Too many consecutive HTTP errors — stopping ramp-up"
                        break 2
                    fi
                fi
                sleep "$DELAY"
            done
        fi

        # Dashboard every 30 seconds
        local now
        now=$(date +%s)
        if [ $((now - last_dashboard)) -ge 30 ]; then
            if ! print_dashboard "$registered" "$total_label"; then
                break  # memory limit hit
            fi
            last_dashboard=$now
        fi

        # Check Tor container is still running (unlimited mode safety valve)
        if [ "$TOTAL" -eq 0 ]; then
            if ! docker_cmd inspect --format='{{.State.Running}}' onionpress-tor 2>/dev/null | grep -q true; then
                log "ERROR: Tor container crashed — stopping ramp-up"
                break
            fi
        fi
    done

    echo ""
    log "=== Final metrics ==="
    print_dashboard "$registered" "$total_label" || true
    echo ""
    log "Total registered: ${registered} | Errors: ${errors}"
    log "Results saved to: ${OUTPUT_DIR}/metrics.jsonl"
}

# ── Worker mode ───────────────────────────────────────────────────────────────
run_worker() {
    detect_cellar_addr
    mkdir -p "$OUTPUT_DIR"

    log "=== OnionCellar Stress Test (worker) ==="
    log "Cellar: ${CELLAR_ADDR} | Delay: ${DELAY}s"
    log "Polling healthcheck messages for stress-test batches..."
    echo ""

    local registered=0
    local errors=0

    while true; do
        # Poll healthcheck messages via GET to our own healthcheck
        local messages
        messages=$(docker_cmd exec onionpress-tor \
            sh -c "ls /var/lib/tor/healthcheck-messages/*.json 2>/dev/null" | head -20) || true

        if [ -z "$messages" ]; then
            sleep 5
            continue
        fi

        # Process each message file
        local found_batch=false
        for msgfile in $messages; do
            local content
            content=$(docker_cmd exec onionpress-tor cat "$msgfile" 2>/dev/null) || continue

            # Check if it's a stress_test_batch
            if ! echo "$content" | grep -q '"stress_test_batch"'; then
                continue
            fi
            found_batch=true

            # Delete message file after reading
            docker_cmd exec onionpress-tor rm -f "$msgfile" 2>/dev/null || true

            # Extract addresses using python3 (available in tor container's Alpine)
            # Fall back to sed-based extraction if python3 unavailable
            local addresses
            addresses=$(echo "$content" | docker_cmd exec -i onionpress-tor sh -c "
                python3 -c '
import json,sys
data = json.load(sys.stdin)
for a in data.get(\"addresses\", []):
    print(a[\"content_address\"], a[\"healthcheck_address\"], a[\"secret_key\"], a[\"public_key\"])
' 2>/dev/null") || continue

            if [ -z "$addresses" ]; then
                continue
            fi

            # Register each address with the cellar
            while IFS=' ' read -r ca ha sk pk; do
                [ -z "$ca" ] && continue
                local start_ts
                start_ts=$(date +%s%N 2>/dev/null || date +%s)

                # POST to cellar's /register via Tor
                local output
                output=$(docker_cmd exec onionpress-tor \
                    wget -q -O - --timeout=30 \
                    --header="Content-Type: application/json" \
                    --post-data="{\"content_address\":\"${ca}\",\"healthcheck_address\":\"${ha}\",\"secret_key\":\"${sk}\",\"public_key\":\"${pk}\",\"version\":\"${STRESS_VERSION}\"}" \
                    "http://${CELLAR_ADDR}/register" 2>&1) || true

                local end_ts
                end_ts=$(date +%s%N 2>/dev/null || date +%s)

                if echo "$output" | grep -q '"registered".*true'; then
                    registered=$((registered + 1))
                    log "Registered ${ca} (${registered} total)"
                    log_json "\"event\":\"register\",\"address\":\"${ca}\",\"ok\":true,\"elapsed_ns\":$((end_ts - start_ts))"
                else
                    errors=$((errors + 1))
                    log "ERROR registering ${ca}: ${output}"
                    log_json "\"event\":\"register\",\"address\":\"${ca}\",\"ok\":false,\"elapsed_ns\":$((end_ts - start_ts))"
                fi

                sleep "$DELAY"
            done <<< "$addresses"
        done

        if [ "$found_batch" = false ]; then
            sleep 5
        fi
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
    coordinator) run_coordinator ;;
    worker)      run_worker ;;
    *)
        echo "Unknown mode: $MODE (use 'coordinator' or 'worker')"
        exit 1
        ;;
esac
