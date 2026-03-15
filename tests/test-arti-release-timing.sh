#!/bin/bash
#
# Arti Onion Service Release Timing Test
# https://github.com/brewsterkahle/onionpress/issues/114
#
# Standalone test — only requires Docker. No OnionPress needed.
# Runs its own Arti containers, generates a faux onion service,
# publishes it, then removes it and measures how long until clients
# can no longer reach it.
#
# Steps:
#   1. Start two Arti containers: one to host the service, one as a client
#   2. Generate a faux ed25519 key, install it, and start the service
#   3. Wait for the address to become reachable via Tor
#   4. Remove the service config and keystore, SIGHUP Arti (release)
#   5. Poll until the address becomes unreachable, recording elapsed time
#
# Expected result with C Tor: seconds (DESTROY cells tear down intro circuits)
# Observed result with Arti: 3+ hours (descriptor cached on HSDirs, intro alive)
#
# Usage:
#   ./test-arti-release-timing.sh
#
# Cleanup:
#   docker rm -f arti-release-test-service arti-release-test-client 2>/dev/null
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_START=$(date +%s)
TMPDIR_TEST=$(mktemp -d)
NETWORK_NAME="arti-release-test-net"
SERVICE_CONTAINER="arti-release-test-service"
CLIENT_CONTAINER="arti-release-test-client"

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
printf "${YELLOW}Arti Onion Service Release Timing Test${NC}\n"
printf "${YELLOW}=======================================${NC}\n"
printf "Measures how long a released Arti onion service remains reachable.\n"
printf "See: https://github.com/brewsterkahle/onionpress/issues/114\n"
echo ""

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

log "Running pre-flight checks..."

if ! docker info >/dev/null 2>&1; then
    printf "${RED}Docker not accessible${NC}\n"
    exit 1
fi
log "Docker accessible"

# Check architecture
ARCH=$(uname -m)
log "Architecture: $ARCH"

# ---------------------------------------------------------------------------
step 1 "Start Arti containers"
# ---------------------------------------------------------------------------

# Clean up any previous run
docker rm -f "$SERVICE_CONTAINER" "$CLIENT_CONTAINER" 2>/dev/null || true
docker network rm "$NETWORK_NAME" 2>/dev/null || true

# Create isolated network
docker network create "$NETWORK_NAME" >/dev/null
log "Created network: $NETWORK_NAME"

# Write Arti configs
SERVICE_TOML="$TMPDIR_TEST/arti-service.toml"
CLIENT_TOML="$TMPDIR_TEST/arti-client.toml"

cat > "$SERVICE_TOML" << 'EOF'
[logging]
console = "info,tor_hsservice=debug"

[proxy]
socks_listen = "0.0.0.0:9050"

[storage]
cache_dir = "/var/lib/arti/cache"
state_dir = "/var/lib/arti/state"

[storage.keystore]
enabled = true

[vanguards]
mode = "disabled"
EOF

cat > "$CLIENT_TOML" << 'EOF'
[logging]
console = "info"

[proxy]
socks_listen = "0.0.0.0:9050"

[storage]
cache_dir = "/var/lib/arti/cache"
state_dir = "/var/lib/arti/state"

[address_filter]
allow_onion_addrs = true
EOF

# Write a minimal HTTP server that returns 200 (the "service" we're publishing)
HTTPD_SCRIPT="$TMPDIR_TEST/httpd.sh"
cat > "$HTTPD_SCRIPT" << 'HTTPEOF'
#!/bin/sh
# Minimal HTTP server on port 8080 using socat
echo "HTTP test server starting on port 8080..."
socat TCP-LISTEN:8080,reuseaddr,fork SYSTEM:'echo "HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 13\r\nConnection: close\r\n\r\nrelease-test!" '
HTTPEOF
chmod +x "$HTTPD_SCRIPT"

# Pull the Arti image (use Tor Project's official or build from OnionPress)
ARTI_IMAGE="ghcr.io/brewsterkahle/onionpress-tor:latest"
log "Pulling Arti image: $ARTI_IMAGE"
docker pull "$ARTI_IMAGE" >/dev/null 2>&1 || {
    # Fallback: try building from the OnionPress Dockerfile if available
    if [ -f "$SCRIPT_DIR/../OnionPress.app/Contents/Resources/docker/tor/Dockerfile" ]; then
        log "Pull failed, building from local Dockerfile..."
        docker build -t "$ARTI_IMAGE" "$SCRIPT_DIR/../OnionPress.app/Contents/Resources/docker/tor/" >/dev/null 2>&1
    else
        printf "${RED}Cannot pull or build Arti image${NC}\n"
        exit 1
    fi
}

# Start the service container (Arti + HTTP server)
log "Starting service container..."
docker run -d --name "$SERVICE_CONTAINER" \
    --network "$NETWORK_NAME" \
    -v "$SERVICE_TOML:/etc/arti/arti-service.toml:ro" \
    -v "$HTTPD_SCRIPT:/httpd.sh:ro" \
    "$ARTI_IMAGE" sh -c "
        mkdir -p /var/lib/arti/cache /var/lib/arti/state
        chown -R arti:arti /var/lib/arti
        chmod 700 /var/lib/arti /var/lib/arti/cache /var/lib/arti/state
        # Start HTTP test server
        socat TCP-LISTEN:8080,reuseaddr,fork SYSTEM:'printf \"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 13\r\nConnection: close\r\n\r\nrelease-test!\"' &
        # Start Arti
        su -s /bin/sh arti -c 'arti proxy -c /etc/arti/arti-service.toml'
    " >/dev/null
sleep 3
if docker ps --format '{{.Names}}' | grep -q "^${SERVICE_CONTAINER}$"; then
    log "Service container running"
else
    printf "${RED}Service container failed to start${NC}\n"
    docker logs "$SERVICE_CONTAINER" 2>&1 | tail -20
    exit 1
fi

# Start the client container (Arti SOCKS proxy only)
log "Starting client container..."
docker run -d --name "$CLIENT_CONTAINER" \
    --network "$NETWORK_NAME" \
    -v "$CLIENT_TOML:/etc/arti/arti-client.toml:ro" \
    "$ARTI_IMAGE" sh -c "
        mkdir -p /var/lib/arti/cache /var/lib/arti/state
        chown -R arti:arti /var/lib/arti
        chmod 700 /var/lib/arti /var/lib/arti/cache /var/lib/arti/state
        su -s /bin/sh arti -c 'arti proxy -c /etc/arti/arti-client.toml'
    " >/dev/null
sleep 3
if docker ps --format '{{.Names}}' | grep -q "^${CLIENT_CONTAINER}$"; then
    log "Client container running"
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
    # Check if SOCKS proxy is responding
    if docker exec "$SERVICE_CONTAINER" curl -s --max-time 3 --socks5-hostname 127.0.0.1:9050 http://www.example.com/ >/dev/null 2>&1; then
        log "Service Arti bootstrapped"
        break
    fi
    if [ "$i" -eq 24 ]; then
        printf "${RED}Service Arti failed to bootstrap after 120s${NC}\n"
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
        exit 1
    fi
done

# ---------------------------------------------------------------------------
step 2 "Generate faux key and install onion service"
# ---------------------------------------------------------------------------

# Generate ed25519 keypair using Python (inline, no external dependencies)
log "Generating faux ed25519 keypair..."
KEYGEN_OUTPUT=$(python3 -c "
import hashlib, os, struct, base64

# Pure-Python ed25519 scalar mult (minimal, for key generation only)
# We only need to derive public key from seed — no signing needed here.
# Use Python's pow() for modular arithmetic on Curve25519.

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

# Generate expanded key
seed = os.urandom(32)
h = hashlib.sha512(seed).digest()
a_bytes = bytearray(h[:32])
a_bytes[0] &= 248; a_bytes[31] &= 127; a_bytes[31] |= 64
expanded = bytes(a_bytes) + h[32:]
a = int.from_bytes(a_bytes, 'little')
A = scalar_mult(a, B)
pub = encode_point(A)

# Derive .onion address (v3)
import hashlib as hl
version = b'\x03'
checksum = hl.sha3_256(b'.onion checksum' + pub + version).digest()[:2]
addr = base64.b32encode(pub + checksum + version).decode().lower() + '.onion'

# Build Arti PEM
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

CONTENT_ADDR=$(echo "$KEYGEN_OUTPUT" | grep '^ADDR=' | cut -d= -f2)
PEM_B64=$(echo "$KEYGEN_OUTPUT" | grep '^PEM_B64=' | cut -d= -f2)
log "Faux address: $CONTENT_ADDR"

# Install key in service container's Arti keystore
ADDR_PREFIX=$(echo "$CONTENT_ADDR" | sed 's/\.onion$//' | cut -c1-16)
NICKNAME="release_test_${ADDR_PREFIX}"
KEYSTORE_DIR="/var/lib/arti/state/keystore/hss/${NICKNAME}"
ARTI_TOML="/etc/arti/arti-service.toml"
MARKER="# release-test:${CONTENT_ADDR}"

log "Installing key in Arti keystore..."
docker exec "$SERVICE_CONTAINER" mkdir -p "$KEYSTORE_DIR"
docker exec "$SERVICE_CONTAINER" sh -c "echo '$PEM_B64' | base64 -d > ${KEYSTORE_DIR}/ks_hs_id.ed25519_expanded_private"
docker exec "$SERVICE_CONTAINER" chown -R arti:arti "$KEYSTORE_DIR"
docker exec "$SERVICE_CONTAINER" chmod 700 "$KEYSTORE_DIR"
docker exec "$SERVICE_CONTAINER" chmod 600 "${KEYSTORE_DIR}/ks_hs_id.ed25519_expanded_private"
log "Key installed"

# Add onion service config
docker exec "$SERVICE_CONTAINER" sh -c "cat >> $ARTI_TOML << SEOF

${MARKER}
[onion_services.\"${NICKNAME}\"]
enabled = true
proxy_ports = [[\"80\", \"127.0.0.1:8080\"]]
SEOF"
log "Service config added to $ARTI_TOML"

# SIGHUP Arti
ARTI_PID=$(docker exec "$SERVICE_CONTAINER" pidof arti 2>/dev/null)
docker exec "$SERVICE_CONTAINER" kill -HUP "$ARTI_PID"
log "Sent SIGHUP to Arti (pid $ARTI_PID)"

# ---------------------------------------------------------------------------
step 3 "Wait for address to become reachable via Tor"
# ---------------------------------------------------------------------------

log "Polling faux address through client's SOCKS proxy..."
log "Descriptor propagation typically takes 30s-10min"

REACHABLE=false
PROP_START=$(date +%s)

for i in $(seq 1 60); do
    sleep 10
    ELAPSED=$(( $(date +%s) - PROP_START ))
    CODE=$(docker exec "$CLIENT_CONTAINER" curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${CONTENT_ADDR}/" 2>/dev/null || echo "000")
    log "  [${ELAPSED}s] HTTP $CODE"
    if [ "$CODE" = "200" ]; then
        REACHABLE=true
        PROPAGATION_TIME=$ELAPSED
        printf "${GREEN}Address reachable via Tor after ${ELAPSED}s (HTTP $CODE)${NC}\n"
        break
    fi
done

if [ "$REACHABLE" = false ]; then
    printf "${RED}Address never became reachable after 600s — aborting${NC}\n"
    exit 1
fi

# Confirm with a second request
sleep 5
CODE2=$(docker exec "$CLIENT_CONTAINER" curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${CONTENT_ADDR}/" 2>/dev/null || echo "000")
log "Confirmation: HTTP $CODE2"

# ---------------------------------------------------------------------------
step 4 "Release: remove config + keystore, SIGHUP Arti"
# ---------------------------------------------------------------------------

RELEASE_TIME=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
RELEASE_EPOCH=$(date +%s)
log "Release timestamp: $RELEASE_TIME"

# Remove service config
docker exec "$SERVICE_CONTAINER" sh -c "awk -v m='$MARKER' 'BEGIN{s=0} \$0==m{s=3;next} s>0{s--;next} {print}' $ARTI_TOML > ${ARTI_TOML}.tmp && mv ${ARTI_TOML}.tmp $ARTI_TOML"
log "Removed service config"

# Remove keystore
docker exec "$SERVICE_CONTAINER" rm -rf "$KEYSTORE_DIR"
log "Removed keystore"

# SIGHUP Arti
ARTI_PID=$(docker exec "$SERVICE_CONTAINER" pidof arti 2>/dev/null)
docker exec "$SERVICE_CONTAINER" kill -HUP "$ARTI_PID"
log "Sent SIGHUP to Arti (pid $ARTI_PID)"

# ---------------------------------------------------------------------------
step 5 "Poll until address becomes unreachable"
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
    CODE=$(docker exec "$CLIENT_CONTAINER" curl -s -o /dev/null -w "%{http_code}" --max-time 30 --socks5-hostname 127.0.0.1:9050 "http://${CONTENT_ADDR}/" 2>/dev/null || echo "000")

    if [ "$CODE" = "000" ]; then
        CONSECUTIVE_FAIL=$((CONSECUTIVE_FAIL + 1))
        log "  [${MINS}m ${SECS}s] UNREACHABLE (${CONSECUTIVE_FAIL}/3 consecutive)"
        if [ "$CONSECUTIVE_FAIL" -ge 3 ]; then
            UNREACHABLE=true
            RELEASE_DURATION=$((ELAPSED - 60))
            printf "\n${GREEN}Address became unreachable after ~${MINS}m${NC}\n"
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
printf "  Propagation time:      ${PROPAGATION_TIME}s (address became reachable)\n"
printf "  Release method:        remove config + keystore, SIGHUP\n"

if [ "$UNREACHABLE" = true ]; then
    RELEASE_MINS=$((RELEASE_DURATION / 60))
    printf "  ${GREEN}Release duration:      ~${RELEASE_MINS} minutes${NC}\n"
    printf "\n  Address became unreachable ~${RELEASE_MINS} minutes after release.\n"
else
    TOTAL_MINS=$((MAX_POLL_SECONDS / 60))
    printf "  ${RED}Release duration:      >${TOTAL_MINS} minutes (still reachable!)${NC}\n"
    printf "\n  The address was STILL reachable after ${TOTAL_MINS} minutes.\n"
    printf "  Descriptors cached on HSDirs have a 3-hour lifetime.\n"
fi

echo ""
printf "  For comparison, C Tor sends DESTROY cells to introduction point\n"
printf "  relays on service removal. The relay removes the circuit from its\n"
printf "  map and NACKs subsequent INTRODUCE1 cells with UNKNOWN_ID.\n"
printf "  Clients fail within seconds, even though the descriptor persists.\n"
echo ""
printf "  See: https://github.com/brewsterkahle/onionpress/issues/114\n"
echo ""
