#!/bin/bash
#
# Arti Onion Service Takeover & Release Timing Test
# https://github.com/brewsterkahle/onionpress/issues/114
#
# Standalone test — only requires Docker. No OnionPress needed.
# Runs two Arti containers (service + client), generates a throwaway
# onion service, and measures:
#
#   1. TAKEOVER TIME  — how long from key install + SIGHUP until the
#      address is reachable through the Tor network
#   2. RELEASE TIME   — how long from config removal + SIGHUP until
#      the address is no longer reachable
#
# Expected with C Tor:  takeover ~30s-5min, release ~seconds
#   (C Tor sends DESTROY cells to tear down intro point circuits)
# Observed with Arti:   takeover ~30s-5min, release 3+ HOURS
#   (Arti leaves intro circuits alive; descriptor cached on HSDirs)
#
# Usage:
#   ./test-arti-release-timing.sh                              # official image (amd64 only)
#   ARTI_IMAGE=ghcr.io/brewsterkahle/onionpress-tor:latest \
#     ./test-arti-release-timing.sh                            # OnionPress image (amd64+arm64)
#
# Cleanup (if interrupted):
#   docker rm -f arti-release-test-service arti-release-test-client 2>/dev/null
#   docker network rm arti-release-test-net 2>/dev/null
#

set -euo pipefail

TEST_START=$(date +%s)
TMPDIR_TEST=$(mktemp -d)
NETWORK_NAME="arti-release-test-net"
SERVICE_CONTAINER="arti-release-test-service"
CLIENT_CONTAINER="arti-release-test-client"

# Default: official Tor Project Arti image (amd64 only)
# Override with ARTI_IMAGE env var for arm64 or custom builds
ARTI_IMAGE="${ARTI_IMAGE:-containers.torproject.org/tpo/onion-services/onimages/arti:alpine}"

# Max polling time: 4 hours (descriptor lifetime is 3 hours)
MAX_POLL_SECONDS=14400

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

cleanup() {
    log "Cleaning up..."
    docker rm -f "$SERVICE_CONTAINER" "$CLIENT_CONTAINER" 2>/dev/null || true
    docker network rm "$NETWORK_NAME" 2>/dev/null || true
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

log() {
    local elapsed=$(( $(date +%s) - TEST_START ))
    printf "${BLUE}[%3ds]${NC} %s\n" "$elapsed" "$1"
}

step() {
    echo ""
    printf "${YELLOW}━━━ Step %s: %s ━━━${NC}\n" "$1" "$2"
}

# ---------------------------------------------------------------------------
echo ""
printf "${YELLOW}Arti Onion Service Takeover & Release Timing Test${NC}\n"
printf "${YELLOW}==================================================${NC}\n"
printf "Measures how long it takes to take over an onion address,\n"
printf "and how long the address remains reachable after release.\n"
printf "See: https://github.com/brewsterkahle/onionpress/issues/114\n"
echo ""

# ---------------------------------------------------------------------------
# Image-specific paths
# ---------------------------------------------------------------------------
# The official Tor Project image uses /home/arti for data.
# The OnionPress image uses /var/lib/arti.
# We detect which layout to use based on the image name.

if echo "$ARTI_IMAGE" | grep -q "onionpress"; then
    DATA_DIR="/var/lib/arti"
    STATE_DIR="/var/lib/arti/state"
    CACHE_DIR="/var/lib/arti/cache"
    KEYSTORE_BASE="/var/lib/arti/state/keystore"
    ARTI_USER="arti"
    IMAGE_TYPE="onionpress"
else
    DATA_DIR="/home/arti"
    STATE_DIR="/home/arti/state"
    CACHE_DIR="/home/arti/cache"
    KEYSTORE_BASE="/home/arti/keystore"
    ARTI_USER="arti"
    IMAGE_TYPE="official"
fi

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

log "Running pre-flight checks..."

if ! docker info >/dev/null 2>&1; then
    printf "${RED}Docker not accessible${NC}\n"
    exit 1
fi
log "Docker accessible"

ARCH=$(uname -m)
log "Architecture: $ARCH"
log "Image: $ARTI_IMAGE ($IMAGE_TYPE layout)"

# ---------------------------------------------------------------------------
step 1 "Start Arti containers"
# ---------------------------------------------------------------------------

# Clean up any previous run
docker rm -f "$SERVICE_CONTAINER" "$CLIENT_CONTAINER" 2>/dev/null || true
docker network rm "$NETWORK_NAME" 2>/dev/null || true

# Create isolated network (Arti needs --subnet for IP-based proxy_ports)
docker network create --subnet=10.99.0.0/24 "$NETWORK_NAME" >/dev/null
log "Created network: $NETWORK_NAME (10.99.0.0/24)"

# Write Arti configs
SERVICE_TOML="$TMPDIR_TEST/arti-service.toml"
CLIENT_TOML="$TMPDIR_TEST/arti-client.toml"

cat > "$SERVICE_TOML" << EOF
[logging]
console = "info,tor_hsservice=debug"

[proxy]
socks_listen = "0.0.0.0:9050"

[storage]
cache_dir = "$CACHE_DIR"
state_dir = "$STATE_DIR"

[storage.keystore]
enabled = true

[vanguards]
mode = "disabled"
EOF

cat > "$CLIENT_TOML" << EOF
[logging]
console = "info"

[proxy]
socks_listen = "0.0.0.0:9050"

[storage]
cache_dir = "$CACHE_DIR"
state_dir = "$STATE_DIR"

[address_filter]
allow_onion_addrs = true
EOF

# Pull the Arti image
log "Pulling Arti image..."
if ! docker pull "$ARTI_IMAGE" 2>&1 | tail -1; then
    printf "${RED}Cannot pull image: $ARTI_IMAGE${NC}\n"
    printf "On arm64, try: ARTI_IMAGE=ghcr.io/brewsterkahle/onionpress-tor:latest\n"
    exit 1
fi

# Config path inside container — must be writable (we append onion service config later)
CONTAINER_SERVICE_TOML="/etc/arti/arti-service.toml"
CONTAINER_CLIENT_TOML="/etc/arti/arti-client.toml"

# Both images have entrypoints that run their own Arti — override with --entrypoint
# to get a clean shell where we control everything.

# Start the service container
log "Starting service container..."
docker run -d --name "$SERVICE_CONTAINER" \
    --network "$NETWORK_NAME" --ip 10.99.0.10 \
    --entrypoint sh \
    "$ARTI_IMAGE" -c "
        mkdir -p '$CACHE_DIR' '$STATE_DIR' /etc/arti
        chown -R $ARTI_USER:$ARTI_USER '$DATA_DIR'
        chmod 700 '$DATA_DIR' '$CACHE_DIR' '$STATE_DIR'
        # Install curl + socat if missing (Alpine)
        if command -v apk >/dev/null 2>&1; then
            apk add --no-cache curl socat >/dev/null 2>&1 || true
        fi
        # Minimal HTTP server on port 8080 (the 'service' behind the onion)
        socat TCP-LISTEN:8080,reuseaddr,fork SYSTEM:'printf \"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 13\r\nConnection: close\r\n\r\nrelease-test!\"' &
        # Wait for config to be copied in
        while [ ! -f '$CONTAINER_SERVICE_TOML' ]; do sleep 0.2; done
        # Start Arti as arti user
        su -s /bin/sh $ARTI_USER -c 'arti proxy -c $CONTAINER_SERVICE_TOML'
    " >/dev/null
sleep 1
# Copy config into running container (writable, not bind-mounted)
docker cp "$SERVICE_TOML" "${SERVICE_CONTAINER}:${CONTAINER_SERVICE_TOML}"
docker exec "$SERVICE_CONTAINER" chown "$ARTI_USER:$ARTI_USER" "$CONTAINER_SERVICE_TOML"
sleep 3
if docker ps --format '{{.Names}}' | grep -q "^${SERVICE_CONTAINER}$"; then
    log "Service container running (10.99.0.10)"
else
    printf "${RED}Service container failed to start${NC}\n"
    docker logs "$SERVICE_CONTAINER" 2>&1 | tail -20
    exit 1
fi

# Start the client container (SOCKS proxy only)
log "Starting client container..."
docker run -d --name "$CLIENT_CONTAINER" \
    --network "$NETWORK_NAME" --ip 10.99.0.20 \
    --entrypoint sh \
    "$ARTI_IMAGE" -c "
        mkdir -p '$CACHE_DIR' '$STATE_DIR' /etc/arti
        chown -R $ARTI_USER:$ARTI_USER '$DATA_DIR'
        chmod 700 '$DATA_DIR' '$CACHE_DIR' '$STATE_DIR'
        # Install curl if missing (Alpine)
        if command -v apk >/dev/null 2>&1; then
            apk add --no-cache curl >/dev/null 2>&1 || true
        fi
        # Wait for config to be copied in
        while [ ! -f '$CONTAINER_CLIENT_TOML' ]; do sleep 0.2; done
        su -s /bin/sh $ARTI_USER -c 'arti proxy -c $CONTAINER_CLIENT_TOML'
    " >/dev/null
sleep 1
# Copy config into running container
docker cp "$CLIENT_TOML" "${CLIENT_CONTAINER}:${CONTAINER_CLIENT_TOML}"
docker exec "$CLIENT_CONTAINER" chown "$ARTI_USER:$ARTI_USER" "$CONTAINER_CLIENT_TOML"
sleep 3
if docker ps --format '{{.Names}}' | grep -q "^${CLIENT_CONTAINER}$"; then
    log "Client container running (10.99.0.20)"
else
    printf "${RED}Client container failed to start${NC}\n"
    docker logs "$CLIENT_CONTAINER" 2>&1 | tail -20
    exit 1
fi

# Print Arti version
ARTI_VERSION=$(docker exec "$SERVICE_CONTAINER" arti --version 2>/dev/null || echo "unknown")
log "Arti version: $ARTI_VERSION"

# Wait for Arti to bootstrap
log "Waiting for Arti to bootstrap (up to 120s)..."
for i in $(seq 1 24); do
    sleep 5
    if docker exec "$SERVICE_CONTAINER" curl -s --max-time 3 --socks5-hostname 127.0.0.1:9050 http://www.example.com/ >/dev/null 2>&1; then
        log "Service Arti bootstrapped"
        break
    fi
    if [ "$i" -eq 24 ]; then
        printf "${RED}Service Arti failed to bootstrap after 120s${NC}\n"
        docker logs "$SERVICE_CONTAINER" 2>&1 | tail -10
        exit 1
    fi
done

for i in $(seq 1 24); do
    sleep 5
    if docker exec "$CLIENT_CONTAINER" curl -s --max-time 3 --socks5-hostname 127.0.0.1:9050 http://www.example.com/ >/dev/null 2>&1; then
        log "Client Arti bootstrapped"
        break
    fi
    if [ "$i" -eq 24 ]; then
        printf "${RED}Client Arti failed to bootstrap after 120s${NC}\n"
        docker logs "$CLIENT_CONTAINER" 2>&1 | tail -10
        exit 1
    fi
done

# ---------------------------------------------------------------------------
step 2 "Generate throwaway key and install onion service (TAKEOVER)"
# ---------------------------------------------------------------------------

TAKEOVER_START=$(date +%s)

# Generate ed25519 keypair using Python (inline, no external dependencies)
log "Generating throwaway ed25519 keypair..."
KEYGEN_OUTPUT=$(python3 -c "
import hashlib, os, struct, base64

# Pure-Python ed25519 scalar mult (minimal, for key generation only)
p = 2**255 - 19
d = -121665 * pow(121666, p-2, p) % p
I = pow(2, (p-1)//4, p)

def xrecover(y):
    xx = (y*y-1) * pow(d*y*y+1, p-2, p)
    x = pow(xx, (p+3)//8, p)
    if (x*x - xx) % p != 0:
        x = (x*I) % p
    if x % 2 != 0:
        x = p - x
    return x

By = 4 * pow(5, p-2, p) % p
Bx = xrecover(By)
B = (Bx % p, By % p, 1, (Bx * By) % p)

def edwards_add(P, Q):
    x1,y1,z1,t1 = P; x2,y2,z2,t2 = Q
    a = (y1-x1)*(y2-x2) % p; b = (y1+x1)*(y2+x2) % p
    c = t1*2*d*t2 % p; dd = z1*2*z2 % p
    e = b-a; f = dd-c; g = dd+c; h = b+a
    return (e*f%p, g*h%p, f*g%p, e*h%p)

def scalar_mult(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1: Q = edwards_add(Q, P)
        P = edwards_add(P, P); s >>= 1
    return Q

def encode_point(P):
    x,y,z,_ = P
    zi = pow(z, p-2, p); x = (x*zi)%p; y = (y*zi)%p
    bs = y.to_bytes(32, 'little'); bs = bytearray(bs)
    bs[31] |= (x & 1) << 7
    return bytes(bs)

# Generate expanded key from random seed
seed = os.urandom(32)
h = hashlib.sha512(seed).digest()
a_bytes = bytearray(h[:32])
a_bytes[0] &= 248; a_bytes[31] &= 127; a_bytes[31] |= 64
expanded = bytes(a_bytes) + h[32:]
a = int.from_bytes(a_bytes, 'little')
A = scalar_mult(a, B)
pub = encode_point(A)

# Derive .onion address (v3)
version = b'\x03'
checksum = hashlib.sha3_256(b'.onion checksum' + pub + version).digest()[:2]
addr = base64.b32encode(pub + checksum + version).decode().lower() + '.onion'

# Build OpenSSH-format key for Arti keystore
KEY_TYPE = b'ed25519-expanded@spec.torproject.org'
def pack(data): return struct.pack('>I', len(data)) + data
pub_blob = pack(KEY_TYPE) + pack(pub)
check = struct.pack('>I', int.from_bytes(os.urandom(4), 'big'))
priv_blob = check + check + pack(KEY_TYPE) + pack(pub) + pack(expanded) + pack(b'')
pad_len = (8 - len(priv_blob) % 8) % 8
priv_blob += bytes(range(1, pad_len + 1))
MAGIC = b'openssh-key-v1\x00'
binary = MAGIC + pack(b'none') + pack(b'none') + pack(b'') + struct.pack('>I', 1) + pack(pub_blob) + pack(priv_blob)
b64 = base64.b64encode(binary).decode()
lines = [b64[i:i+70] for i in range(0, len(b64), 70)]
pem = '-----BEGIN OPENSSH PRIVATE KEY-----\n' + '\n'.join(lines) + '\n-----END OPENSSH PRIVATE KEY-----\n'

print(f'ADDR={addr}')
print(f'PEM_B64={base64.b64encode(pem.encode()).decode()}')
")

ONION_ADDR=$(echo "$KEYGEN_OUTPUT" | grep '^ADDR=' | cut -d= -f2)
PEM_B64=$(echo "$KEYGEN_OUTPUT" | grep '^PEM_B64=' | cut -d= -f2)
log "Throwaway address: $ONION_ADDR"

# Install key in service container's Arti keystore
NICKNAME="release_timing_test"
KEYSTORE_DIR="${KEYSTORE_BASE}/hss/${NICKNAME}"
MARKER="# release-test:${ONION_ADDR}"

log "Installing key in Arti keystore..."
docker exec "$SERVICE_CONTAINER" mkdir -p "$KEYSTORE_DIR"
docker exec "$SERVICE_CONTAINER" sh -c "echo '$PEM_B64' | base64 -d > ${KEYSTORE_DIR}/ks_hs_id.ed25519_expanded_private"
docker exec "$SERVICE_CONTAINER" chown -R "$ARTI_USER:$ARTI_USER" "$KEYSTORE_DIR"
docker exec "$SERVICE_CONTAINER" chmod 700 "$KEYSTORE_DIR"
docker exec "$SERVICE_CONTAINER" chmod 600 "${KEYSTORE_DIR}/ks_hs_id.ed25519_expanded_private"
log "Key installed"

# Append onion service config
docker exec "$SERVICE_CONTAINER" sh -c "cat >> $CONTAINER_SERVICE_TOML << SEOF

${MARKER}
[onion_services.\"${NICKNAME}\"]
enabled = true
proxy_ports = [[\"80\", \"127.0.0.1:8080\"]]
SEOF"
docker exec "$SERVICE_CONTAINER" chown "$ARTI_USER:$ARTI_USER" "$CONTAINER_SERVICE_TOML"
log "Service config appended"

# SIGHUP Arti to pick up new config
ARTI_PID=$(docker exec "$SERVICE_CONTAINER" pidof arti 2>/dev/null)
docker exec "$SERVICE_CONTAINER" kill -HUP "$ARTI_PID"
log "Sent SIGHUP to Arti (pid $ARTI_PID) — takeover started"

# ---------------------------------------------------------------------------
step 3 "Measure TAKEOVER TIME (until address is reachable via Tor)"
# ---------------------------------------------------------------------------

log "Polling through client SOCKS proxy..."
log "Descriptor propagation typically takes 30s-10min"

REACHABLE=false
for i in $(seq 1 60); do
    sleep 10
    ELAPSED=$(( $(date +%s) - TAKEOVER_START ))
    CODE=$(docker exec "$CLIENT_CONTAINER" curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${ONION_ADDR}/" 2>/dev/null) || true
    log "  [${ELAPSED}s] HTTP $CODE"
    if [ "$CODE" = "200" ]; then
        REACHABLE=true
        TAKEOVER_TIME=$ELAPSED
        printf "${GREEN}  TAKEOVER COMPLETE — address reachable after ${ELAPSED}s${NC}\n"
        break
    fi
done

if [ "$REACHABLE" = false ]; then
    printf "${RED}Address never became reachable after 600s — aborting${NC}\n"
    exit 1
fi

# Confirm reachability
sleep 5
CODE2=$(docker exec "$CLIENT_CONTAINER" curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${ONION_ADDR}/" 2>/dev/null) || true
log "Confirmation: HTTP $CODE2"

# ---------------------------------------------------------------------------
step 4 "Release: remove config + keystore, SIGHUP Arti"
# ---------------------------------------------------------------------------

RELEASE_TIME=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
RELEASE_EPOCH=$(date +%s)
log "Release timestamp: $RELEASE_TIME"

# Remove onion service config (the marker + 3 lines after it)
docker exec "$SERVICE_CONTAINER" sh -c "
    awk -v m='$MARKER' 'BEGIN{s=0} \$0==m{s=3;next} s>0{s--;next} {print}' \
    $CONTAINER_SERVICE_TOML > ${CONTAINER_SERVICE_TOML}.tmp && \
    mv ${CONTAINER_SERVICE_TOML}.tmp $CONTAINER_SERVICE_TOML"
docker exec "$SERVICE_CONTAINER" chown "$ARTI_USER:$ARTI_USER" "$CONTAINER_SERVICE_TOML"
log "Removed service config"

# Remove keystore
docker exec "$SERVICE_CONTAINER" rm -rf "$KEYSTORE_DIR"
log "Removed keystore"

# SIGHUP Arti
ARTI_PID=$(docker exec "$SERVICE_CONTAINER" pidof arti 2>/dev/null)
docker exec "$SERVICE_CONTAINER" kill -HUP "$ARTI_PID"
log "Sent SIGHUP to Arti (pid $ARTI_PID) — release started"

# ---------------------------------------------------------------------------
step 5 "Measure RELEASE TIME (until address is unreachable)"
# ---------------------------------------------------------------------------

log "Polling every 30s for up to 4 hours..."
log "C Tor sends DESTROY cells here — clients fail in seconds."
log "If Arti doesn't, the descriptor stays functional for up to 3 hours."
echo ""

UNREACHABLE=false
CONSECUTIVE_FAIL=0

POLL_COUNT=$((MAX_POLL_SECONDS / 30))
for i in $(seq 1 $POLL_COUNT); do
    sleep 30
    ELAPSED=$(( $(date +%s) - RELEASE_EPOCH ))
    MINS=$((ELAPSED / 60))
    SECS=$((ELAPSED % 60))
    CODE=$(docker exec "$CLIENT_CONTAINER" curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${ONION_ADDR}/" 2>/dev/null) || true

    if [ "$CODE" != "200" ]; then
        CONSECUTIVE_FAIL=$((CONSECUTIVE_FAIL + 1))
        log "  [${MINS}m ${SECS}s] UNREACHABLE (${CONSECUTIVE_FAIL}/3 consecutive)"
        if [ "$CONSECUTIVE_FAIL" -ge 3 ]; then
            UNREACHABLE=true
            RELEASE_DURATION=$((ELAPSED - 60))  # subtract the 3x30s confirmation window
            printf "\n${GREEN}  RELEASED — address unreachable after ~${MINS}m${NC}\n"
            break
        fi
    else
        CONSECUTIVE_FAIL=0
        log "  [${MINS}m ${SECS}s] Still reachable (HTTP $CODE)"
    fi
done

# ---------------------------------------------------------------------------
echo ""
printf "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
printf "${YELLOW}  Results${NC}\n"
printf "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
echo ""
printf "  Arti version:          $ARTI_VERSION\n"
printf "  Architecture:          $ARCH\n"
printf "  Image:                 $ARTI_IMAGE\n"
printf "  Address:               $ONION_ADDR\n"
echo ""
printf "  TAKEOVER TIME:         ${TAKEOVER_TIME}s (key install + SIGHUP → reachable)\n"

if [ "$UNREACHABLE" = true ]; then
    RELEASE_MINS=$((RELEASE_DURATION / 60))
    printf "  RELEASE TIME:          ~${RELEASE_MINS} minutes (config remove + SIGHUP → unreachable)\n"
else
    TOTAL_MINS=$((MAX_POLL_SECONDS / 60))
    printf "  ${RED}RELEASE TIME:          >${TOTAL_MINS} minutes — STILL REACHABLE!${NC}\n"
    printf "\n  The address was STILL reachable ${TOTAL_MINS} minutes after release.\n"
    printf "  This means an attacker who obtains the key can serve content\n"
    printf "  indefinitely — the legitimate operator cannot revoke it.\n"
fi

echo ""
printf "  ┌─────────────────────────────────────────────────────────┐\n"
printf "  │ C Tor sends DESTROY cells to introduction point relays │\n"
printf "  │ on service removal. The relay tears down the circuit    │\n"
printf "  │ and NACKs subsequent INTRODUCE1 cells. Clients fail    │\n"
printf "  │ within seconds, even though the descriptor persists    │\n"
printf "  │ on HSDirs for up to 3 hours.                           │\n"
printf "  │                                                        │\n"
printf "  │ Arti does not appear to send DESTROY cells on release. │\n"
printf "  │ The intro circuits stay alive, and the service remains │\n"
printf "  │ reachable until the descriptor naturally expires.      │\n"
printf "  └─────────────────────────────────────────────────────────┘\n"
echo ""
printf "  See: https://github.com/brewsterkahle/onionpress/issues/114\n"
echo ""
