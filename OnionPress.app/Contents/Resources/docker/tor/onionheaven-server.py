#!/usr/bin/env python3
"""
OnionHeaven Registration API Server

Lightweight HTTP server (Python stdlib only) that handles onionheaven registration,
unregistration, and lifecycle notifications. Runs inside the onionheaven
container on port 8083, exposed through the main tor container's onion service.

Endpoints:
  POST /online       — Heartbeat / register (upserts registry entry, optionally stores arti key)
  POST /register     — Alias for /online (backwards compatibility)
  POST /unregister   — Mark a registration as unregistered (soft delete)
  POST /offline      — Notify OnionHeaven that instance is going offline
  GET  /status       — Public status summary (no auth)
  GET  /status/<addr> — Per-address detail (looks up by content or healthcheck address)
"""

ONIONHEAVEN_SERVER_VERSION = "2.4.33"

import base64
import hashlib
import json
import os
import re
import struct
import sys
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

from onion_auth import verify_payload
from onionheaven_common import (
    db_connect, db_commit_with_retry, db_ensure_schema, log,
    takeover_function, release_function, flush_sighup_tor,
    KEYS_DIR, PROPAGATION_DELAY, ONIONHEAVEN_DATA_DIR,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8083

ONION_RE = re.compile(r"^[a-z2-7]{56}\.onion$")

# Read our own onion address so we can reject self-registration
OWN_ONION_ADDRESS = None
_HOSTNAME_PATH = "/var/lib/tor/hidden_service/wordpress/hostname"


def _get_own_address():
    """Return this server's own .onion address (cached after first read)."""
    global OWN_ONION_ADDRESS
    if OWN_ONION_ADDRESS is None:
        try:
            with open(_HOSTNAME_PATH) as f:
                addr = f.read().strip()
            if ONION_RE.match(addr):
                OWN_ONION_ADDRESS = addr
                log(f"Own onion address: {addr}")
        except FileNotFoundError:
            pass  # hostname file not written yet, Arti still starting
    return OWN_ONION_ADDRESS


# ---------------------------------------------------------------------------
# OpenSSH PEM key builder (reimplemented from key_manager.py)
# ---------------------------------------------------------------------------

OPENSSH_MAGIC = b"openssh-key-v1\x00"
ARTI_KEY_TYPE = b"ed25519-expanded@spec.torproject.org"


def validate_arti_pem(pem_bytes):
    """Validate that an Arti PEM key is structurally sound.

    Checks for:
    - Proper PEM header/footer
    - No NUL bytes in the PEM envelope (the error Arti reports)
    - Base64 payload decodes successfully
    - OpenSSH magic header present in decoded data
    - Minimum size for ed25519-expanded key (64 bytes private + 32 bytes public)

    Returns True if valid, False if corrupted.
    """
    try:
        text = pem_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False

    lines = text.strip().splitlines()
    if len(lines) < 3:
        return False
    if not lines[0].startswith("-----BEGIN OPENSSH PRIVATE KEY-----"):
        return False
    if not lines[-1].startswith("-----END OPENSSH PRIVATE KEY-----"):
        return False

    # Extract base64 payload between header and footer
    b64_payload = "".join(lines[1:-1])

    # Check for NUL bytes in the PEM text (the specific Arti error)
    if "\x00" in b64_payload:
        return False

    # Decode and verify OpenSSH structure
    try:
        decoded = base64.b64decode(b64_payload)
    except Exception:
        return False

    if not decoded.startswith(OPENSSH_MAGIC):
        return False

    # Minimum size: magic(15) + ciphername(8) + kdfname(8) + kdfoptions(4)
    # + nkeys(4) + pubkey(~50) + privkey(~120) = ~200+ bytes
    if len(decoded) < 100:
        return False

    return True


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
    if os.environ.get("ONIONHEAVEN_ENFORCE_AUTH") == "1":
        return False
    addr = handler.client_address[0]
    return (
        addr == "127.0.0.1"
        or addr == "::1"
        or addr.startswith("172.")
        or addr.startswith("10.")
    )


def _verify_signature(handler, data, endpoint):
    """Verify ed25519 signature on a request.

    Returns (ok, error_message). Skips verification for local requests.
    """
    if is_local_request(handler):
        return True, ""

    content_address = data.get("content_address", "")
    healthcheck_address = data.get("healthcheck_address", "")
    timestamp = data.get("timestamp", "")
    signature = data.get("signature", "")

    return verify_payload(
        content_address, endpoint, healthcheck_address,
        timestamp, signature
    )


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class OnionHeavenHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        """Override to add timestamp prefix (local time to match host logs)."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{ts}] onionheaven-server: {format % args}\n")
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

    # -- GET dispatch -------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/status":
            self._handle_status()
        elif path.startswith("/status/"):
            addr = path[len("/status/"):]
            self._handle_status_detail(addr)
        else:
            self._send_json(404, {"error": "Not found"})

    # -- GET /status --------------------------------------------------------

    def _handle_status(self):
        try:
            conn = db_connect()
            db_ensure_schema(conn)
            total = conn.execute("SELECT COUNT(*) FROM registry").fetchone()[0]
            online = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE status='online'"
            ).fetchone()[0]
            taken_over = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE status='taken-over'"
            ).fetchone()[0]
            unregistered = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE unregistered_at IS NOT NULL"
            ).fetchone()[0]
            # Entries with a recent heartbeat
            heartbeat_healthy = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE status='online' AND last_healthy IS NOT NULL"
            ).fetchone()[0]
            # Entries where WordPress reported unhealthy in last heartbeat
            wp_unhealthy = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE status='online' AND wordpress_healthy = 0"
            ).fetchone()[0]
            # OnionHeaven peer count (protected from takeover)
            onionheaven_peers = conn.execute(
                "SELECT COUNT(*) FROM registry WHERE is_onionheaven = 1"
            ).fetchone()[0]
            # Farm container counts
            try:
                takeover_containers = conn.execute(
                    "SELECT COUNT(*) FROM takeover_containers WHERE status='active'"
                ).fetchone()[0]
            except Exception:
                takeover_containers = 0
            conn.close()
            self._send_json(200, {
                "version": ONIONHEAVEN_SERVER_VERSION,
                "total": total,
                "online": online,
                "taken_over": taken_over,
                "unregistered": unregistered,
                "heartbeat_healthy": heartbeat_healthy,
                "wordpress_unhealthy": wp_unhealthy,
                "onionheaven_peers": onionheaven_peers,
                "takeover_containers": takeover_containers,
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # -- GET /status/<address> ------------------------------------------------

    def _handle_status_detail(self, address):
        if not ONION_RE.match(address):
            self._send_json(400, {"error": "Invalid .onion address format"})
            return

        try:
            conn = db_connect()
            db_ensure_schema(conn)

            # Try content_address first, then healthcheck_address
            rows = conn.execute(
                "SELECT * FROM registry WHERE content_address = ? ORDER BY registered_at",
                (address,)
            ).fetchall()
            lookup_type = "content_address"

            if not rows:
                rows = conn.execute(
                    "SELECT * FROM registry WHERE healthcheck_address = ? ORDER BY registered_at",
                    (address,)
                ).fetchall()
                lookup_type = "healthcheck_address"

            conn.close()

            if not rows:
                self._send_json(404, {"error": "No entries found for this address (checked both content_address and healthcheck_address)"})
                return

            now = datetime.now(timezone.utc)
            entries = []
            for row in rows:
                entry = {
                    "content_address": row["content_address"],
                    "healthcheck_address": row["healthcheck_address"],
                    "status": row["status"],
                    "registered_at": row["registered_at"],
                    "unregistered_at": row["unregistered_at"],
                    "unregistered_reason": row["unregistered_reason"],
                    "version": row["version"],
                    "last_checked": row["last_checked"],
                    "last_healthy": row["last_healthy"],
                    "last_released": row["last_released"],
                    "last_taken_over": row["last_taken_over"],
                    "last_redirect": row["last_redirect"],
                    "wordpress_healthy": row["wordpress_healthy"],
                    "audit_result": row["audit_result"],
                }

                # Add computed debugging fields
                entry["seconds_since_last_checked"] = self._seconds_since(row["last_checked"], now)
                entry["seconds_since_last_healthy"] = self._seconds_since(row["last_healthy"], now)
                entry["seconds_since_last_taken_over"] = self._seconds_since(row["last_taken_over"], now)
                entry["seconds_since_last_released"] = self._seconds_since(row["last_released"], now)

                # Would the heartbeat monitor take over right now?
                # Conditions: status == 'online', last_healthy is stale (> PROPAGATION_DELAY)
                stale = True
                if row["last_healthy"]:
                    age = self._seconds_since(row["last_healthy"], now)
                    if age is not None:
                        stale = age > PROPAGATION_DELAY
                entry["last_healthy_stale"] = stale if row["status"] == "online" else None
                entry["propagation_delay_seconds"] = PROPAGATION_DELAY

                entries.append(entry)

            self._send_json(200, {
                "lookup_type": lookup_type,
                "address": address,
                "entries": entries,
                "count": len(entries),
                "server_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    @staticmethod
    def _seconds_since(ts_str, now):
        """Return seconds elapsed since a timestamp string, or None."""
        if not ts_str:
            return None
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return round((now - ts).total_seconds())
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _ts_gte(a, b):
        """Return True if timestamp a >= b, None if either is missing."""
        if not a or not b:
            return None
        try:
            ta = datetime.fromisoformat(a.replace("Z", "+00:00"))
            tb = datetime.fromisoformat(b.replace("Z", "+00:00"))
            return ta >= tb
        except (ValueError, TypeError):
            return None

    # -- POST dispatch ------------------------------------------------------

    def do_POST(self):
        path = self.path.split("?")[0]
        handlers = {
            "/register": self._handle_online,  # alias — /register and /online are the same
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

        # Optional: target a specific healthcheck row
        healthcheck_address = data.get("healthcheck_address", "")
        if healthcheck_address and not ONION_RE.match(healthcheck_address):
            self._send_json(400, {"error": "Invalid healthcheck_address format"})
            return

        # Verify ed25519 signature
        ok, err = _verify_signature(self, data, "unregister")
        if not ok:
            self._send_json(403, {"error": err})
            return

        conn = db_connect()
        db_ensure_schema(conn)

        # Find entry
        entry = conn.execute(
            "SELECT * FROM registry WHERE content_address = ? LIMIT 1",
            (content_address,)
        ).fetchone()

        if not entry:
            conn.close()
            self._send_json(404, {"error": "Entry not found"})
            return

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Stress test entries (version='stress-test') get hard-deleted to avoid
        # accumulating junk rows across repeated test runs.
        is_stress = (entry["version"] or "").startswith("stress-test")

        if is_stress:
            if healthcheck_address:
                conn.execute(
                    "DELETE FROM registry "
                    "WHERE content_address = ? AND healthcheck_address = ?",
                    (content_address, healthcheck_address)
                )
            else:
                conn.execute(
                    "DELETE FROM registry WHERE content_address = ?",
                    (content_address,)
                )
        else:
            # Soft delete — preserve rows and keys for real registrations
            if healthcheck_address:
                conn.execute(
                    "UPDATE registry SET status = 'taken-over', unregistered_at = ?, "
                    "unregistered_reason = 'user_request' "
                    "WHERE content_address = ? AND healthcheck_address = ?",
                    (now, content_address, healthcheck_address)
                )
            else:
                conn.execute(
                    "UPDATE registry SET status = 'taken-over', unregistered_at = ?, "
                    "unregistered_reason = 'user_request' "
                    "WHERE content_address = ?",
                    (now, content_address)
                )
        db_commit_with_retry(conn)

        # Check if no healthy rows remain for this content-address — trigger takeover
        healthy_remaining = conn.execute(
            "SELECT COUNT(*) FROM registry "
            "WHERE content_address = ? AND status = 'online' AND unregistered_at IS NULL",
            (content_address,)
        ).fetchone()[0]

        if healthy_remaining == 0:
            # Find a row to use as the target for takeover_function
            target_row = conn.execute(
                "SELECT healthcheck_address FROM registry WHERE content_address = ? LIMIT 1",
                (content_address,)
            ).fetchone()
            if target_row:
                takeover_function(conn, content_address, target_row["healthcheck_address"], force=True)

        conn.close()

        self._send_json(200, {
            "unregistered": True,
            "content_address": content_address,
            "hard_deleted": is_stress,
        })

    # -- POST /online (also handles /register) --------------------------------

    def _handle_online(self):
        """Unified handler for /online and /register.

        Always upserts the registry entry. If arti_key_pem is provided,
        validates and stores it (needed for takeover). Heartbeats can omit
        the key — only the first call needs it.
        """
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

        # Reject self-registration — an OnionHeaven server must never take over its own address
        own_addr = _get_own_address()
        if own_addr and content_address == own_addr:
            log(f"ERROR: Rejected self-registration — {content_address} is this server's own address")
            self._send_json(403, {"error": "Cannot register with yourself"})
            return

        # Verify ed25519 signature — accept both "online" and "register" actions
        # for backwards compatibility with older clients
        endpoint = self.path.split("?")[0].lstrip("/")
        ok, err = _verify_signature(self, data, endpoint)
        if not ok:
            self._send_json(403, {"error": err})
            return

        # If arti_key_pem is provided, validate and store it
        arti_key_stored = False
        if data.get("arti_key_pem"):
            try:
                arti_pem = base64.b64decode(data["arti_key_pem"])
            except Exception:
                self._send_json(400, {"error": "Invalid arti_key_pem base64"})
                return
            if not arti_pem.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
                self._send_json(400, {"error": "Invalid arti_key_pem format"})
                return
            if not validate_arti_pem(arti_pem):
                self._send_json(400, {"error": "Corrupted arti_key_pem: key data failed integrity check"})
                return

            # Store plaintext PEM key
            keys_dir = os.path.join(KEYS_DIR, content_address)
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

            arti_key_stored = True

        conn = db_connect()
        db_ensure_schema(conn)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Accept optional fields
        wordpress_healthy = data.get("wordpress_healthy")
        is_onionheaven = data.get("is_onionheaven")
        version = data.get("version", "unknown")
        is_oh = 1 if is_onionheaven else 0

        created = False

        if healthcheck_address:
            # Check if entry exists (for logging/response)
            existing = conn.execute(
                "SELECT 1 FROM registry WHERE content_address = ? AND healthcheck_address = ?",
                (content_address, healthcheck_address)
            ).fetchone()

            # Upsert: create if new, update if exists
            wp_healthy_val = (1 if wordpress_healthy else 0) if wordpress_healthy is not None else None
            conn.execute("""INSERT INTO registry
                (content_address, healthcheck_address, registered_at, version,
                 last_healthy, status, is_onionheaven, wordpress_healthy, wordpress_checked_at)
                VALUES (?, ?, ?, ?, ?, 'online', ?, ?, ?)
                ON CONFLICT(content_address, healthcheck_address) DO UPDATE SET
                    last_healthy = excluded.last_healthy,
                    status = 'online',
                    version = CASE WHEN excluded.version != 'unknown' THEN excluded.version ELSE registry.version END,
                    is_onionheaven = excluded.is_onionheaven,
                    wordpress_healthy = COALESCE(excluded.wordpress_healthy, registry.wordpress_healthy),
                    wordpress_checked_at = COALESCE(excluded.wordpress_checked_at, registry.wordpress_checked_at),
                    unregistered_at = NULL,
                    unregistered_reason = NULL,
                    audit_result = NULL,
                    audit_at = NULL""",
                (content_address, healthcheck_address, now, version, now, is_oh,
                 wp_healthy_val, now if wp_healthy_val is not None else None))
            db_commit_with_retry(conn)
            release_function(conn, content_address, healthcheck_address, force=True)
            if not existing:
                created = True
                log(f"New registry entry for {content_address} / {healthcheck_address}")
        else:
            # No healthcheck_address — update all rows for this content_address
            existing = conn.execute(
                "SELECT healthcheck_address FROM registry WHERE content_address = ?",
                (content_address,)
            ).fetchall()
            if not existing:
                conn.close()
                self._send_json(400, {"error": "healthcheck_address required for new entries"})
                return
            conn.execute(
                "UPDATE registry SET last_healthy = ?, status = 'online', "
                "is_onionheaven = ?, "
                "unregistered_at = NULL, unregistered_reason = NULL, "
                "audit_result = NULL, audit_at = NULL "
                "WHERE content_address = ?",
                (now, is_oh, content_address)
            )
            db_commit_with_retry(conn)
            for row in existing:
                release_function(conn, content_address, row["healthcheck_address"], force=True)
            flush_sighup_tor()

        conn.close()

        # Write activation flag — signals the host to start the heartbeat
        # monitor + takeover Arti container.  Written on every registration
        # (not just the first) so the host watcher can restart the container
        # if it was stopped externally.
        activate_path = os.path.join(ONIONHEAVEN_DATA_DIR, "activate")
        try:
            with open(activate_path, "w") as f:
                f.write(now + "\n")
        except OSError as e:
            log(f"WARNING: could not write activation flag: {e}")

        self._send_json(200, {
            "online": True,
            "registered": True,  # backwards compat for old clients checking this
            "content_address": content_address,
            "created": created,
            "arti_key_stored": arti_key_stored,
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

        # Verify ed25519 signature
        ok, err = _verify_signature(self, data, "offline")
        if not ok:
            self._send_json(403, {"error": err})
            return

        conn = db_connect()
        db_ensure_schema(conn)

        # Takeover via the shared function (force=True since we know it's offline)
        if healthcheck_address:
            takeover_function(conn, content_address, healthcheck_address, force=True)
        else:
            rows = conn.execute(
                "SELECT healthcheck_address FROM registry WHERE content_address = ?",
                (content_address,)
            ).fetchall()
            for row in rows:
                takeover_function(conn, content_address, row["healthcheck_address"], force=True)
        flush_sighup_tor()

        conn.close()

        self._send_json(200, {
            "offline": True,
            "content_address": content_address,
        })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Ensure data directories exist
    os.makedirs(KEYS_DIR, exist_ok=True)

    # Initialize DB schema
    conn = db_connect()
    db_ensure_schema(conn)
    conn.close()

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), OnionHeavenHandler)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] onionheaven-server: listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
