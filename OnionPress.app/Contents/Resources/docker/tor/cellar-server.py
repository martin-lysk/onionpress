#!/usr/bin/env python3
"""
OnionCellar Registration API Server

Lightweight HTTP server (Python stdlib only) that handles cellar registration,
unregistration, and lifecycle notifications. Runs inside the onioncellar
container on port 8083, exposed through the main tor container's onion service.

Endpoints:
  POST /register     — Register an OnionPress instance with the cellar
  POST /unregister   — Remove a registration
  POST /online       — Notify cellar that instance is back online
  POST /offline      — Notify cellar that instance is going offline
  GET  /status       — Public status summary (no auth)
"""

import base64
import hashlib
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8083

CELLAR_DATA_DIR = "/var/lib/onionpress/cellar"
CELLAR_DB_PATH = os.path.join(CELLAR_DATA_DIR, "registry.db")
CELLAR_KEYS_DIR = os.path.join(CELLAR_DATA_DIR, "keys")

ONION_RE = re.compile(r"^[a-z2-7]{56}\.onion$")
TOR_MANAGER = "/cellar-tor-manager.sh"


def immediate_release(content_address, conn):
    """If a takeover is active for this content_address, release it immediately.

    Called from /register and /online so the cellar stops serving the takeover
    onion service as soon as the worker is confirmed alive. This lets the
    worker's fresh descriptor win in the Tor DHT faster.

    Returns True if a release was performed, False otherwise.
    """
    rows = conn.execute(
        "SELECT takeover_active FROM registry WHERE content_address = ?",
        (content_address,)
    ).fetchall()
    if not any(row["takeover_active"] for row in rows):
        return False

    try:
        result = subprocess.run(
            [TOR_MANAGER, "release", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            conn.execute(
                "UPDATE registry SET takeover_active = 0 WHERE content_address = ?",
                (content_address,)
            )
            conn.commit()
            sys.stderr.write(
                f"[immediate_release] Released takeover for {content_address}\n"
            )
            sys.stderr.flush()
            return True
        else:
            sys.stderr.write(
                f"[immediate_release] Failed for {content_address}: {result.stderr}\n"
            )
            sys.stderr.flush()
            return False
    except Exception as e:
        sys.stderr.write(
            f"[immediate_release] Error for {content_address}: {e}\n"
        )
        sys.stderr.flush()
        return False

# ---------------------------------------------------------------------------
# SQLite helpers (same schema as cellar-poller.py)
# ---------------------------------------------------------------------------

def db_connect():
    """Open the cellar SQLite database with WAL mode."""
    os.makedirs(CELLAR_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(CELLAR_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def db_ensure_schema(conn):
    """Create registry table if it doesn't exist."""
    conn.execute("""CREATE TABLE IF NOT EXISTS registry (
        content_address     TEXT NOT NULL,
        healthcheck_address TEXT NOT NULL,
        registered_at       TEXT NOT NULL,
        version             TEXT NOT NULL DEFAULT 'unknown',
        status              TEXT NOT NULL DEFAULT 'healthy',
        last_contact        TEXT,
        last_redirect       TEXT,
        fail_count          INTEGER NOT NULL DEFAULT 0,
        takeover_active     INTEGER NOT NULL DEFAULT 0,
        fast_poll_remaining INTEGER NOT NULL DEFAULT 0,
        key_hash            TEXT,
        PRIMARY KEY (content_address, healthcheck_address)
    )""")
    # Add columns that may be missing on older tables
    cols = [row[1] for row in conn.execute("PRAGMA table_info(registry)").fetchall()]
    if "key_hash" not in cols:
        conn.execute("ALTER TABLE registry ADD COLUMN key_hash TEXT")
    if "last_redirect" not in cols:
        conn.execute("ALTER TABLE registry ADD COLUMN last_redirect TEXT")
    conn.commit()


# ---------------------------------------------------------------------------
# OpenSSH PEM key builder (reimplemented from key_manager.py)
# ---------------------------------------------------------------------------

OPENSSH_MAGIC = b"openssh-key-v1\x00"
ARTI_KEY_TYPE = b"ed25519-expanded@spec.torproject.org"


def _pack_string(data):
    """Pack bytes as uint32 big-endian length + data."""
    return struct.pack(">I", len(data)) + data


def build_openssh_key(private_key, public_key):
    """Build an OpenSSH PEM private key for Arti from raw Ed25519 keys.

    private_key: 64 bytes (expanded Ed25519)
    public_key: 32 bytes
    Returns bytes (PEM-encoded).
    """
    # Build public key blob
    pub_blob = _pack_string(ARTI_KEY_TYPE) + _pack_string(public_key)

    # Build private key blob
    check = struct.pack(">I", int.from_bytes(os.urandom(4), "big"))
    priv_blob = (
        check + check +
        _pack_string(ARTI_KEY_TYPE) +
        _pack_string(public_key) +
        _pack_string(private_key) +
        _pack_string(b"")  # empty comment
    )
    # Pad to 8-byte boundary
    pad_len = (8 - len(priv_blob) % 8) % 8
    priv_blob += bytes(range(1, pad_len + 1))

    binary = (
        OPENSSH_MAGIC +
        _pack_string(b"none") +
        _pack_string(b"none") +
        _pack_string(b"") +
        struct.pack(">I", 1) +
        _pack_string(pub_blob) +
        _pack_string(priv_blob)
    )

    b64 = base64.b64encode(binary).decode("ascii")
    lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    pem += "\n".join(lines) + "\n"
    pem += "-----END OPENSSH PRIVATE KEY-----\n"
    return pem.encode("utf-8")


# ---------------------------------------------------------------------------
# Tor v3 address derivation
# ---------------------------------------------------------------------------

BASE32_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def base32_encode(data):
    """RFC 4648 base32 encode (lowercase, no padding)."""
    bits = ""
    for byte in data:
        bits += format(byte, "08b")
    result = []
    for i in range(0, len(bits) - 4, 5):
        result.append(BASE32_ALPHABET[int(bits[i:i + 5], 2)])
    return "".join(result)


def derive_onion_address(public_key_32):
    """Derive a Tor v3 .onion address from a 32-byte Ed25519 public key."""
    checksum_input = b".onion checksum" + public_key_32 + b"\x03"
    checksum = hashlib.sha3_256(checksum_input).digest()[:2]
    addr_bytes = public_key_32 + checksum + b"\x03"
    return base32_encode(addr_bytes) + ".onion"


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def is_local_request(handler):
    """Check if request is from localhost or Docker network (skip auth)."""
    addr = handler.client_address[0]
    return (
        addr == "127.0.0.1"
        or addr == "::1"
        or addr.startswith("172.")
        or addr.startswith("10.")
    )


def verify_proof(stored_hash, proof):
    """Constant-time comparison of proof against stored key_hash."""
    if not stored_hash or not proof:
        return False
    # Use hmac.compare_digest for constant-time comparison
    import hmac
    return hmac.compare_digest(stored_hash, proof)


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class CellarHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        """Override to add timestamp prefix."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{ts}] cellar-server: {format % args}\n")
        sys.stderr.flush()

    def _send_json(self, status_code, data):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None
        body = self.rfile.read(length)
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None

    # -- GET /status --------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?")[0]
        if path != "/status":
            self._send_json(404, {"error": "Not found"})
            return

        try:
            conn = db_connect()
            db_ensure_schema(conn)
            total = conn.execute("SELECT COUNT(*) FROM registry").fetchone()[0]
            healthy = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE status='healthy'"
            ).fetchone()[0]
            failing = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE status='failing'"
            ).fetchone()[0]
            taken_over = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE takeover_active=1"
            ).fetchone()[0]
            conn.close()
            self._send_json(200, {
                "total": total,
                "healthy": healthy,
                "failing": failing,
                "taken_over": taken_over,
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # -- POST dispatch ------------------------------------------------------

    def do_POST(self):
        path = self.path.split("?")[0]
        handlers = {
            "/register": self._handle_register,
            "/unregister": self._handle_unregister,
            "/online": self._handle_online,
            "/offline": self._handle_offline,
        }
        handler = handlers.get(path)
        if handler is None:
            self._send_json(404, {"error": "Not found"})
            return
        try:
            handler()
        except Exception as e:
            self.log_message("ERROR in %s: %s", path, e)
            self._send_json(500, {"error": str(e)})

    # -- POST /register -----------------------------------------------------

    def _handle_register(self):
        data = self._read_json()
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        # Validate required fields
        for field in ("content_address", "healthcheck_address", "secret_key", "public_key"):
            if not data.get(field):
                self._send_json(400, {"error": f"Missing required field: {field}"})
                return

        content_address = data["content_address"]
        healthcheck_address = data["healthcheck_address"]
        version = data.get("version", "unknown")

        # Validate address format
        if not ONION_RE.match(content_address):
            self._send_json(400, {"error": "Invalid content_address format"})
            return
        if not ONION_RE.match(healthcheck_address):
            self._send_json(400, {"error": "Invalid healthcheck_address format"})
            return

        # Decode and validate keys
        try:
            secret_key = base64.b64decode(data["secret_key"])
            public_key = base64.b64decode(data["public_key"])
        except Exception:
            self._send_json(400, {"error": "Invalid base64 key encoding"})
            return

        if len(secret_key) != 64:
            self._send_json(400, {
                "error": f"Invalid secret_key length: expected 64 bytes, got {len(secret_key)}"
            })
            return

        # Handle 32 or 64-byte public key (64 = 32-byte Tor header + 32-byte key)
        if len(public_key) == 64:
            raw_pubkey = public_key[32:]
        elif len(public_key) == 32:
            raw_pubkey = public_key
        else:
            self._send_json(400, {
                "error": f"Invalid public_key length: expected 32 or 64 bytes, got {len(public_key)}"
            })
            return

        # Verify content_address matches public_key
        derived_address = derive_onion_address(raw_pubkey)
        if derived_address != content_address:
            self._send_json(400, {
                "error": "content_address does not match public_key",
                "expected": derived_address,
            })
            return

        # Build or validate Arti PEM
        if data.get("arti_key_pem"):
            try:
                arti_pem = base64.b64decode(data["arti_key_pem"])
            except Exception:
                self._send_json(400, {"error": "Invalid arti_key_pem base64"})
                return
            if not arti_pem.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
                self._send_json(400, {"error": "Invalid arti_key_pem format"})
                return
        else:
            arti_pem = build_openssh_key(secret_key, raw_pubkey)

        # Store plaintext PEM key
        keys_dir = os.path.join(CELLAR_KEYS_DIR, content_address)
        os.makedirs(keys_dir, mode=0o700, exist_ok=True)

        pem_path = os.path.join(keys_dir, "ks_hs_id.ed25519_expanded_private")
        with open(pem_path, "wb") as f:
            f.write(arti_pem)
        os.chmod(pem_path, 0o600)

        # Write hostname file
        hostname_path = os.path.join(keys_dir, "hostname")
        with open(hostname_path, "w") as f:
            f.write(content_address + "\n")
        os.chmod(hostname_path, 0o600)

        # Remove old encrypted files if present (migration cleanup)
        for old_file in ("ks_hs_id.ed25519_expanded_private.enc",
                         "hs_ed25519_secret_key.enc", "hs_ed25519_public_key.enc",
                         "hs_ed25519_secret_key", "hs_ed25519_public_key"):
            old_path = os.path.join(keys_dir, old_file)
            try:
                os.unlink(old_path)
            except FileNotFoundError:
                pass

        # Compute key_hash for auth
        key_hash = hashlib.sha256(secret_key).hexdigest()

        # Upsert into registry
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = db_connect()
        db_ensure_schema(conn)

        existing = conn.execute(
            "SELECT 1 FROM registry WHERE content_address = ? AND healthcheck_address = ?",
            (content_address, healthcheck_address)
        ).fetchone()

        conn.execute("""INSERT INTO registry
            (content_address, healthcheck_address, registered_at, version, key_hash, last_contact)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_address, healthcheck_address) DO UPDATE SET
                registered_at = excluded.registered_at,
                version = excluded.version,
                key_hash = excluded.key_hash,
                fail_count = 0,
                status = 'healthy',
                fast_poll_remaining = 0,
                last_contact = excluded.last_contact""",
            (content_address, healthcheck_address, now, version, key_hash, now))
        conn.commit()

        # If cellar was redirecting this address, release immediately so the
        # worker's descriptor can take over in the Tor DHT.
        released = immediate_release(content_address, conn)
        conn.close()

        self._send_json(200, {
            "registered": True,
            "content_address": content_address,
            "released": released,
            "message": "Registration updated" if existing else "Registration created",
        })

    # -- POST /unregister ---------------------------------------------------

    def _handle_unregister(self):
        data = self._read_json()
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        content_address = data.get("content_address", "")
        if not content_address:
            self._send_json(400, {"error": "Missing required field: content_address"})
            return
        if not ONION_RE.match(content_address):
            self._send_json(400, {"error": "Invalid content_address format"})
            return

        conn = db_connect()
        db_ensure_schema(conn)
        entry = conn.execute(
            "SELECT * FROM registry WHERE content_address = ? LIMIT 1",
            (content_address,)
        ).fetchone()

        if not entry:
            conn.close()
            self._send_json(404, {"error": "Entry not found"})
            return

        # Auth check
        if not is_local_request(self):
            proof = data.get("proof", "")
            stored_hash = entry["key_hash"] or ""
            if not stored_hash:
                conn.close()
                self._send_json(403, {
                    "error": "No key_hash on file — re-register first to enable remote unregister"
                })
                return
            if not verify_proof(stored_hash, proof):
                conn.close()
                self._send_json(403, {"error": "Invalid proof"})
                return

        takeover_was_active = bool(entry["takeover_active"])

        # Remove key files
        keys_dir = os.path.join(CELLAR_KEYS_DIR, content_address)
        if os.path.isdir(keys_dir):
            for f in os.listdir(keys_dir):
                os.unlink(os.path.join(keys_dir, f))
            os.rmdir(keys_dir)

        # Delete all registry entries for this content_address
        conn.execute("DELETE FROM registry WHERE content_address = ?", (content_address,))
        conn.commit()
        conn.close()

        self._send_json(200, {
            "unregistered": True,
            "content_address": content_address,
            "takeover_was_active": takeover_was_active,
        })

    # -- POST /online -------------------------------------------------------

    def _handle_online(self):
        data = self._read_json()
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        content_address = data.get("content_address", "")
        healthcheck_address = data.get("healthcheck_address", "")

        if not content_address:
            self._send_json(400, {"error": "Missing required field: content_address"})
            return
        if not ONION_RE.match(content_address):
            self._send_json(400, {"error": "Invalid content_address format"})
            return
        if healthcheck_address and not ONION_RE.match(healthcheck_address):
            self._send_json(400, {"error": "Invalid healthcheck_address format"})
            return

        conn = db_connect()
        db_ensure_schema(conn)

        # healthcheck_address is optional — if provided, match specific row;
        # otherwise match all rows for this content_address.
        if healthcheck_address:
            entry = conn.execute(
                "SELECT * FROM registry WHERE content_address = ? AND healthcheck_address = ?",
                (content_address, healthcheck_address)
            ).fetchone()
        else:
            entry = conn.execute(
                "SELECT * FROM registry WHERE content_address = ? LIMIT 1",
                (content_address,)
            ).fetchone()

        if not entry:
            conn.close()
            self._send_json(404, {"error": "Entry not found"})
            return

        # Auth check
        if not is_local_request(self):
            proof = data.get("proof", "")
            stored_hash = entry["key_hash"] or ""
            if not stored_hash:
                conn.close()
                self._send_json(403, {"error": "No key_hash on file — re-register first"})
                return
            if not verify_proof(stored_hash, proof):
                conn.close()
                self._send_json(403, {"error": "Invalid proof"})
                return

        takeover_was_active = bool(entry["takeover_active"])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if healthcheck_address:
            conn.execute("""UPDATE registry SET
                status = 'healthy', fail_count = 0, fast_poll_remaining = 20, last_contact = ?
                WHERE content_address = ? AND healthcheck_address = ?""",
                (now, content_address, healthcheck_address))
        else:
            conn.execute("""UPDATE registry SET
                status = 'healthy', fail_count = 0, fast_poll_remaining = 20, last_contact = ?
                WHERE content_address = ?""",
                (now, content_address))
        conn.commit()

        # If cellar was redirecting this address, release immediately so the
        # worker's descriptor can take over in the Tor DHT.
        released = immediate_release(content_address, conn)
        conn.close()

        self._send_json(200, {
            "online": True,
            "content_address": content_address,
            "takeover_was_active": takeover_was_active,
            "released": released,
        })

    # -- POST /offline ------------------------------------------------------

    def _handle_offline(self):
        data = self._read_json()
        if not data:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        content_address = data.get("content_address", "")
        healthcheck_address = data.get("healthcheck_address", "")

        if not content_address:
            self._send_json(400, {"error": "Missing required field: content_address"})
            return
        if not ONION_RE.match(content_address):
            self._send_json(400, {"error": "Invalid content_address format"})
            return
        if healthcheck_address and not ONION_RE.match(healthcheck_address):
            self._send_json(400, {"error": "Invalid healthcheck_address format"})
            return

        conn = db_connect()
        db_ensure_schema(conn)

        if healthcheck_address:
            entry = conn.execute(
                "SELECT * FROM registry WHERE content_address = ? AND healthcheck_address = ?",
                (content_address, healthcheck_address)
            ).fetchone()
        else:
            entry = conn.execute(
                "SELECT * FROM registry WHERE content_address = ? LIMIT 1",
                (content_address,)
            ).fetchone()

        if not entry:
            conn.close()
            self._send_json(404, {"error": "Entry not found"})
            return

        # Auth check
        if not is_local_request(self):
            proof = data.get("proof", "")
            stored_hash = entry["key_hash"] or ""
            if not stored_hash:
                conn.close()
                self._send_json(403, {"error": "No key_hash on file — re-register first"})
                return
            if not verify_proof(stored_hash, proof):
                conn.close()
                self._send_json(403, {"error": "Invalid proof"})
                return

        takeover_was_active = bool(entry["takeover_active"])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # fail_count=10 matches FAIL_THRESHOLD in cellar-poller.py
        if healthcheck_address:
            conn.execute("""UPDATE registry SET
                status = 'failing', fail_count = 10, fast_poll_remaining = 20, last_contact = ?
                WHERE content_address = ? AND healthcheck_address = ?""",
                (now, content_address, healthcheck_address))
        else:
            conn.execute("""UPDATE registry SET
                status = 'failing', fail_count = 10, fast_poll_remaining = 20, last_contact = ?
                WHERE content_address = ?""",
                (now, content_address))
        conn.commit()
        conn.close()

        self._send_json(200, {
            "offline": True,
            "content_address": content_address,
            "takeover_active": takeover_was_active,
        })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Ensure data directories exist
    os.makedirs(CELLAR_DATA_DIR, exist_ok=True)
    os.makedirs(CELLAR_KEYS_DIR, exist_ok=True)

    # Initialize DB schema
    conn = db_connect()
    db_ensure_schema(conn)
    conn.close()

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), CellarHandler)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] cellar-server: listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
