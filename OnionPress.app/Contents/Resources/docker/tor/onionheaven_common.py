"""
OnionHeaven shared module — constants, DB schema, takeover/release functions.

Imported by both onionheaven-server.py and onionheaven-heartbeat.py to ensure
consistent schema and decision logic.

Farm mode: when takeover_containers are registered in the DB,
takeover/release operations are delegated to farm containers via DB flags
instead of executing locally. This distributes Arti guard pool usage across
multiple containers to prevent circuit exhaustion under load.
"""

import itertools
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ONIONHEAVEN_DATA_DIR = "/var/lib/onionpress/onionheaven"
DB_PATH = os.path.join(ONIONHEAVEN_DATA_DIR, "registry.db")
KEYS_DIR = os.path.join(ONIONHEAVEN_DATA_DIR, "keys")
TOR_MANAGER = "/onionheaven-tor-manager.sh"

# How long to wait after the last successful healthcheck before considering
# a node stale enough for Arti takeover. Override via env for testing.
PROPAGATION_DELAY = int(os.environ.get("TOR_PROPAGATION_DELAY", "180"))

# How many missed heartbeats before considering takeover.
# With 60s heartbeat interval and 180s PROPAGATION_DELAY, this is ~3 missed beats.
CONSECUTIVE_FAILS_THRESHOLD = int(os.environ.get("ONIONHEAVEN_CONSECUTIVE_FAILS", "3"))

# Extra grace period (seconds) for OnionHeaven peers before takeover.
# Peers run OnionHeaven themselves, so a restart is expected to take longer.
# Default 30 minutes — a peer that's been down 30+ minutes is likely truly dead.
ONIONHEAVEN_PEER_GRACE = int(os.environ.get("ONIONHEAVEN_PEER_GRACE", "1800"))

# Minimum interval between SIGHUPs to Tor (seconds).
# Higher values reduce circuit rebuilds at the cost of slower takeover/release.
SIGHUP_MIN_INTERVAL = int(os.environ.get("ONIONHEAVEN_SIGHUP_INTERVAL", "60"))

# Container identity — set by entrypoint for takeover workers
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    """Log to stderr with timestamp (captured by docker logs).

    Uses local time (not UTC) so timestamps match the host-side stress test
    logs and operator's clock. The container inherits /etc/localtime from
    the Colima VM, which mirrors the Mac's timezone.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] OnionHeaven: {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Rate-limited SIGHUP
# ---------------------------------------------------------------------------

_last_sighup_time = 0.0
_sighup_pending = False


def _is_ctor():
    """Check if we're running C Tor (not Arti). SIGHUP is harmful for C Tor
    with ephemeral ADD_ONION services — it re-reads the torrc and kills them."""
    return os.environ.get("TOR_IMPL", "tor").lower() == "tor"


def sighup_tor():
    """Send SIGHUP to Arti. No-op for C Tor (would kill ephemeral services)."""
    if _is_ctor():
        return
    global _last_sighup_time, _sighup_pending
    import time as _time
    now = _time.monotonic()
    elapsed = now - _last_sighup_time

    if elapsed >= SIGHUP_MIN_INTERVAL:
        _do_sighup()
        _last_sighup_time = now
        _sighup_pending = False
    else:
        _sighup_pending = True


def flush_sighup_tor(force=False):
    """Send a final SIGHUP if pending. No-op for C Tor."""
    if _is_ctor():
        return
    global _sighup_pending, _last_sighup_time
    import time as _time
    if _sighup_pending or force:
        _do_sighup()
        _last_sighup_time = _time.monotonic()
        _sighup_pending = False


def _do_sighup():
    """Send SIGHUP to Tor (Arti or C Tor) via tor-manager, then check for corrupted key errors.

    Sanitizes the config BEFORE sending SIGHUP to prevent stale fragments
    from blocking reload.
    """
    _sanitize_arti_toml()

    try:
        result = subprocess.run(
            [TOR_MANAGER, "sighup"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            log(f"SIGHUP sent to Tor")
        else:
            log(f"SIGHUP failed: {result.stderr.strip()}")
    except Exception as e:
        log(f"SIGHUP error: {e}")

    # Check for corrupted key errors after SIGHUP and auto-clean
    _check_arti_key_errors()


def _sanitize_arti_toml():
    """Remove broken fragments from Arti toml before SIGHUP.

    Previous cleanup bugs left orphaned lines like bare [["80", ...]] which
    TOML parses as invalid table headers, blocking ALL config reloads.
    """
    if os.environ.get("NO_ONION_SERVICE") == "1" or os.environ.get("TAKEOVER_WORKER") == "1":
        toml_path = "/etc/arti/arti-onionheaven.toml"
    else:
        toml_path = "/etc/arti/arti.toml"

    try:
        with open(toml_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    cleaned = []
    changed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Remove bare proxy_ports value lines (orphaned from buggy cleanup)
        # Matches [["80", "127.0.0.1:XXXX"]] but NOT proxy_ports = [["80"...]]
        if stripped.startswith("[["  ) and "127.0.0.1" in stripped and stripped.endswith("]]"):
            log(f"sanitize-toml: removing orphaned line: {stripped}")
            changed = True
            i += 1
            continue

        # Remove orphaned comment lines not followed by a proper section header
        if stripped.startswith("# onionheaven:") and stripped.endswith(".onion"):
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j].strip().startswith('[onion_services."'):
                cleaned.append(line)
                i += 1
                continue
            else:
                log(f"sanitize-toml: removing orphaned comment: {stripped}")
                changed = True
                i += 1
                continue

        cleaned.append(line)
        i += 1

    if changed:
        # Collapse excessive blank lines
        final = []
        prev_blank = False
        for line in cleaned:
            if line.strip() == "":
                if prev_blank:
                    continue
                prev_blank = True
            else:
                prev_blank = False
            final.append(line)
        with open(toml_path, "w") as f:
            f.writelines(final)
        log(f"sanitize-toml: cleaned {toml_path}")


def _check_arti_key_errors():
    """Scan recent Arti log output for corrupted key errors and auto-clean.

    Arti logs errors like:
      Unable to launch onion service onionheaven_XXXX: ... PEM preamble contains invalid data (NUL byte)
      Unable to launch onion service onionheaven_XXXX: ... corrupted data in keystore

    When found, remove the corrupted key from keystore and toml config so it
    doesn't block other services on subsequent SIGHUPs.
    """
    import re
    arti_keystore = "/var/lib/arti/state/keystore/hss"

    # Read recent Arti stderr — check the container's process stderr via /proc
    arti_pid = None
    try:
        result = subprocess.run(
            ["pidof", "arti"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            arti_pid = result.stdout.strip().split()[0]
    except Exception:
        pass
    if not arti_pid:
        try:
            result = subprocess.run(
                ["sh", "-c", "ps aux | grep '[/]usr/local/bin/arti' | awk '{print $2}' | head -1"],
                capture_output=True, text=True, timeout=5
            )
            arti_pid = result.stdout.strip()
        except Exception:
            pass

    if not arti_pid:
        return

    # Read Arti's fd 2 (stderr) isn't possible after the fact, but we can check
    # the keystore directly for corrupted keys
    try:
        if not os.path.isdir(arti_keystore):
            return
        for nickname in os.listdir(arti_keystore):
            if not nickname.startswith("onionheaven_"):
                continue
            key_path = os.path.join(arti_keystore, nickname, "ks_hs_id.ed25519_expanded_private")
            if not os.path.isfile(key_path):
                continue
            # Check for NUL bytes or other corruption
            try:
                with open(key_path, "rb") as f:
                    data = f.read()
                if b"\x00" in data:
                    log(f"CORRUPTED KEY detected in Arti keystore: {nickname} — auto-cleaning")
                    _clean_corrupted_service(nickname)
                elif not data.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
                    log(f"INVALID KEY detected in Arti keystore: {nickname} — auto-cleaning")
                    _clean_corrupted_service(nickname)
                elif not data.rstrip().endswith(b"-----END OPENSSH PRIVATE KEY-----"):
                    log(f"TRUNCATED KEY detected in Arti keystore: {nickname} — auto-cleaning")
                    _clean_corrupted_service(nickname)
            except Exception as e:
                log(f"Error checking key {nickname}: {e}")
    except Exception as e:
        log(f"Error scanning Arti keystore: {e}")


def _clean_corrupted_service(nickname):
    """Remove a corrupted onion service from Arti's keystore and config."""
    import shutil
    arti_keystore = "/var/lib/arti/state/keystore/hss"

    # Detect config file
    if os.environ.get("NO_ONION_SERVICE") == "1" or os.environ.get("TAKEOVER_WORKER") == "1":
        arti_toml = "/etc/arti/arti-onionheaven.toml"
    else:
        arti_toml = "/etc/arti/arti.toml"

    # Remove keystore directory
    ks_dir = os.path.join(arti_keystore, nickname)
    if os.path.isdir(ks_dir):
        shutil.rmtree(ks_dir, ignore_errors=True)
        log(f"  Removed keystore dir: {ks_dir}")

    # Remove from arti.toml config
    try:
        with open(arti_toml, "r") as f:
            lines = f.readlines()
        new_lines = []
        skip = 0
        for line in lines:
            if skip > 0:
                skip -= 1
                continue
            if f'[onion_services."{nickname}"]' in line:
                # Remove this line + next 2 (enabled, proxy_ports)
                skip = 2
                # Also remove preceding comment line if it's the marker
                if new_lines and new_lines[-1].startswith("# onionheaven:"):
                    new_lines.pop()
                # Remove preceding blank line too
                if new_lines and new_lines[-1].strip() == "":
                    new_lines.pop()
                continue
            new_lines.append(line)
        with open(arti_toml, "w") as f:
            f.writelines(new_lines)
        log(f"  Removed config for {nickname} from {arti_toml}")
    except Exception as e:
        log(f"  Warning: could not clean config for {nickname}: {e}")

    # Also clean the source key in OnionHeaven keys dir
    # Extract content_address from nickname: onionheaven_XXXX -> find matching key dir
    addr_prefix = nickname.replace("onionheaven_", "")
    keys_base = "/var/lib/onionpress/onionheaven/keys"
    if os.path.isdir(keys_base):
        for entry in os.listdir(keys_base):
            if entry.startswith(addr_prefix):
                src_key = os.path.join(keys_base, entry, "ks_hs_id.ed25519_expanded_private")
                if os.path.isfile(src_key):
                    try:
                        with open(src_key, "rb") as f:
                            data = f.read()
                        if b"\x00" in data or not data.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
                            os.unlink(src_key)
                            log(f"  Removed corrupted source key for {entry}")
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def db_connect():
    """Open OnionHeaven SQLite database with WAL mode."""
    os.makedirs(ONIONHEAVEN_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def db_commit_with_retry(conn, max_retries=5, base_delay=0.5):
    """Commit with retry on 'database is locked' errors.

    The busy_timeout PRAGMA handles most contention, but under extreme load
    (800+ concurrent heartbeats) the timeout can still be exceeded.
    """
    for attempt in range(max_retries):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                log(f"DB locked on commit, retry {attempt + 1}/{max_retries} in {delay:.1f}s")
                time.sleep(delay)
            else:
                raise


def db_ensure_schema(conn):
    """Create the registry table with the new schema.

    Starting fresh — no migration from the old fail_count/takeover_active schema.
    If the old table exists, drop it and recreate.
    """
    # Check if table exists with old schema
    cols = []
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(registry)").fetchall()]
    except sqlite3.Error:
        pass

    if cols and ("fail_count" in cols or "takeover_active" in cols or "last_contact" in cols
                 or "last_polled" in cols):
        # Old schema detected — drop and recreate
        log("Old schema detected — dropping and recreating registry table")
        conn.execute("DROP TABLE registry")
        cols = []

    if not cols:
        conn.execute("""CREATE TABLE IF NOT EXISTS registry (
            content_address     TEXT NOT NULL,
            healthcheck_address TEXT NOT NULL,
            key_hash            TEXT,
            registered_at       TEXT NOT NULL,
            unregistered_at     TEXT,
            unregistered_reason TEXT,
            version             TEXT DEFAULT 'unknown',
            status              TEXT DEFAULT 'online',
            last_checked        TEXT,
            last_healthy        TEXT,
            last_released       TEXT,
            last_taken_over     TEXT,
            last_redirect       TEXT,
            takeover_container  TEXT,
            takeover_pending    TEXT,
            release_pending     TEXT,
            consecutive_fails   INTEGER DEFAULT 0,
            wordpress_healthy   INTEGER,
            wordpress_checked_at TEXT,
            audit_result        TEXT,
            audit_at            TEXT,
            is_onionheaven      INTEGER DEFAULT 0,
            PRIMARY KEY (content_address, healthcheck_address)
        )""")
        db_commit_with_retry(conn)
    else:
        # Table exists — add any missing columns
        for col in [
            "unregistered_at", "unregistered_reason",
            "last_checked", "last_healthy", "last_released",
            "last_taken_over", "last_redirect",
            "takeover_container", "takeover_pending", "release_pending",
            "wordpress_checked_at", "audit_result", "audit_at",
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE registry ADD COLUMN {col} TEXT")
        if "consecutive_fails" not in cols:
            conn.execute("ALTER TABLE registry ADD COLUMN consecutive_fails INTEGER DEFAULT 0")
        if "wordpress_healthy" not in cols:
            conn.execute("ALTER TABLE registry ADD COLUMN wordpress_healthy INTEGER")
        if "is_onionheaven" not in cols:
            conn.execute("ALTER TABLE registry ADD COLUMN is_onionheaven INTEGER DEFAULT 0")
        db_commit_with_retry(conn)

    # Drop old poll_containers table if it exists (no longer needed — heartbeat-based)
    conn.execute("DROP TABLE IF EXISTS poll_containers")

    # Farm coordination tables (always created, idempotent)
    conn.execute("""CREATE TABLE IF NOT EXISTS takeover_containers (
        container_name  TEXT PRIMARY KEY,
        max_services    INTEGER DEFAULT 10,
        active_services INTEGER DEFAULT 0,
        last_heartbeat  TEXT,
        status          TEXT DEFAULT 'active'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS farm_scale_requests (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        worker_type     TEXT NOT NULL,
        requested_at    TEXT NOT NULL,
        fulfilled_at    TEXT
    )""")
    db_commit_with_retry(conn)


# ---------------------------------------------------------------------------
# Farm mode helpers
# ---------------------------------------------------------------------------

def is_farm_mode(conn):
    """Check if farm mode is active.

    Always True on the OnionHeaven server (ONIONHEAVEN=1 env var).
    On normal OnionPress instances, checks DB for active takeover containers.

    Result is cached per heartbeat pass — call invalidate_farm_cache()
    at the start of each pass to refresh.
    """
    if os.environ.get("ONIONHEAVEN") == "1":
        return True
    # Check cache first
    if _farm_mode_cache is not None:
        return _farm_mode_cache
    # Normal OnionPress: check DB
    _discover_takeover_containers(conn)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM takeover_containers WHERE status = 'active'"
        ).fetchone()[0]
        return count > 0
    except sqlite3.OperationalError:
        return False


# Cache for farm mode discovery — invalidated each heartbeat pass
_farm_mode_cache = None
_farm_containers_cache = None
_farm_cache_time = 0.0
FARM_CACHE_TTL = 10.0  # seconds


def invalidate_farm_cache():
    """Clear farm mode cache. Call at the start of each heartbeat pass."""
    global _farm_mode_cache, _farm_containers_cache, _farm_cache_time
    _farm_mode_cache = None
    _farm_containers_cache = None
    _farm_cache_time = 0.0


def _discover_takeover_containers(conn):
    """Discover running takeover containers and register any missing from DB.

    Results are cached for FARM_CACHE_TTL seconds to avoid repeated DNS
    lookups (10 lookups per call × N calls per pass was a major bottleneck).
    """
    global _farm_containers_cache, _farm_cache_time
    import socket as _sock

    now = time.monotonic()
    if _farm_containers_cache is not None and (now - _farm_cache_time) < FARM_CACHE_TTL:
        return

    for idx in range(10):  # check up to 10 takeover containers
        name = f"onionheaven-takeover-{idx}"
        try:
            _sock.getaddrinfo(name, 9050, proto=_sock.IPPROTO_TCP)
        except _sock.gaierror:
            continue
        try:
            existing = conn.execute(
                "SELECT 1 FROM takeover_containers WHERE container_name = ?",
                (name,)
            ).fetchone()
            if not existing:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    "INSERT INTO takeover_containers "
                    "(container_name, max_services, active_services, last_heartbeat, status) "
                    "VALUES (?, 10, 0, ?, 'active')",
                    (name, ts)
                )
                db_commit_with_retry(conn)
                log(f"Discovered running takeover container not in DB: {name}")
        except Exception as e:
            log(f"Warning: takeover container discovery failed for {name}: {e}")

    _farm_cache_time = now
    # Cache the container list
    try:
        rows = conn.execute(
            "SELECT container_name FROM takeover_containers "
            "WHERE status = 'active' ORDER BY container_name"
        ).fetchall()
        _farm_containers_cache = [row["container_name"] for row in rows]
    except sqlite3.OperationalError:
        _farm_containers_cache = []


def get_takeover_containers(conn):
    """Get list of active takeover container names (sorted for consistency)."""
    _discover_takeover_containers(conn)
    return _farm_containers_cache if _farm_containers_cache else []


# Round-robin state for takeover assignment within a heartbeat pass
_takeover_rr_cycle = None
_takeover_rr_containers = None


def assign_takeover_container(conn):
    """Assign to a takeover container that has capacity.

    Uses round-robin but skips containers that are at max_services.
    Returns None if all containers are full (caller should set takeover_pending
    and wait for scale-up).
    """
    global _takeover_rr_cycle, _takeover_rr_containers

    containers = get_takeover_containers(conn)
    if not containers:
        return None

    # Reset cycle if container list changed (scale-up, container died, etc.)
    if containers != _takeover_rr_containers:
        _takeover_rr_containers = containers
        _takeover_rr_cycle = itertools.cycle(containers)

    # Try each container once — skip any that are full.
    # Count assigned rows in the DB (not just active_services) to avoid
    # over-assigning during a single heartbeat pass before the worker updates.
    for _ in range(len(containers)):
        candidate = next(_takeover_rr_cycle)
        try:
            row = conn.execute(
                "SELECT max_services FROM takeover_containers "
                "WHERE container_name = ? AND status = 'active'",
                (candidate,)
            ).fetchone()
            if not row:
                continue
            assigned = conn.execute(
                "SELECT COUNT(*) FROM registry "
                "WHERE takeover_container = ? AND status = 'taken-over'",
                (candidate,)
            ).fetchone()[0]
            if assigned < row["max_services"]:
                return candidate
        except sqlite3.OperationalError:
            continue

    return None  # all full


def check_farm_scaling(conn, active_entries):
    """Check if farm needs more takeover workers and write scale requests.

    Called by the heartbeat monitor each cycle. Checks if there are unassigned
    taken-over entries that need a container. Each scale request creates 2 new
    containers (fulfilled by the host-side farm monitor).

    active_entries: number of active registry entries this cycle.
    """
    try:
        # Count taken-over entries that have no container assigned
        unassigned = conn.execute(
            "SELECT COUNT(*) FROM registry "
            "WHERE status = 'taken-over' AND takeover_container IS NULL AND unregistered_at IS NULL"
        ).fetchone()[0]

        if unassigned == 0:
            return

        # Check if there are already unfulfilled scale requests
        pending_requests = conn.execute(
            "SELECT COUNT(*) FROM farm_scale_requests "
            "WHERE worker_type = 'takeover' AND fulfilled_at IS NULL"
        ).fetchone()[0]

        if pending_requests == 0:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "INSERT INTO farm_scale_requests (worker_type, requested_at) VALUES ('takeover', ?)",
                (now,)
            )
            db_commit_with_retry(conn)
            log(f"Farm scale-up requested: {unassigned} unassigned taken-over entries need containers")
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------------------
# Takeover function
# ---------------------------------------------------------------------------

def takeover_function(conn, content_address, healthcheck_address, force=False):
    """Handle takeover for a single registry row.

    In farm mode: marks DB flags for a takeover worker to pick up.
    In legacy mode: calls tor-manager locally.

    1. Mark the row status='taken-over' and record last_taken_over.
    2. Check Arti guards before triggering takeover:
       - No OTHER row for same content_address already has status='taken-over'
       - No other row for content_address has status='online'
       - last_healthy is stale (now - last_healthy > PROPAGATION_DELAY) OR force=True
    3. If farm mode: assign to a takeover container and set takeover_pending.
       Else: call onionheaven-tor-manager.sh takeover locally.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    row = conn.execute(
        "SELECT * FROM registry WHERE content_address = ? AND healthcheck_address = ?",
        (content_address, healthcheck_address)
    ).fetchone()

    if not row:
        log(f"ERROR: takeover_function called for non-existent row: {content_address} / {healthcheck_address}")
        return

    if row["status"] != "online" and not force:
        return

    # Mark this row as taken-over
    conn.execute(
        "UPDATE registry SET status = 'taken-over', last_taken_over = ? "
        "WHERE content_address = ? AND healthcheck_address = ?",
        (now, content_address, healthcheck_address)
    )
    db_commit_with_retry(conn)
    log(f"Marked {healthcheck_address} as taken-over for {content_address}")

    # Arti guards: should we actually start serving the onion service?
    already_serving = conn.execute(
        "SELECT COUNT(*) FROM registry "
        "WHERE content_address = ? AND healthcheck_address != ? AND status = 'taken-over'",
        (content_address, healthcheck_address)
    ).fetchone()[0] > 0

    if already_serving:
        log(f"Skipping Arti takeover for {content_address} — another row already taken-over")
        return

    no_other_online = conn.execute(
        "SELECT COUNT(*) FROM registry "
        "WHERE content_address = ? AND healthcheck_address != ? AND status = 'online'",
        (content_address, healthcheck_address)
    ).fetchone()[0] == 0

    if not no_other_online:
        log(f"Skipping Arti takeover for {content_address} — other row(s) still online")
        return

    # Check propagation delay
    last_healthy_stale = True
    if row["last_healthy"] and not force:
        try:
            lh = datetime.fromisoformat(row["last_healthy"].replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            elapsed = (now_dt - lh).total_seconds()
            last_healthy_stale = elapsed > PROPAGATION_DELAY
        except (ValueError, TypeError):
            last_healthy_stale = True

    if not last_healthy_stale:
        log(f"Skipping Arti takeover for {content_address} — last_healthy not yet stale")
        return

    # All guards pass — route to farm worker
    if is_farm_mode(conn):
        container = assign_takeover_container(conn)
        if container:
            conn.execute(
                "UPDATE registry SET takeover_container = ?, takeover_pending = ? "
                "WHERE content_address = ? AND healthcheck_address = ?",
                (container, now, content_address, healthcheck_address)
            )
            db_commit_with_retry(conn)
            log(f"Queued takeover of {content_address} → farm worker {container}")
            return
        # No containers yet — mark pending so a worker picks it up once spawned
        conn.execute(
            "UPDATE registry SET takeover_pending = ? "
            "WHERE content_address = ? AND healthcheck_address = ?",
            (now, content_address, healthcheck_address)
        )
        db_commit_with_retry(conn)
        log(f"Takeover pending for {content_address} — waiting for farm worker to be spawned")
        return

    _takeover_local(content_address)


def _takeover_local(content_address, no_sighup=False):
    """Execute takeover via local tor-manager.

    C Tor: uses ADD_ONION via control port (no SIGHUP needed).
    Arti: modifies config; if no_sighup=True, caller must call flush_sighup_tor().
    """
    log(f"Taking over {content_address} via tor-manager (local)")
    try:
        result = subprocess.run(
            [TOR_MANAGER, "takeover", "--no-sighup", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Takeover complete for {content_address}")
        else:
            log(f"Takeover failed for {content_address}: {result.stderr.strip()}")
    except Exception as e:
        log(f"Arti takeover error for {content_address}: {e}")

    if not no_sighup:
        sighup_tor()


# ---------------------------------------------------------------------------
# Release function
# ---------------------------------------------------------------------------

def release_function(conn, content_address, healthcheck_address, force=False):
    """Handle release for a single registry row.

    In farm mode: sets release_pending flag for the assigned takeover worker.
    In legacy mode: calls tor-manager locally.

    1. Mark the row status='online' and record last_released.
    2. If farm mode and row has takeover_container: set release_pending.
       Else: call onionheaven-tor-manager.sh release locally.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    row = conn.execute(
        "SELECT * FROM registry WHERE content_address = ? AND healthcheck_address = ?",
        (content_address, healthcheck_address)
    ).fetchone()

    if not row:
        log(f"ERROR: release_function called for non-existent row: {content_address} / {healthcheck_address}")
        return

    if row["status"] != "taken-over" and not force:
        return

    # Mark this row as online
    conn.execute(
        "UPDATE registry SET status = 'online', last_released = ? "
        "WHERE content_address = ? AND healthcheck_address = ?",
        (now, content_address, healthcheck_address)
    )
    db_commit_with_retry(conn)
    log(f"Marked {healthcheck_address} as online for {content_address}")

    # Route to farm worker or execute locally
    takeover_container = row["takeover_container"] if "takeover_container" in row.keys() else None
    if takeover_container and is_farm_mode(conn):
        conn.execute(
            "UPDATE registry SET release_pending = ?, takeover_pending = NULL "
            "WHERE content_address = ? AND healthcheck_address = ?",
            (now, content_address, healthcheck_address)
        )
        db_commit_with_retry(conn)
        log(f"Queued release of {content_address} → farm worker {takeover_container}")
        return

    _release_local(content_address)


def _release_local(content_address, no_sighup=False):
    """Execute release via local tor-manager (legacy/worker mode).

    If no_sighup=True, skips the SIGHUP — caller is responsible for
    calling flush_sighup_tor() after a batch of releases.
    """
    log(f"Releasing {content_address} via tor-manager (local)")
    try:
        result = subprocess.run(
            [TOR_MANAGER, "release", "--no-sighup", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Arti release complete for {content_address}")
        else:
            log(f"Arti release failed for {content_address}: {result.stderr.strip()}")
    except Exception as e:
        log(f"Arti release error for {content_address}: {e}")

    if not no_sighup:
        sighup_tor()
