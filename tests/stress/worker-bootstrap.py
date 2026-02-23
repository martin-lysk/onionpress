#!/usr/bin/env python3
"""
Worker bootstrap: waits for Arti keys, extracts addresses, registers with cellar over Tor.

Runs inside each worker container after Arti starts. Each worker self-registers
with the cellar just like a real OnionPress instance would — over Tor, using the
container's own Arti SOCKS proxy.

Usage:
    python3 worker-bootstrap.py <cellar_addr> <container_idx> <num_workers> [base_port]
"""

import base64
import json
import os
import struct
import subprocess
import sys
import time

CELLAR_ADDR = sys.argv[1]
CONTAINER_IDX = int(sys.argv[2])
NUM_WORKERS = int(sys.argv[3])
BASE_PORT = int(sys.argv[4]) if len(sys.argv) > 4 else 9100

KEYSTORE_BASE = "/var/lib/arti/state/keystore/hss"
ARTI_TOML = "/etc/arti/arti.toml"


def get_onion_address(nickname):
    """Get .onion address from Arti CLI. Retries until available."""
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


def register_with_cellar(content_addr, hc_addr, secret_b64, public_b64, pem_b64):
    """Register with cellar over Tor (via this container's SOCKS proxy).

    Uses exponential backoff: retries up to 6 times with delays of
    5s, 15s, 30s, 60s, 60s between attempts. Total worst-case ~8 minutes
    per worker, but this trades speed for reliability when Tor circuits
    are flaky.
    """
    payload = json.dumps({
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "secret_key": secret_b64,
        "public_key": public_b64,
        "arti_key_pem": pem_b64,
        "version": "stress-test",
    })

    max_attempts = 6
    backoff = [5, 15, 30, 60, 60]  # delays between attempts

    for attempt in range(max_attempts):
        try:
            result = subprocess.run(
                [
                    "curl", "-s", "-X", "POST",
                    "--socks5-hostname", "127.0.0.1:9050",
                    "-H", "Content-Type: application/json",
                    "-d", payload,
                    "--max-time", "90",
                    f"http://{CELLAR_ADDR}/register",
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
    print("Waiting for Arti SOCKS proxy to be ready...", flush=True)
    for attempt in range(120):  # up to 4 minutes
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--socks5-hostname", "127.0.0.1:9050",
                 "--max-time", "10",
                 f"http://{CELLAR_ADDR}/"],
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


def main():
    workers = []

    # Wait for Arti SOCKS to be functional before registering any workers
    wait_for_socks()

    for i in range(NUM_WORKERS):
        content_nick = f"w{CONTAINER_IDX}_{i}_content"
        hc_nick = f"w{CONTAINER_IDX}_{i}_hc"

        print(f"[worker {i}] Waiting for Arti addresses...", flush=True)
        content_addr = get_onion_address(content_nick)
        hc_addr = get_onion_address(hc_nick)

        if not content_addr or not hc_addr:
            print(f"[worker {i}] ERROR: timed out waiting for addresses", flush=True)
            workers.append({
                "index": CONTAINER_IDX * NUM_WORKERS + i,
                "local_index": i,
                "container": CONTAINER_IDX,
                "registered": False,
                "error": "address_timeout",
            })
            continue

        print(f"[worker {i}] content={content_addr} hc={hc_addr}", flush=True)

        # Read PEM and extract raw keys
        pem_path = f"{KEYSTORE_BASE}/{content_nick}/ks_hs_id.ed25519_expanded_private"

        # Wait for keystore file (Arti creates it slightly after address is available)
        for _ in range(30):
            if os.path.exists(pem_path):
                break
            time.sleep(1)

        if not os.path.exists(pem_path):
            print(f"[worker {i}] ERROR: PEM not found at {pem_path}", flush=True)
            workers.append({
                "index": CONTAINER_IDX * NUM_WORKERS + i,
                "local_index": i,
                "container": CONTAINER_IDX,
                "content_address": content_addr,
                "healthcheck_address": hc_addr,
                "registered": False,
                "error": "pem_not_found",
            })
            continue

        try:
            pubkey, privkey = parse_openssh_pem(pem_path)
        except Exception as e:
            print(f"[worker {i}] ERROR: failed to parse PEM: {e}", flush=True)
            workers.append({
                "index": CONTAINER_IDX * NUM_WORKERS + i,
                "local_index": i,
                "container": CONTAINER_IDX,
                "content_address": content_addr,
                "healthcheck_address": hc_addr,
                "registered": False,
                "error": f"pem_parse: {e}",
            })
            continue

        with open(pem_path, "rb") as f:
            pem_data = f.read()

        secret_b64 = base64.b64encode(privkey).decode()
        public_b64 = base64.b64encode(pubkey).decode()
        pem_b64 = base64.b64encode(pem_data).decode()

        # Self-register with cellar over Tor (retries with backoff inside)
        print(f"[worker {i}] Registering with cellar over Tor...", flush=True)
        result = register_with_cellar(content_addr, hc_addr, secret_b64, public_b64, pem_b64)
        ok = False
        try:
            resp = json.loads(result)
            ok = resp.get("registered", False)
        except Exception:
            pass

        status = "OK" if ok else f"FAILED: {result[:200]}"
        print(f"[worker {i}] Registration: {status}", flush=True)

        # Stagger between workers to avoid overwhelming Tor circuits
        if i < NUM_WORKERS - 1:
            time.sleep(2)

        workers.append({
            "index": CONTAINER_IDX * NUM_WORKERS + i,
            "local_index": i,
            "container": CONTAINER_IDX,
            "content_address": content_addr,
            "healthcheck_address": hc_addr,
            "content_port": BASE_PORT + i * 2,
            "hc_port": BASE_PORT + i * 2 + 1,
            "registered": ok,
        })

    # Write info for stress test script to read
    with open("/worker-info.json", "w") as f:
        json.dump(workers, f, indent=2)

    registered = sum(1 for w in workers if w.get("registered"))
    print(f"Bootstrap complete: {registered}/{len(workers)} registered", flush=True)


if __name__ == "__main__":
    main()
