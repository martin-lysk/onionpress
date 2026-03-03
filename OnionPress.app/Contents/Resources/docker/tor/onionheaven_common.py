"""
OnionHeaven shared module — constants, DB schema, takeover/release functions.

Imported by both onionheaven-server.py and onionheaven-poller.py to ensure
consistent schema and decision logic.

Farm mode: when takeover_containers/poll_containers are registered in the DB,
takeover/release operations are delegated to farm containers via DB flags
instead of executing locally. This distributes Arti guard pool usage across
multiple containers to prevent circuit exhaustion under load.
"""

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

    if cols and ("fail_count" in cols or "takeover_active" in cols or "last_contact" in cols):
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
            last_polled         TEXT,
            last_healthy        TEXT,
            last_released       TEXT,
            last_taken_over     TEXT,
            last_redirect       TEXT,
            takeover_container  TEXT,
            takeover_pending    TEXT,
            release_pending     TEXT,
            PRIMARY KEY (content_address, healthcheck_address)
        )""")
        conn.commit()
    else:
        # Table exists with new schema — add any missing columns
        for col, default in [
            ("unregistered_at", None),
            ("unregistered_reason", None),
            ("last_polled", None),
            ("last_healthy", None),
            ("last_released", None),
            ("last_taken_over", None),
            ("last_redirect", None),
            ("takeover_container", None),
            ("takeover_pending", None),
            ("release_pending", None),
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE registry ADD COLUMN {col} TEXT")
        conn.commit()

    # Farm coordination tables (always created, idempotent)
    conn.execute("""CREATE TABLE IF NOT EXISTS takeover_containers (
        container_name  TEXT PRIMARY KEY,
        max_services    INTEGER DEFAULT 50,
        active_services INTEGER DEFAULT 0,
        last_heartbeat  TEXT,
        status          TEXT DEFAULT 'active'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS poll_containers (
        container_name  TEXT PRIMARY KEY,
        socks_addr      TEXT NOT NULL,
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
    """Check if any active takeover containers are registered."""
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM takeover_containers WHERE status = 'active'"
        ).fetchone()[0]
        return count > 0
    except sqlite3.OperationalError:
        return False


def assign_takeover_container(conn):
    """Pick the least-loaded active takeover container.

    Returns container_name or None if no active containers.
    """
    row = conn.execute(
        "SELECT container_name FROM takeover_containers "
        "WHERE status = 'active' "
        "ORDER BY active_services ASC LIMIT 1"
    ).fetchone()
    return row["container_name"] if row else None


def get_poll_socks_addrs(conn):
    """Get list of SOCKS proxy addresses from active poll containers.

    Always includes the default SOCKS_ADDR as a fallback.
    Returns list of "host:port" strings.
    """
    default = os.environ.get("ONIONHEAVEN_SOCKS_ADDR", "onionpress-tor-client:9050")
    addrs = [default]
    try:
        rows = conn.execute(
            "SELECT socks_addr FROM poll_containers WHERE status = 'active'"
        ).fetchall()
        for row in rows:
            addr = row["socks_addr"]
            if addr and addr not in addrs:
                addrs.append(addr)
    except sqlite3.OperationalError:
        pass
    return addrs


# Entries per poll worker before requesting scale-up
POLL_SCALE_THRESHOLD = int(os.environ.get("ONIONHEAVEN_POLL_SCALE_THRESHOLD", "50"))
# Max services per takeover worker before requesting scale-up
TAKEOVER_SCALE_THRESHOLD = int(os.environ.get("ONIONHEAVEN_TAKEOVER_SCALE_THRESHOLD", "50"))


def check_farm_scaling(conn, active_entries):
    """Check if farm needs more workers and write scale requests.

    Called by the poller each cycle. Writes unfulfilled requests to
    farm_scale_requests for the host-side monitor to pick up.

    active_entries: number of registry entries being polled this cycle.
    """
    try:
        # --- Poll worker scaling ---
        poll_count = conn.execute(
            "SELECT COUNT(*) FROM poll_containers WHERE status = 'active'"
        ).fetchone()[0]
        # +1 for the default tor-client SOCKS proxy (always present)
        total_poll_capacity = (poll_count + 1) * POLL_SCALE_THRESHOLD
        if active_entries > total_poll_capacity:
            # Check no unfulfilled request already pending
            pending = conn.execute(
                "SELECT COUNT(*) FROM farm_scale_requests "
                "WHERE worker_type = 'poll' AND fulfilled_at IS NULL"
            ).fetchone()[0]
            if pending == 0:
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    "INSERT INTO farm_scale_requests (worker_type, requested_at) VALUES ('poll', ?)",
                    (now,)
                )
                conn.commit()
                log(f"Farm scale-up requested: poll (entries={active_entries}, capacity={total_poll_capacity})")

        # --- Takeover worker scaling ---
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
