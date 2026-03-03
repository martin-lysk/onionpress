"""
OnionHeaven shared module — constants, DB schema, takeover/release functions.

Imported by both onionheaven-server.py and onionheaven-poller.py to ensure
consistent schema and decision logic.
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


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    """Log to stderr with timestamp (captured by docker logs)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] OnionHeaven: {msg}\n")
    sys.stderr.flush()


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
            PRIMARY KEY (content_address, healthcheck_address)
        )""")
        conn.commit()
        return

    # Table exists with new schema — add any missing columns
    for col, default in [
        ("unregistered_at", None),
        ("unregistered_reason", None),
        ("last_polled", None),
        ("last_healthy", None),
        ("last_released", None),
        ("last_taken_over", None),
        ("last_redirect", None),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE registry ADD COLUMN {col} TEXT")
    conn.commit()


# ---------------------------------------------------------------------------
# Takeover function
# ---------------------------------------------------------------------------

def takeover_function(conn, content_address, healthcheck_address, force=False):
    """Handle takeover for a single registry row.

    1. Mark the row status='taken-over' and record last_taken_over.
    2. Check Arti guards before calling tor-manager takeover:
       - No OTHER row for same content_address already has status='taken-over'
         (prevents duplicate Arti service entries)
       - No other row for content_address has status='online'
         (don't take over if another instance is still healthy)
       - last_healthy is stale (now - last_healthy > PROPAGATION_DELAY)
         OR force=True
    3. If all guards pass, call onionheaven-tor-manager.sh takeover.
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

    # All guards pass — call tor-manager
    log(f"Taking over {content_address} via Arti")
    try:
        result = subprocess.run(
            [TOR_MANAGER, "takeover", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Arti takeover complete for {content_address}")
        else:
            log(f"Arti takeover failed for {content_address}: {result.stderr.strip()}")
    except Exception as e:
        log(f"Arti takeover error for {content_address}: {e}")


# ---------------------------------------------------------------------------
# Release function
# ---------------------------------------------------------------------------

def release_function(conn, content_address, healthcheck_address, force=False):
    """Handle release for a single registry row.

    1. Mark the row status='online' and record last_released.
    2. Call onionheaven-tor-manager.sh release.
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

    # Call tor-manager release
    log(f"Releasing {content_address} via Arti")
    try:
        result = subprocess.run(
            [TOR_MANAGER, "release", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Arti release complete for {content_address}")
        else:
            log(f"Arti release failed for {content_address}: {result.stderr.strip()}")
    except Exception as e:
        log(f"Arti release error for {content_address}: {e}")
