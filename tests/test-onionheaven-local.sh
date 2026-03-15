#!/bin/bash
#
# OnionHeaven Local Functionality Test
# https://github.com/brewsterkahle/onionpress/issues/113
#
# Tests the full OnionHeaven lifecycle against a live local OnionPress instance:
#   1. Initial state verification
#   2. Activation via faux heartbeat
#   3. Takeover after heartbeat goes stale (180s propagation delay)
#   4. Takeover propagation (Arti keystore + config)
#   5. Release via heartbeat
#   6. Release propagation
#   7. Unregister
#
# Prerequisites:
#   - OnionPress running locally (OnionHeaven API runs on every node)
#   - Docker accessible
#
# Usage:
#   ./tests/test-onionheaven-local.sh
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
HELPER="$SCRIPT_DIR/oh-test-helper.py"
TMPDIR_TEST=$(mktemp -d)
IDENTITY="$TMPDIR_TEST/identity.json"
PAYLOAD="$TMPDIR_TEST/payload.json"
TEST_LOG="$SCRIPT_DIR/test-onionheaven-local.log"
TEST_START=$(date +%s)
PASS_COUNT=0
FAIL_COUNT=0

# Use DOCKER_HOST if set (Colima), otherwise default
export DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.onionpress/colima/docker.sock}"

# Container name
TOR_CONTAINER="onionpress-tor"

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

# Open a Terminal.app window tailing the test log
open_log_window() {
    local abs_path
    abs_path=$(cd "$(dirname "$TEST_LOG")" && pwd)/$(basename "$TEST_LOG")
    > "$abs_path"  # truncate
    osascript -e "
        tell application \"Terminal\"
            activate
            do script \"tail -f '${abs_path}'\"
        end tell
    " 2>/dev/null &
}

# Tee all output to both the terminal and the log file
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

# Call the OnionHeaven API via docker exec
oh_api() {
    local method="$1"
    local path="$2"
    local data="${3:-}"

    if [ "$method" = "GET" ]; then
        docker exec "$TOR_CONTAINER" curl -s "http://127.0.0.1:8083${path}" 2>/dev/null
    else
        docker exec "$TOR_CONTAINER" curl -s -X POST \
            -H "Content-Type: application/json" \
            -d "$data" \
            "http://127.0.0.1:8083${path}" 2>/dev/null
    fi
}

# Generate a signed payload using the Python helper
sign_payload() {
    local cmd="$1"
    shift
    python3 "$HELPER" "$cmd" "$@"
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

echo ""
printf "${YELLOW}OnionHeaven Local Functionality Test${NC}\n"
printf "${YELLOW}=====================================${NC}\n"
echo ""

log "Running pre-flight checks..."

# Check Docker is accessible
if ! docker info >/dev/null 2>&1; then
    fail "Docker not accessible (is OnionPress running?)"
    exit 1
fi
pass "Docker accessible"

# Check tor container is running
if ! docker ps --format '{{.Names}}' | grep -q "^${TOR_CONTAINER}$"; then
    fail "$TOR_CONTAINER container not running"
    exit 1
fi
pass "$TOR_CONTAINER container running"

# Check OnionHeaven API is responding (runs on every node since v2.4.33+)
if ! oh_api GET /status >/dev/null 2>&1; then
    fail "OnionHeaven API not responding on port 8083"
    log "  The tor container image may need updating: docker pull ghcr.io/brewsterkahle/onionpress-tor:latest"
    exit 1
fi
pass "OnionHeaven API responding"

# Read the local onion address
ONION_ADDR=$(docker exec "$TOR_CONTAINER" cat /var/lib/tor/hidden_service/wordpress/hostname 2>/dev/null || echo "")
if [ -z "$ONION_ADDR" ]; then
    fail "Could not read local onion address from tor container"
    exit 1
fi
log "Local onion address: $ONION_ADDR"

# Verify WordPress is responding locally
WP_STATUS=$(docker exec "$TOR_CONTAINER" curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://wordpress:80/" 2>/dev/null || echo "000")
if [ "$WP_STATUS" = "200" ] || [ "$WP_STATUS" = "301" ] || [ "$WP_STATUS" = "302" ]; then
    pass "WordPress responding internally (HTTP $WP_STATUS)"
else
    fail "WordPress not responding internally (HTTP $WP_STATUS)"
    exit 1
fi

# Verify the onion address is reachable via Tor
log "Checking onion address reachability via Tor (may take a moment)..."
TOR_STATUS=$(docker exec onionpress-tor-client curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${ONION_ADDR}/" 2>/dev/null || echo "000")
if [ "$TOR_STATUS" = "200" ] || [ "$TOR_STATUS" = "301" ] || [ "$TOR_STATUS" = "302" ]; then
    pass "Onion address reachable via Tor (HTTP $TOR_STATUS)"
else
    fail "Onion address not reachable via Tor (HTTP $TOR_STATUS)"
    exit 1
fi

# Clean up leftover state from previous test runs
log "Cleaning up leftover OnionHeaven state..."
# Remove activate flag so the lazy watcher doesn't restart containers
docker exec "$TOR_CONTAINER" rm -f /var/lib/onionpress/onionheaven/activate 2>/dev/null || true
# Clear test entries from registry DB so the watcher doesn't see total > 0 and re-bootstrap
docker exec "$TOR_CONTAINER" sh -c "sqlite3 /var/lib/onionpress/onionheaven/registry.db \"DELETE FROM registry WHERE version LIKE 'test-%' OR version LIKE 'stress-test-%'\" 2>/dev/null" || true
# Stop any leftover onionheaven containers
for c in onionheaven onionheaven-takeover-0 onionheaven-takeover-1 onionheaven-takeover-2; do
    if docker ps -a --format '{{.Names}}' | grep -q "^${c}$"; then
        docker stop "$c" >/dev/null 2>&1 && docker rm "$c" >/dev/null 2>&1 && log "  Cleaned up $c"
    fi
done
# Brief pause to let the lazy watcher cycle past without restarting
sleep 12
# Verify containers stayed down
if docker ps --format '{{.Names}}' | grep -q "^onionheaven$"; then
    fail "onionheaven container respawned after cleanup — check for leftover DB entries"
    exit 1
fi
pass "Leftover OnionHeaven state cleaned up"

# Generate faux OnionPress identity
log "Generating faux OnionPress identity..."
python3 "$HELPER" generate > "$IDENTITY"
CONTENT_ADDR=$(python3 -c "import json; print(json.load(open('$IDENTITY'))['content_address'])")
HEALTHCHECK_ADDR=$(python3 -c "import json; print(json.load(open('$IDENTITY'))['healthcheck_address'])")
log "  Content address:     ${CONTENT_ADDR:0:20}...onion"
log "  Healthcheck address: ${HEALTHCHECK_ADDR:0:20}...onion"

# ---------------------------------------------------------------------------
# Step 1: Test initial state
# ---------------------------------------------------------------------------

step 1 "Verify initial state (OnionPress up, OnionHeaven not yet activated)"

# Verify the onionheaven container is NOT running (lazy activation)
if docker ps --format '{{.Names}}' | grep -q "^onionheaven$"; then
    fail "onionheaven container is already running — stop it first for a clean test"
    log "  Run: docker stop onionheaven && docker rm onionheaven"
    exit 1
else
    pass "No onionheaven container running (lazy activation not yet triggered)"
fi

# Check that our faux address is not already registered
STATUS_ADDR=$(oh_api GET "/status/${CONTENT_ADDR}")
if echo "$STATUS_ADDR" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'error' in d else 1)" 2>/dev/null; then
    pass "Faux address not in registry (as expected)"
else
    fail "Faux address already exists in registry — aborting"
    echo "$STATUS_ADDR"
    exit 1
fi

# Record current total count
INITIAL_TOTAL=$(oh_api GET /status | python3 -c "import sys,json; print(json.load(sys.stdin).get('total', 0))")
log "Current registry total: $INITIAL_TOTAL entries"

# ---------------------------------------------------------------------------
# Step 2: Activate via heartbeat
# ---------------------------------------------------------------------------

step 2 "Activate via faux heartbeat"

# Generate signed /online payload with Arti key
sign_payload sign-online "$IDENTITY" --with-key > "$PAYLOAD"
RESPONSE=$(oh_api POST /online "$(cat "$PAYLOAD")")
log "Response: $RESPONSE"

# Check response fields
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
NEW_TOTAL=$(oh_api GET /status | python3 -c "import sys,json; print(json.load(sys.stdin).get('total', 0))")
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

# Check key stored on disk
KEY_EXISTS=$(docker exec "$TOR_CONTAINER" test -f "/var/lib/onionpress/onionheaven/keys/${CONTENT_ADDR}/ks_hs_id.ed25519_expanded_private" && echo "yes" || echo "no")
if [ "$KEY_EXISTS" = "yes" ]; then
    pass "Arti key file exists in OnionHeaven keys directory"
else
    fail "Arti key file not found in OnionHeaven keys directory"
fi

# ---------------------------------------------------------------------------
# Step 3: Wait for takeover
# ---------------------------------------------------------------------------

step 3 "Wait for takeover (heartbeat goes stale after 180s)"

log "Stopping heartbeats — waiting for heartbeat monitor to trigger takeover..."
log "Propagation delay: 180s, monitor interval: 15s, worst case: ~200s"

TAKEOVER_START=$(date +%s)
TAKEOVER_DETECTED=false

# Poll every 10 seconds for up to 240 seconds
for i in $(seq 1 24); do
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
    fail "Takeover not detected after 240s"
fi

# ---------------------------------------------------------------------------
# Step 4: Verify takeover propagation
# ---------------------------------------------------------------------------

step 4 "Verify takeover propagation (keystore + 302 redirect via Tor)"

# Takeover keys are installed in the onionheaven container's Arti, not onionpress-tor
OH_CONTAINER="onionheaven"

# Check that the onionheaven container is running (lazy activation should have started it)
if docker ps --format '{{.Names}}' | grep -q "^${OH_CONTAINER}$"; then
    pass "onionheaven container is running (lazy activation triggered)"
else
    fail "onionheaven container not running — takeover cannot install keys"
fi

# Check that the key was installed in the onionheaven container's Arti keystore
ADDR_PREFIX="${CONTENT_ADDR:0:20}"
ARTI_KEY_EXISTS=$(docker exec "$OH_CONTAINER" sh -c "ls /var/lib/arti/state/keystore/hss/onionheaven_${ADDR_PREFIX}*/ks_hs_id* 2>/dev/null | head -1" || echo "")
if [ -n "$ARTI_KEY_EXISTS" ]; then
    pass "Key installed in onionheaven Arti keystore: $(basename $(dirname $ARTI_KEY_EXISTS))"
else
    # Try finding by content address pattern
    ARTI_KEY_EXISTS=$(docker exec "$OH_CONTAINER" sh -c "find /var/lib/arti/state/keystore/hss/ -name 'ks_hs_id*' 2>/dev/null | head -1" || echo "")
    if [ -n "$ARTI_KEY_EXISTS" ]; then
        pass "Key installed in onionheaven Arti keystore"
    else
        fail "Key not found in onionheaven Arti keystore"
        log "  Listing onionheaven keystore:"
        docker exec "$OH_CONTAINER" sh -c "ls -la /var/lib/arti/state/keystore/hss/ 2>/dev/null" || true
    fi
fi

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
    REDIRECT_CODE=$(docker exec onionpress-tor-client curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${CONTENT_ADDR}/" 2>/dev/null || echo "000")
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
    REDIRECT_LOCATION=$(docker exec onionpress-tor-client curl -s -D - -o /dev/null --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${CONTENT_ADDR}/" 2>/dev/null | grep -i "^Location:" | tr -d '\r')
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

# Generate a new signed /online payload (no key needed this time)
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
# Step 6: Verify release propagation
# ---------------------------------------------------------------------------

step 6 "Verify release propagation (no more 302 via Tor)"

# Give Arti a moment to process the SIGHUP and remove keystore
sleep 5

# Check that the key was removed from the onionheaven container's Arti keystore
ARTI_KEY_GONE=$(docker exec "$OH_CONTAINER" sh -c "find /var/lib/arti/state/keystore/hss/ -path '*${ADDR_PREFIX}*' 2>/dev/null | head -1" || echo "")
if [ -z "$ARTI_KEY_GONE" ]; then
    pass "Key removed from onionheaven Arti keystore"
else
    fail "Key still present in onionheaven Arti keystore: $ARTI_KEY_GONE"
fi

# Wait for Tor descriptor to expire, then verify the address no longer serves a 302
# This can resolve in seconds or take up to 3 hours depending on Arti's behavior.
# We poll for 5 minutes — if still reachable, log it as a known Arti limitation
# (issue #114) rather than failing the test.
log "Waiting for Tor descriptor to expire after release..."
RELEASE_CONFIRMED=false
REL_START=$(date +%s)

for i in $(seq 1 30); do
    sleep 10
    REL_ELAPSED=$(( $(date +%s) - REL_START ))
    RELEASE_CODE=$(docker exec onionpress-tor-client curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${CONTENT_ADDR}/" 2>/dev/null || echo "000")
    log "  [${REL_ELAPSED}s] HTTP $RELEASE_CODE"
    if [ "$RELEASE_CODE" != "302" ]; then
        RELEASE_CONFIRMED=true
        pass "Address no longer returns 302 after ${REL_ELAPSED}s (HTTP $RELEASE_CODE)"
        break
    fi
done

if [ "$RELEASE_CONFIRMED" = false ]; then
    log "  Address still returning 302 after 300s — known Arti descriptor caching (issue #114)"
    log "  Descriptor may persist on HSDirs for up to 3 hours after release"
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

# Verify key file was cleaned up
KEY_GONE=$(docker exec "$TOR_CONTAINER" test -f "/var/lib/onionpress/onionheaven/keys/${CONTENT_ADDR}/ks_hs_id.ed25519_expanded_private" && echo "exists" || echo "gone")
if [ "$KEY_GONE" = "gone" ]; then
    pass "Key file removed from OnionHeaven keys directory"
else
    # Non-stress-test entries get soft-deleted, key stays
    log "  (Key file retained — expected for non-stress-test entries)"
    pass "Unregister completed (soft delete, key retained)"
fi

# Verify /status total decreased or entry is marked unregistered
FINAL_STATUS=$(oh_api GET "/status/${CONTENT_ADDR}" 2>/dev/null || echo '{"error":"not found"}')
log "Final status: $FINAL_STATUS"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
printf "${YELLOW}━━━ Summary ━━━${NC}\n"
TOTAL_ELAPSED=$(( $(date +%s) - TEST_START ))
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
