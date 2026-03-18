#!/usr/bin/env python3
"""
Worker bootstrap: waits for Arti keys, extracts addresses, registers with OnionHeaven over Tor.

Runs inside each worker container after Arti starts. Each worker self-registers
with OnionHeaven just like a real OnionPress instance would — over Tor, using the
container's own Tor SOCKS proxy.

Usage:
    python3 worker-bootstrap.py <onionheaven_addr> <container_idx> <num_workers> [base_port]
"""

import base64
import json
import os
import random
import struct
import subprocess
import sys
import threading
import time

ONIONHEAVEN_ADDR = sys.argv[1]
CONTAINER_IDX = int(sys.argv[2])
NUM_WORKERS = int(sys.argv[3])
BASE_PORT = int(sys.argv[4]) if len(sys.argv) > 4 else 9100

KEYSTORE_BASE = "/var/lib/arti/state/keystore/hss"
ARTI_TOML = "/etc/arti/arti.toml"
NO_HEALTHCHECK = os.environ.get("NO_HEALTHCHECK", "false").lower() == "true"
USE_CTOR = os.environ.get("TOR_IMPL", "arti").lower() == "tor"
CTOR_HS_BASE = "/var/lib/tor/hidden_service"


def ctor_control(cmd):
    """Send a command to C Tor's control port. Returns the full response."""
    result = subprocess.run(
        ["sh", "-c",
         f'cookie=$(xxd -p /var/lib/tor/control_auth_cookie | tr -d "\\n"); '
         f'printf "AUTHENTICATE %s\\r\\n{cmd}\\r\\nQUIT\\r\\n" "$cookie" | '
         f'nc -w 5 127.0.0.1 9051'],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout


def ctor_add_onion(port):
    """Create an ephemeral onion service via ADD_ONION NEW:ED25519-V3.

    Returns (address, privkey_b64) or (None, None) on failure.
    The address includes .onion suffix. The privkey is the raw base64
    key that can be used with ADD_ONION ED25519-V3:<key> to re-add.
    """
    response = ctor_control(f"ADD_ONION NEW:ED25519-V3 Flags=Detach Port=80,127.0.0.1:{port}")
    service_id = None
    privkey_b64 = None
    for line in response.splitlines():
        if line.startswith("250-ServiceID="):
            service_id = line.split("=", 1)[1].strip()
        elif line.startswith("250-PrivateKey=ED25519-V3:"):
            privkey_b64 = line.split("ED25519-V3:", 1)[1].strip()
    if service_id and privkey_b64:
        return f"{service_id}.onion", privkey_b64
    return None, None


def get_onion_address(nickname):
    """Get .onion address for Arti (reads from Arti CLI). Not used for C Tor."""
    for attempt in range(180):  # up to 6 minutes
        try:
            result = subprocess.run(
                ["su", "-s", "/bin/sh", "arti", "-c",
                 f"arti hss --nickname {nickname} onion-address -c {ARTI_TOML}"],
                capture_output=True, text=True, timeout=10,
            )
            addr = result.stdout.strip()
            if addr and addr.endswith(".onion"):
                return addr
        except Exception:
            pass
        time.sleep(2)
    return None


def parse_openssh_pem(path):
    """Extract raw 32-byte pubkey and 64-byte privkey from OpenSSH PEM file."""
    with open(path, "rb") as f:
        pem = f.read()

    # Strip PEM armor, decode base64
    lines = pem.decode().strip().splitlines()
    b64 = "".join(l for l in lines if not l.startswith("-----"))
    blob = base64.b64decode(b64)

    assert blob[:15] == b"openssh-key-v1\x00", "Not an OpenSSH key"
    pos = 15

    def read_str(data, off):
        ln = struct.unpack_from("!I", data, off)[0]
        return data[off + 4 : off + 4 + ln], off + 4 + ln

    _, pos = read_str(blob, pos)  # cipher
    _, pos = read_str(blob, pos)  # kdf
    _, pos = read_str(blob, pos)  # kdf_options
    pos += 4  # num_keys

    # Public key section
    pub_blob, pos = read_str(blob, pos)
    _, pp = read_str(pub_blob, 0)  # key_type
    pubkey, _ = read_str(pub_blob, pp)  # 32-byte pubkey

    # Private key section
    priv_blob, pos = read_str(blob, pos)
    pp = 8  # skip 2x check ints
    _, pp = read_str(priv_blob, pp)  # key_type
    _, pp = read_str(priv_blob, pp)  # pubkey (again)
    privkey, _ = read_str(priv_blob, pp)  # 64-byte privkey

    return pubkey, privkey


def register_with_onionheaven(content_addr, hc_addr, privkey, pubkey, pem_b64, worker_id=0):
    """Register with OnionHeaven over Tor (via this container's SOCKS proxy).

    Uses exponential backoff: retries up to 6 times with delays of
    5s, 15s, 30s, 60s, 60s between attempts. Total worst-case ~8 minutes
    per worker, but this trades speed for reliability when Tor circuits
    are flaky.

    Each worker uses unique SOCKS auth credentials to force Arti to build
    a separate circuit (stream isolation).
    """
    from onion_auth import sign_payload, make_timestamp
    timestamp = make_timestamp()
    signature = sign_payload(privkey, pubkey, "register", content_addr, hc_addr, timestamp)
    payload = json.dumps({
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "arti_key_pem": pem_b64,
        "version": os.environ.get("STRESS_VERSION", "stress-test"),
        "timestamp": timestamp,
        "signature": signature,
    })

    max_attempts = 6
    backoff = [5, 15, 30, 60, 60]  # delays between attempts

    for attempt in range(max_attempts):
        try:
            result = subprocess.run(
                [
                    "curl", "-s", "-X", "POST",
                    "--socks5-hostname", f"w{worker_id}:x@127.0.0.1:9050",
                    "-H", "Content-Type: application/json",
                    "-d", payload,
                    "--max-time", "90",
                    f"http://{ONIONHEAVEN_ADDR}:8083/register",
                ],
                capture_output=True, text=True, timeout=105,
            )
            try:
                resp = json.loads(result.stdout)
                if resp.get("registered"):
                    return result.stdout
            except (json.JSONDecodeError, ValueError):
                pass
        except Exception:
            pass

        if attempt < max_attempts - 1:
            delay = backoff[min(attempt, len(backoff) - 1)]
            print(f"  Registration attempt {attempt + 1} failed, retrying in {delay}s...", flush=True)
            time.sleep(delay)

    # All attempts exhausted — return last result or error
    try:
        return result.stdout
    except Exception:
        return '{"error": "all attempts exhausted"}'


def wait_for_socks():
    """Wait for Arti's SOCKS proxy to be ready before attempting registration."""
    print("Waiting for Tor SOCKS proxy to be ready...", flush=True)
    for attempt in range(120):  # up to 4 minutes
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--socks5-hostname", "127.0.0.1:9050",
                 "--max-time", "10",
                 f"http://{ONIONHEAVEN_ADDR}/"],
                capture_output=True, text=True, timeout=15,
            )
            # Any response (even error) means SOCKS is working and Tor is connected
            if result.returncode == 0:
                print("SOCKS proxy ready", flush=True)
                return True
        except Exception:
            pass
        time.sleep(2)
    print("WARNING: SOCKS proxy not ready after 4 minutes", flush=True)
    return False


def bootstrap_one_worker(i):
    """Bootstrap a single worker: create onion service, read keys, register over Tor."""
    content_nick = f"w{CONTAINER_IDX}_{i}_content"
    hc_nick = f"w{CONTAINER_IDX}_{i}_hc"
    global_idx = CONTAINER_IDX * NUM_WORKERS + i
    cp = BASE_PORT + i * 2
    hp = BASE_PORT + i * 2 + 1

    if USE_CTOR:
        # C Tor: create services via ADD_ONION (ephemeral, DEL_ONION can remove them)
        print(f"[worker {i}] Creating onion service via ADD_ONION...", flush=True)
        content_addr, content_key_b64 = ctor_add_onion(cp)
        if not content_addr:
            print(f"[worker {i}] ERROR: ADD_ONION failed for content service", flush=True)
            return {
                "global_index": global_idx, "local_index": i,
                "container": CONTAINER_IDX, "registered": False,
                "error": "add_onion_failed",
            }

        if NO_HEALTHCHECK:
            hc_addr = content_addr.replace(content_addr[:8], "hc" + content_addr[2:8])
        else:
            hc_addr, _ = ctor_add_onion(hp)
            if not hc_addr:
                print(f"[worker {i}] ERROR: ADD_ONION failed for healthcheck service", flush=True)
                return {
                    "global_index": global_idx, "local_index": i,
                    "container": CONTAINER_IDX, "registered": False,
                    "error": "add_onion_hc_failed",
                }

        print(f"[worker {i}] content={content_addr} hc={hc_addr}", flush=True)

        # Build Arti PEM from the raw key for OnionHeaven registration
        # The raw key from ADD_ONION is a 64-byte expanded ed25519 key in base64
        raw_key = base64.b64decode(content_key_b64)
        # We need pubkey too — derive from expanded key using onion_auth
        import onion_auth
        a_bytes = raw_key[:32]
        a = int.from_bytes(a_bytes, 'little')
        A = onion_auth._scalar_mult(a, onion_auth._B)
        pubkey = onion_auth._encode_point(A)
        privkey = raw_key  # 64-byte expanded key

        # Build Arti PEM from raw key for OnionHeaven registration.
        # Write C Tor key files to a temp dir, then convert to PEM.
        key_dir = f"/tmp/ctor_keys_{CONTAINER_IDX}_{i}"
        os.makedirs(key_dir, exist_ok=True)
        with open(f"{key_dir}/hs_ed25519_secret_key", "wb") as f:
            f.write(b"== ed25519v1-secret: type0 ==\x00\x00\x00" + raw_key)
        with open(f"{key_dir}/hs_ed25519_public_key", "wb") as f:
            f.write(b"== ed25519v1-public: type0 ==\x00\x00\x00" + pubkey)
        pem_path = f"/tmp/w{CONTAINER_IDX}_{i}_content.pem"
        subprocess.run(
            ["python3", "/key-convert.py", "ctor-to-arti",
             f"{key_dir}/hs_ed25519_secret_key", pem_path],
            capture_output=True, text=True, timeout=10,
        )
        with open(pem_path, "rb") as f:
            pem_b64 = base64.b64encode(f.read()).decode()

    else:
        # Arti: wait for address from Arti CLI
        print(f"[worker {i}] Waiting for Arti addresses...", flush=True)
        content_addr = get_onion_address(content_nick)
        if NO_HEALTHCHECK:
            hc_addr = content_addr.replace(content_addr[:8], "hc" + content_addr[2:8])
        else:
            hc_addr = get_onion_address(hc_nick)

        if not content_addr or not hc_addr:
            print(f"[worker {i}] ERROR: timed out waiting for addresses", flush=True)
            return {
                "global_index": global_idx, "local_index": i,
                "container": CONTAINER_IDX, "registered": False,
                "error": "address_timeout",
            }

        print(f"[worker {i}] content={content_addr} hc={hc_addr}", flush=True)

        pem_path = f"{KEYSTORE_BASE}/{content_nick}/ks_hs_id.ed25519_expanded_private"
        for _ in range(30):
            if os.path.exists(pem_path):
                break
            time.sleep(1)
        if not os.path.exists(pem_path):
            print(f"[worker {i}] ERROR: PEM not found at {pem_path}", flush=True)
            return {
                "global_index": global_idx, "local_index": i,
                "container": CONTAINER_IDX,
                "content_address": content_addr, "healthcheck_address": hc_addr,
                "registered": False, "error": "pem_not_found",
            }

        try:
            pubkey, privkey = parse_openssh_pem(pem_path)
        except Exception as e:
            print(f"[worker {i}] ERROR: failed to parse PEM: {e}", flush=True)
            return {
                "global_index": global_idx, "local_index": i,
                "container": CONTAINER_IDX,
                "content_address": content_addr, "healthcheck_address": hc_addr,
                "registered": False, "error": f"pem_parse: {e}",
            }

        with open(pem_path, "rb") as f:
            pem_data = f.read()
        pem_b64 = base64.b64encode(pem_data).decode()

    print(f"[worker {i}] Registering with OnionHeaven over Tor...", flush=True)
    result = register_with_onionheaven(content_addr, hc_addr, privkey, pubkey, pem_b64, worker_id=global_idx)
    ok = False
    try:
        resp = json.loads(result)
        ok = resp.get("registered", False)
    except Exception:
        pass

    status = "OK" if ok else f"FAILED: {result[:200]}"
    print(f"[worker {i}] Registration: {status}", flush=True)

    return {
        "global_index": global_idx, "local_index": i,
        "container": CONTAINER_IDX,
        "content_address": content_addr, "healthcheck_address": hc_addr,
        "content_port": cp,
        "hc_port": hp,
        "registered": ok,
        "privkey_b64": base64.b64encode(privkey).decode(),
        "pubkey_b64": base64.b64encode(pubkey).decode(),
        "ctor_key_b64": content_key_b64 if USE_CTOR else "",
    }


def main():
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Wait for Tor SOCKS to be functional before registering any workers
    wait_for_socks()

    # Bootstrap all workers in parallel (10 concurrent to avoid overwhelming SOCKS)
    max_parallel = min(10, NUM_WORKERS)
    print(f"Bootstrapping {NUM_WORKERS} workers ({max_parallel} parallel)...", flush=True)

    workers = [None] * NUM_WORKERS
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {pool.submit(bootstrap_one_worker, i): i for i in range(NUM_WORKERS)}
        for future in as_completed(futures):
            i = futures[future]
            workers[i] = future.result()
            # Write incrementally so the stress test can track progress per-site
            with lock:
                with open("/worker-info.json", "w") as f:
                    json.dump([w for w in workers if w is not None], f, indent=2)

    registered_workers = [w for w in workers if w.get("registered")]
    print(f"Bootstrap complete: {len(registered_workers)}/{len(workers)} registered", flush=True)

    # Start heartbeat loop — sends /online for each registered worker every 60s
    if registered_workers:
        heartbeat_loop(registered_workers)


def send_heartbeat(worker):
    """Send a single /online heartbeat for a worker."""
    from onion_auth import sign_payload, make_timestamp

    privkey = base64.b64decode(worker["privkey_b64"])
    pubkey = base64.b64decode(worker["pubkey_b64"])
    ca = worker["content_address"]
    ha = worker["healthcheck_address"]

    timestamp = make_timestamp()
    signature = sign_payload(privkey, pubkey, "online", ca, ha, timestamp)
    payload = json.dumps({
        "content_address": ca,
        "healthcheck_address": ha,
        "timestamp": timestamp,
        "signature": signature,
        "wordpress_healthy": True,
    })

    # Use unique SOCKS credentials per worker for circuit isolation
    worker_id = worker.get("global_index", worker.get("local_index", 0))
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-X", "POST",
                "--socks5-hostname", f"w{worker_id}:x@127.0.0.1:9050",
                "-H", "Content-Type: application/json",
                "-d", payload,
                "--max-time", "30",
                f"http://{ONIONHEAVEN_ADDR}:8083/online",
            ],
            capture_output=True, text=True, timeout=45,
        )
        return result.stdout
    except Exception as e:
        return f'{{"error": "{e}"}}'


def is_worker_enabled(worker):
    """Check if this worker's HTTP responder is still enabled (not disabled by stress test)."""
    cp = worker["content_port"]
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "2", f"http://127.0.0.1:{cp}/"],
            capture_output=True, text=True, timeout=5,
        )
        # If we get a response, the worker is enabled
        return result.returncode == 0 and result.stdout.strip() != ""
    except Exception:
        return False


def heartbeat_loop(registered_workers):
    """Send periodic /online heartbeats for all enabled registered workers."""
    HEARTBEAT_INTERVAL = 60

    # Initial jitter to avoid thundering herd
    jitter = random.uniform(0, 15)
    print(f"Heartbeat loop starting for {len(registered_workers)} workers (first beat in {jitter:.0f}s)...", flush=True)
    time.sleep(jitter)

    while True:
        enabled = 0
        skipped = 0
        errors = 0

        for w in registered_workers:
            if not is_worker_enabled(w):
                skipped += 1
                continue

            result = send_heartbeat(w)
            try:
                resp = json.loads(result)
                if resp.get("online"):
                    enabled += 1
                else:
                    errors += 1
                    print(f"  heartbeat rejected for worker {w['local_index']}: {result[:100]}", flush=True)
            except Exception:
                errors += 1

            # Small stagger between workers
            time.sleep(0.5)

        print(f"Heartbeat: {enabled} sent, {skipped} disabled, {errors} errors", flush=True)
        time.sleep(HEARTBEAT_INTERVAL)


if __name__ == "__main__":
    main()
