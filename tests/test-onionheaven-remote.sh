#!/bin/bash
#
# OnionHeaven Remote Functionality Test
# https://github.com/brewsterkahle/onionpress/issues/113
#
# Tests the full OnionHeaven lifecycle against a REMOTE OnionHeaven node
# that we do NOT have direct container access to.  All verification goes
# through the public OnionHeaven API over Tor and Tor reachability checks.
#
# Lifecycle tested:
#   1. API reachability + initial state
#   2. Register via /online (with Arti key)
#   3. Takeover after heartbeat goes stale (180s propagation delay)
#   4. Takeover verification (302 redirect via Tor)
#   5. Release via /online heartbeat
#   6. Release verification (no more 302)
#   7. Unregister via /unregister
#
# Prerequisites:
#   - A Tor SOCKS proxy accessible from this machine.  The script auto-detects:
#       a) OnionPress tor-client container (docker exec)
#       b) Tor Browser SOCKS on 127.0.0.1:9150
#       c) System Tor SOCKS on 127.0.0.1:9050
#     Or pass --socks host:port to override.
#
# Usage:
#   ./tests/test-onionheaven-remote.sh <onionheaven-address.onion>
#   ./tests/test-onionheaven-remote.sh --socks 127.0.0.1:9150 <address.onion>
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
HELPER="$SCRIPT_DIR/oh-test-helper.py"
TMPDIR_TEST=$(mktemp -d)
IDENTITY="$TMPDIR_TEST/identity.json"
PAYLOAD="$TMPDIR_TEST/payload.json"
TEST_LOG="$SCRIPT_DIR/test-onionheaven-remote.log"
TEST_START=$(date +%s)
PASS_COUNT=0
FAIL_COUNT=0

# Tor SOCKS access mode: "docker" or "direct"
SOCKS_MODE=""
SOCKS_ADDR=""          # host:port for direct mode
DOCKER_CTR=""          # container name for docker mode
ONIONHEAVEN_ADDR=""

# Use DOCKER_HOST if set, otherwise Colima socket on macOS
if [ -z "${DOCKER_HOST:-}" ]; then
    if [ -S "$HOME/.onionpress/colima/docker.sock" ]; then
        export DOCKER_HOST="unix://$HOME/.onionpress/colima/docker.sock"
    fi
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --socks)
            SOCKS_ADDR="$2"
            SOCKS_MODE="direct"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [--socks host:port] <onionheaven-address.onion>"
            echo ""
            echo "Options:"
            echo "  --socks host:port   Use this SOCKS5 proxy (e.g. 127.0.0.1:9150)"
            echo ""
            echo "The OnionHeaven .onion address is required."
            exit 0
            ;;
        *)
            ONIONHEAVEN_ADDR="$1"
            shift
            ;;
    esac
done

if [ -z "$ONIONHEAVEN_ADDR" ]; then
    echo "Error: OnionHeaven .onion address required"
    echo "Usage: $0 [--socks host:port] <onionheaven-address.onion>"
    exit 1
fi

# Strip trailing slash, ensure .onion suffix
ONIONHEAVEN_ADDR="${ONIONHEAVEN_ADDR%/}"
if ! echo "$ONIONHEAVEN_ADDR" | grep -q '\.onion$'; then
    echo "Error: Address must end in .onion"
    exit 1
fi

# ---------------------------------------------------------------------------
# Log window + tee
# ---------------------------------------------------------------------------

open_log_window() {
    local abs_path
    abs_path=$(cd "$(dirname "$TEST_LOG")" && pwd)/$(basename "$TEST_LOG")
    > "$abs_path"
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "
            tell application \"Terminal\"
                activate
                do script \"tail -f '${abs_path}'\"
            end tell
        " 2>/dev/null &
    fi
}

open_log_window
exec > >(tee -a "$TEST_LOG") 2>&1

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
    local elapsed=$(( $(date +%s) - TEST_START ))
    printf "${BLUE}[%3ds]${NC} %s\n" "$elapsed" "$1"
}

pass() {
    local elapsed=$(( $(date +%s) - TEST_START ))
    PASS_COUNT=$((PASS_COUNT + 1))
    printf "${GREEN}[%3ds] ✓ PASS:${NC} %s\n" "$elapsed" "$1"
}

fail() {
    local elapsed=$(( $(date +%s) - TEST_START ))
    FAIL_COUNT=$((FAIL_COUNT + 1))
    printf "${RED}[%3ds] ✗ FAIL:${NC} %s\n" "$elapsed" "$1"
}

step() {
    echo ""
    printf "${YELLOW}━━━ Step %s: %s ━━━${NC}\n" "$1" "$2"
}

# Execute a curl via Tor.  Returns curl output on stdout.
# Usage: tor_curl [curl-args...]
tor_curl() {
    if [ "$SOCKS_MODE" = "docker" ]; then
        docker exec "$DOCKER_CTR" \
            curl -s --max-time 30 \
            --socks5-hostname 127.0.0.1:9050 \
            "$@" 2>/dev/null
    else
        curl -s --max-time 30 \
            --socks5-hostname "$SOCKS_ADDR" \
            "$@" 2>/dev/null
    fi
}

# Call the OnionHeaven API via Tor
oh_api() {
    local method="$1"
    local path="$2"
    local data="${3:-}"

    if [ "$method" = "GET" ]; then
        tor_curl "http://${ONIONHEAVEN_ADDR}:8083${path}"
    else
        tor_curl -X POST -H "Content-Type: application/json" \
            -d "$data" \
            "http://${ONIONHEAVEN_ADDR}:8083${path}"
    fi
}

# Generate a signed payload using the Python helper
sign_payload() {
    local cmd="$1"
    shift
    python3 "$HELPER" "$cmd" "$@"
}

# ---------------------------------------------------------------------------
# Auto-detect Tor SOCKS proxy
# ---------------------------------------------------------------------------

detect_socks() {
    log "Detecting Tor SOCKS proxy..."

    # Option 1: OnionPress tor-client container
    if [ -z "$SOCKS_MODE" ] && docker info >/dev/null 2>&1; then
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^onionpress-tor-client$"; then
            # Verify SOCKS is responding inside the container
            if docker exec onionpress-tor-client curl -s -o /dev/null --max-time 5 --socks5-hostname 127.0.0.1:9050 "http://check.torproject.org/" 2>/dev/null; then
                SOCKS_MODE="docker"
                DOCKER_CTR="onionpress-tor-client"
                log "  Using OnionPress tor-client container"
                return
            fi
        fi
    fi

    # Option 2: Tor Browser SOCKS on 9150
    if [ -z "$SOCKS_MODE" ]; then
        if curl -s -o /dev/null --max-time 5 --socks5-hostname 127.0.0.1:9150 "http://check.torproject.org/" 2>/dev/null; then
            SOCKS_MODE="direct"
            SOCKS_ADDR="127.0.0.1:9150"
            log "  Using Tor Browser SOCKS on 127.0.0.1:9150"
            return
        fi
    fi

    # Option 3: System Tor on 9050
    if [ -z "$SOCKS_MODE" ]; then
        if curl -s -o /dev/null --max-time 5 --socks5-hostname 127.0.0.1:9050 "http://check.torproject.org/" 2>/dev/null; then
            SOCKS_MODE="direct"
            SOCKS_ADDR="127.0.0.1:9050"
            log "  Using system Tor SOCKS on 127.0.0.1:9050"
            return
        fi
    fi

    if [ -z "$SOCKS_MODE" ]; then
        fail "No Tor SOCKS proxy found"
        echo ""
        echo "  The remote test needs a Tor SOCKS proxy.  Options:"
        echo "    1. Run OnionPress (provides onionpress-tor-client container)"
        echo "    2. Open Tor Browser (provides SOCKS on 127.0.0.1:9150)"
        echo "    3. Install Tor (provides SOCKS on 127.0.0.1:9050)"
        echo "    4. Pass --socks host:port"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

echo ""
printf "${YELLOW}OnionHeaven Remote Functionality Test${NC}\n"
printf "${YELLOW}======================================${NC}\n"
echo ""

log "Target: $ONIONHEAVEN_ADDR"

# Auto-detect SOCKS if not specified
if [ -z "$SOCKS_MODE" ]; then
    detect_socks
else
    log "Using specified SOCKS proxy: $SOCKS_ADDR"
fi

if [ "$SOCKS_MODE" = "docker" ]; then
    pass "Tor SOCKS: docker exec $DOCKER_CTR"
else
    pass "Tor SOCKS: $SOCKS_ADDR"
fi

# Check OnionHeaven API is responding
log "Checking OnionHeaven API reachability..."
OH_API_OK=false
for attempt in $(seq 1 12); do
    RESPONSE=$(oh_api GET /status 2>/dev/null || echo "")
    if [ -n "$RESPONSE" ] && echo "$RESPONSE" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
        OH_API_OK=true
        break
    fi
    log "  Attempt $attempt/12 — not reachable yet"
    [ "$attempt" -lt 12 ] && sleep 10
done

if [ "$OH_API_OK" = true ]; then
    pass "OnionHeaven API responding at $ONIONHEAVEN_ADDR:8083"
    OH_VERSION=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version', 'unknown'))" 2>/dev/null || echo "unknown")
    OH_TOTAL=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total', '?'))" 2>/dev/null || echo "?")
    log "  Server version: $OH_VERSION"
    log "  Registry entries: $OH_TOTAL"
else
    fail "OnionHeaven API not responding at $ONIONHEAVEN_ADDR:8083 after 2 minutes"
    exit 1
fi

# Generate faux OnionPress identity
log "Generating faux OnionPress identity..."
python3 "$HELPER" generate > "$IDENTITY"
CONTENT_ADDR=$(python3 -c "import json; print(json.load(open('$IDENTITY'))['content_address'])")
HEALTHCHECK_ADDR=$(python3 -c "import json; print(json.load(open('$IDENTITY'))['healthcheck_address'])")
log "  Content address:     ${CONTENT_ADDR:0:20}...onion"
log "  Healthcheck address: ${HEALTHCHECK_ADDR:0:20}...onion"

# Record initial state
INITIAL_TOTAL=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total', 0))")

# ---------------------------------------------------------------------------
# Step 1: Verify initial state
# ---------------------------------------------------------------------------

step 1 "Verify initial state"

# Check that our faux address is not already registered
STATUS_ADDR=$(oh_api GET "/status/${CONTENT_ADDR}")
if echo "$STATUS_ADDR" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'error' in d else 1)" 2>/dev/null; then
    pass "Faux address not in registry (as expected)"
else
    fail "Faux address already exists in registry — extremely unlikely collision"
    echo "$STATUS_ADDR"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 2: Register via /online heartbeat
# ---------------------------------------------------------------------------

step 2 "Register via faux heartbeat (/online with Arti key)"

sign_payload sign-online "$IDENTITY" --with-key > "$PAYLOAD"
RESPONSE=$(oh_api POST /online "$(cat "$PAYLOAD")")
log "Response: $RESPONSE"

ONLINE=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('online', False))")
CREATED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('created', False))")
KEY_STORED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('arti_key_stored', False))")

if [ "$ONLINE" = "True" ]; then
    pass "/online returned online=true"
else
    fail "/online did not return online=true"
fi

if [ "$CREATED" = "True" ]; then
    pass "Entry was created (created=true)"
else
    fail "Entry was not created"
fi

if [ "$KEY_STORED" = "True" ]; then
    pass "Arti key was stored (arti_key_stored=true)"
else
    fail "Arti key was not stored"
fi

# Verify /status count increased
NEW_STATUS=$(oh_api GET /status)
NEW_TOTAL=$(echo "$NEW_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total', 0))")
if [ "$NEW_TOTAL" -gt "$INITIAL_TOTAL" ]; then
    pass "/status total increased ($INITIAL_TOTAL → $NEW_TOTAL)"
else
    fail "/status total did not increase ($INITIAL_TOTAL → $NEW_TOTAL)"
fi

# Verify /status/<address> shows online
ENTRY_STATUS=$(oh_api GET "/status/${CONTENT_ADDR}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['entries'][0]['status'])")
if [ "$ENTRY_STATUS" = "online" ]; then
    pass "/status/<address> shows status=online"
else
    fail "/status/<address> shows status=$ENTRY_STATUS (expected online)"
fi

# ---------------------------------------------------------------------------
# Step 3: Wait for takeover (heartbeat goes stale)
# ---------------------------------------------------------------------------

step 3 "Wait for takeover (heartbeat goes stale after 180s)"

log "Stopping heartbeats — waiting for heartbeat monitor to trigger takeover..."
log "Propagation delay: 180s, monitor interval: 15s, worst case: ~200s"

TAKEOVER_START=$(date +%s)
TAKEOVER_DETECTED=false

# Poll every 10 seconds for up to 300 seconds (extra margin for remote)
for i in $(seq 1 30); do
    sleep 10
    ELAPSED=$(( $(date +%s) - TAKEOVER_START ))
    ENTRY_STATUS=$(oh_api GET "/status/${CONTENT_ADDR}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['entries'][0]['status'])" 2>/dev/null || echo "error")
    log "  [${ELAPSED}s] status=$ENTRY_STATUS"
    if [ "$ENTRY_STATUS" = "taken-over" ]; then
        TAKEOVER_DETECTED=true
        pass "Takeover detected after ${ELAPSED}s"
        break
    fi
done

if [ "$TAKEOVER_DETECTED" = false ]; then
    fail "Takeover not detected after 300s"
fi

# ---------------------------------------------------------------------------
# Step 4: Verify takeover via API and Tor reachability
# ---------------------------------------------------------------------------

step 4 "Verify takeover (API state + 302 redirect via Tor)"

# Check /status/<address> has last_taken_over timestamp
TAKEN_OVER_AT=$(oh_api GET "/status/${CONTENT_ADDR}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['entries'][0].get('last_taken_over', 'none'))")
if [ "$TAKEN_OVER_AT" != "none" ] && [ "$TAKEN_OVER_AT" != "None" ] && [ "$TAKEN_OVER_AT" != "null" ]; then
    pass "last_taken_over timestamp set: $TAKEN_OVER_AT"
else
    fail "last_taken_over timestamp not set"
fi

# Wait for Tor descriptor propagation, then verify 302 redirect via Tor network
log "Waiting for Tor descriptor propagation to reach the faux address..."
log "Descriptor propagation typically takes 5-10 minutes from a separate client"
REDIRECT_DETECTED=false
PROP_START=$(date +%s)

for i in $(seq 1 60); do
    sleep 10
    PROP_ELAPSED=$(( $(date +%s) - PROP_START ))
    REDIRECT_CODE=$(tor_curl -o /dev/null -w "%{http_code}" "http://${CONTENT_ADDR}/" 2>/dev/null || echo "000")
    log "  [${PROP_ELAPSED}s] HTTP $REDIRECT_CODE"
    if [ "$REDIRECT_CODE" = "302" ]; then
        REDIRECT_DETECTED=true
        pass "302 redirect received via Tor after ${PROP_ELAPSED}s"
        break
    fi
done

if [ "$REDIRECT_DETECTED" = false ]; then
    fail "302 redirect not received via Tor after 600s"
fi

# If we got the 302, verify it points to Wayback Machine
if [ "$REDIRECT_DETECTED" = true ]; then
    REDIRECT_LOCATION=$(tor_curl -D - -o /dev/null "http://${CONTENT_ADDR}/" 2>/dev/null | grep -i "^Location:" | tr -d '\r')
    if echo "$REDIRECT_LOCATION" | grep -q "archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"; then
        pass "Redirect points to Wayback Machine onion service"
    else
        fail "Redirect location unexpected: $REDIRECT_LOCATION"
    fi
fi

# ---------------------------------------------------------------------------
# Step 5: Release via heartbeat
# ---------------------------------------------------------------------------

step 5 "Release via heartbeat (send /online again)"

sign_payload sign-online "$IDENTITY" > "$PAYLOAD"
RESPONSE=$(oh_api POST /online "$(cat "$PAYLOAD")")
log "Response: $RESPONSE"

ONLINE=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('online', False))")
if [ "$ONLINE" = "True" ]; then
    pass "/online returned online=true"
else
    fail "/online did not return online=true"
fi

# Verify status changed back to online
ENTRY_STATUS=$(oh_api GET "/status/${CONTENT_ADDR}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['entries'][0]['status'])")
if [ "$ENTRY_STATUS" = "online" ]; then
    pass "/status/<address> shows status=online (released)"
else
    fail "/status/<address> shows status=$ENTRY_STATUS (expected online)"
fi

# Check last_released timestamp
RELEASED_AT=$(oh_api GET "/status/${CONTENT_ADDR}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['entries'][0].get('last_released', 'none'))")
if [ "$RELEASED_AT" != "none" ] && [ "$RELEASED_AT" != "None" ] && [ "$RELEASED_AT" != "null" ]; then
    pass "last_released timestamp set: $RELEASED_AT"
else
    fail "last_released timestamp not set"
fi

# ---------------------------------------------------------------------------
# Step 6: Verify release propagation (no more 302)
# ---------------------------------------------------------------------------

step 6 "Verify release propagation (no more 302 via Tor)"

# Wait for Tor descriptor to expire.  Poll for 30 minutes.
# Send periodic heartbeats to prevent re-takeover.
log "Waiting for Tor descriptor to expire after release (polling up to 30 minutes)..."
log "  (Sending periodic heartbeats to prevent re-takeover)"
RELEASE_CONFIRMED=false
REL_START=$(date +%s)
LAST_HEARTBEAT=$(date +%s)

for i in $(seq 1 180); do
    sleep 10
    REL_ELAPSED=$(( $(date +%s) - REL_START ))
    REL_MINS=$((REL_ELAPSED / 60))
    REL_SECS=$((REL_ELAPSED % 60))

    # Send a heartbeat every 60s to keep the entry "online" and prevent re-takeover
    SINCE_HEARTBEAT=$(( $(date +%s) - LAST_HEARTBEAT ))
    if [ "$SINCE_HEARTBEAT" -ge 60 ]; then
        sign_payload sign-online "$IDENTITY" > "$PAYLOAD"
        oh_api POST /online "$(cat "$PAYLOAD")" >/dev/null 2>&1
        LAST_HEARTBEAT=$(date +%s)
    fi

    RELEASE_CODE=$(tor_curl -o /dev/null -w "%{http_code}" "http://${CONTENT_ADDR}/" 2>/dev/null || echo "000")
    log "  [${REL_MINS}m ${REL_SECS}s] HTTP $RELEASE_CODE"
    if [ "$RELEASE_CODE" != "302" ]; then
        RELEASE_CONFIRMED=true
        pass "Address no longer returns 302 after ${REL_MINS}m ${REL_SECS}s (HTTP $RELEASE_CODE)"
        break
    fi
done

if [ "$RELEASE_CONFIRMED" = false ]; then
    fail "Address still returning 302 after 30 minutes — descriptor caching issue"
fi

# ---------------------------------------------------------------------------
# Step 7: Unregister
# ---------------------------------------------------------------------------

step 7 "Unregister faux OnionPress"

sign_payload sign-unregister "$IDENTITY" > "$PAYLOAD"
RESPONSE=$(oh_api POST /unregister "$(cat "$PAYLOAD")")
log "Response: $RESPONSE"

UNREGISTERED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('unregistered', False))")
if [ "$UNREGISTERED" = "True" ]; then
    pass "/unregister returned unregistered=true"
else
    fail "/unregister did not return unregistered=true"
fi

# Verify entry is gone or marked unregistered
FINAL_STATUS=$(oh_api GET "/status/${CONTENT_ADDR}" 2>/dev/null || echo '{"error":"not found"}')
log "Final status: $FINAL_STATUS"

# Check if entry is gone (error) or shows unregistered status
if echo "$FINAL_STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'error' in d else 1)" 2>/dev/null; then
    pass "Entry removed from registry"
else
    FINAL_ENTRY_STATUS=$(echo "$FINAL_STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['entries'][0].get('status','?'))" 2>/dev/null || echo "?")
    if [ "$FINAL_ENTRY_STATUS" = "unregistered" ]; then
        pass "Entry marked as unregistered"
    else
        log "  Entry still visible with status=$FINAL_ENTRY_STATUS (may be cleaned up later)"
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
printf "${YELLOW}━━━ Summary ━━━${NC}\n"
TOTAL_ELAPSED=$(( $(date +%s) - TEST_START ))
printf "  Target:      %s\n" "$ONIONHEAVEN_ADDR"
if [ "$SOCKS_MODE" = "docker" ]; then
    printf "  Tor SOCKS:   docker exec %s\n" "$DOCKER_CTR"
else
    printf "  Tor SOCKS:   %s\n" "$SOCKS_ADDR"
fi
printf "  Total time:  %dm %ds\n" "$((TOTAL_ELAPSED / 60))" "$((TOTAL_ELAPSED % 60))"
printf "  ${GREEN}Passed: %d${NC}\n" "$PASS_COUNT"
if [ "$FAIL_COUNT" -gt 0 ]; then
    printf "  ${RED}Failed: %d${NC}\n" "$FAIL_COUNT"
else
    printf "  Failed: 0\n"
fi
echo ""

if [ "$FAIL_COUNT" -gt 0 ]; then
    printf "${RED}SOME TESTS FAILED${NC}\n"
    exit 1
else
    printf "${GREEN}ALL TESTS PASSED${NC}\n"
    exit 0
fi
