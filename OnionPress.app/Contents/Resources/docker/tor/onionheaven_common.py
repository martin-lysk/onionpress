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

# Minimum interval between SIGHUPs to Arti (seconds)
SIGHUP_MIN_INTERVAL = int(os.environ.get("ONIONHEAVEN_SIGHUP_INTERVAL", "5"))

# Container identity — set by entrypoint for takeover workers
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    """Log to stderr with timestamp (captured by docker logs)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] OnionHeaven: {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Rate-limited SIGHUP
# ---------------------------------------------------------------------------

_last_sighup_time = 0.0
_sighup_pending = False


def sighup_arti():
    """Send SIGHUP to Arti if at least SIGHUP_MIN_INTERVAL seconds have passed.

    If called too soon after the last SIGHUP, marks it as pending.
    Call flush_sighup_arti() at the end of a batch to ensure the
    final SIGHUP is sent.
    """
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


def flush_sighup_arti():
    """Send a final SIGHUP if one is pending. Call after a batch of changes."""
    global _sighup_pending, _last_sighup_time
    import time as _time
    if _sighup_pending:
        _do_sighup()
        _last_sighup_time = _time.monotonic()
        _sighup_pending = False


def _do_sighup():
    """Send SIGHUP to Arti via tor-manager."""
    try:
        result = subprocess.run(
            [TOR_MANAGER, "sighup"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            log(f"SIGHUP sent to Arti")
        else:
            log(f"SIGHUP failed: {result.stderr.strip()}")
    except Exception as e:
        log(f"SIGHUP error: {e}")


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def db_connect():
    """Open OnionHeaven SQLite database with WAL mode."""
    os.makedirs(ONIONHEAVEN_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


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
            PRIMARY KEY (content_address, healthcheck_address)
        )""")
        conn.commit()
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
        conn.commit()

    # Drop old poll_containers table if it exists (no longer needed — heartbeat-based)
    conn.execute("DROP TABLE IF EXISTS poll_containers")

    # Farm coordination tables (always created, idempotent)
    conn.execute("""CREATE TABLE IF NOT EXISTS takeover_containers (
        container_name  TEXT PRIMARY KEY,
        max_services    INTEGER DEFAULT 50,
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
    conn.commit()


# ---------------------------------------------------------------------------
# Farm mode helpers
# ---------------------------------------------------------------------------

def is_farm_mode(conn):
    """Check if farm mode is active.

    Always True on the OnionHeaven server (ONIONHEAVEN=1 env var).
    On normal OnionPress instances, checks DB for active takeover containers.
    """
    if os.environ.get("ONIONHEAVEN") == "1":
        return True
    # Normal OnionPress: check DB
    _discover_takeover_containers(conn)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM takeover_containers WHERE status = 'active'"
        ).fetchone()[0]
        return count > 0
    except sqlite3.OperationalError:
        return False


def _discover_takeover_containers(conn):
    """Discover running takeover containers and register any missing from DB."""
    import socket as _sock
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
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    "INSERT INTO takeover_containers "
                    "(container_name, max_services, active_services, last_heartbeat, status) "
                    "VALUES (?, 50, 0, ?, 'active')",
                    (name, now)
                )
                conn.commit()
                log(f"Discovered running takeover container not in DB: {name}")
        except Exception as e:
            log(f"Warning: takeover container discovery failed for {name}: {e}")


def get_takeover_containers(conn):
    """Get list of active takeover container names (sorted for consistency)."""
    _discover_takeover_containers(conn)
    try:
        rows = conn.execute(
            "SELECT container_name FROM takeover_containers "
            "WHERE status = 'active' ORDER BY container_name"
        ).fetchall()
        return [row["container_name"] for row in rows]
    except sqlite3.OperationalError:
        return []


# Round-robin state for takeover assignment within a heartbeat pass
_takeover_rr_cycle = None
_takeover_rr_containers = None


def assign_takeover_container(conn):
    """Round-robin assignment across active takeover containers.

    Avoids the stale-DB problem where least-loaded queries return the same
    container for an entire batch because active_services hasn't been updated
    by the worker yet.
    """
    global _takeover_rr_cycle, _takeover_rr_containers

    containers = get_takeover_containers(conn)
    if not containers:
        return None

    # Reset cycle if container list changed (scale-up, container died, etc.)
    if containers != _takeover_rr_containers:
        _takeover_rr_containers = containers
        _takeover_rr_cycle = itertools.cycle(containers)

    return next(_takeover_rr_cycle)


# Max services per takeover worker before requesting scale-up.
# Each Arti instance can handle many onion services — keep this high to avoid
# spawning too many containers (each runs a full Arti at ~60MB RAM).
TAKEOVER_SCALE_THRESHOLD = int(os.environ.get("ONIONHEAVEN_TAKEOVER_SCALE_THRESHOLD", "50"))


def check_farm_scaling(conn, active_entries):
    """Check if farm needs more takeover workers and write scale requests.

    Called by the heartbeat monitor each cycle. Writes unfulfilled requests to
    farm_scale_requests for the host-side monitor to pick up.

    active_entries: number of active registry entries this cycle.
    """
    try:
        takeover_rows = conn.execute(
            "SELECT container_name, max_services, active_services FROM takeover_containers "
            "WHERE status = 'active'"
        ).fetchall()
        if takeover_rows:
            total_active = sum(r["active_services"] for r in takeover_rows)
            total_capacity = len(takeover_rows) * TAKEOVER_SCALE_THRESHOLD
            if total_active >= total_capacity:
                pending = conn.execute(
                    "SELECT COUNT(*) FROM farm_scale_requests "
                    "WHERE worker_type = 'takeover' AND fulfilled_at IS NULL"
                ).fetchone()[0]
                if pending == 0:
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    conn.execute(
                        "INSERT INTO farm_scale_requests (worker_type, requested_at) VALUES ('takeover', ?)",
                        (now,)
                    )
                    conn.commit()
                    log(f"Farm scale-up requested: takeover (active={total_active}, capacity={total_capacity})")
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
    conn.commit()
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

    # All guards pass — route to farm or execute locally
    if is_farm_mode(conn):
        container = assign_takeover_container(conn)
        if container:
            conn.execute(
                "UPDATE registry SET takeover_container = ?, takeover_pending = ? "
                "WHERE content_address = ? AND healthcheck_address = ?",
                (container, now, content_address, healthcheck_address)
            )
            conn.commit()
            log(f"Queued takeover of {content_address} → farm worker {container}")
            return
        log(f"WARNING: farm mode but no active containers — falling back to local takeover")

    _takeover_local(content_address)


def _takeover_local(content_address):
    """Execute takeover via local tor-manager (legacy/worker mode)."""
    log(f"Taking over {content_address} via Arti (local)")
    try:
        result = subprocess.run(
            [TOR_MANAGER, "takeover", "--no-sighup", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Arti takeover complete for {content_address}")
        else:
            log(f"Arti takeover failed for {content_address}: {result.stderr.strip()}")
    except Exception as e:
        log(f"Arti takeover error for {content_address}: {e}")

    sighup_arti()


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
    conn.commit()
    log(f"Marked {healthcheck_address} as online for {content_address}")

    # Route to farm worker or execute locally
    takeover_container = row["takeover_container"] if "takeover_container" in row.keys() else None
    if takeover_container and is_farm_mode(conn):
        conn.execute(
            "UPDATE registry SET release_pending = ?, takeover_pending = NULL "
            "WHERE content_address = ? AND healthcheck_address = ?",
            (now, content_address, healthcheck_address)
        )
        conn.commit()
        log(f"Queued release of {content_address} → farm worker {takeover_container}")
        return

    _release_local(content_address)


def _release_local(content_address):
    """Execute release via local tor-manager (legacy/worker mode)."""
    log(f"Releasing {content_address} via Arti (local)")
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

    sighup_arti()
