#!/usr/bin/env python3
"""
OnionCellar Poller — containerized healthcheck monitor

Runs inside the tor-polling container alongside Arti (SOCKS + keystore).
Monitors registered OnionPress instances, takes over failed addresses,
and releases them when they recover.

All operations are local:
  - SQLite via Python sqlite3 (shared volume)
  - Healthchecks via curl through local Arti SOCKS (127.0.0.1:9050)
  - Takeover/release via /cellar-tor-manager.sh (same container)
  - Cellar lock check via filesystem (shared volume)
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# Paths (inside the container, on shared onionpress-data volume)
CELLAR_DB_PATH = "/var/lib/onionpress/cellar/registry.db"
CELLAR_DATA_DIR = "/var/lib/onionpress/cellar"
TOR_MANAGER = "/cellar-tor-manager.sh"

# SOCKS proxy (Arti running in same container)
SOCKS_ADDR = "127.0.0.1:9050"

# Healthcheck intervals (seconds)
HEALTHY_INTERVAL = 15
FAST_POLL_INTERVAL = 15
LONG_FAIL_INTERVAL = 1800

# Thresholds
FAIL_THRESHOLD = 10
FAST_POLL_COUNT = 20

# Parallel polling
MAX_POLL_WORKERS = 20


def log(msg):
    """Log to stdout (captured by docker logs)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] OnionCellar: {msg}", flush=True)


# ---------------------------------------------------------------------------
# SQLite access
# ---------------------------------------------------------------------------

def db_connect():
    """Open the cellar SQLite database with WAL mode."""
    conn = sqlite3.connect(CELLAR_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def db_ensure_schema(conn):
    """Create registry table if it doesn't exist."""
    conn.execute("""CREATE TABLE IF NOT EXISTS registry (
        content_address     TEXT PRIMARY KEY,
        healthcheck_address TEXT NOT NULL,
        registered_at       TEXT NOT NULL,
        version             TEXT NOT NULL DEFAULT 'unknown',
        status              TEXT NOT NULL DEFAULT 'healthy',
        last_healthcheck    TEXT,
        fail_count          INTEGER NOT NULL DEFAULT 0,
        takeover_active     INTEGER NOT NULL DEFAULT 0,
        fast_poll_remaining INTEGER NOT NULL DEFAULT 0,
        key_hash            TEXT
    )""")
    # Migration: add key_hash column to existing tables
    cols = [row[1] for row in conn.execute("PRAGMA table_info(registry)").fetchall()]
    if "key_hash" not in cols:
        conn.execute("ALTER TABLE registry ADD COLUMN key_hash TEXT")
    conn.commit()


def db_migrate_json(conn):
    """Import entries from registry.json if it exists."""
    json_path = os.path.join(CELLAR_DATA_DIR, "registry.json")
    migrated_path = json_path + ".migrated"

    if not os.path.exists(json_path) or os.path.exists(migrated_path):
        return 0

    try:
        with open(json_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        try:
            os.rename(json_path, migrated_path)
        except OSError:
            pass
        return 0

    if not isinstance(data, list) or not data:
        try:
            os.rename(json_path, migrated_path)
        except OSError:
            pass
        return 0

    count = 0
    for entry in data:
        ca = entry.get("content_address", "")
        ha = entry.get("healthcheck_address", "")
        if not ca or not ha:
            continue
        try:
            conn.execute(
                """INSERT OR IGNORE INTO registry
                   (content_address, healthcheck_address, registered_at, version,
                    status, last_healthcheck, fail_count, takeover_active, fast_poll_remaining)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ca, ha,
                    entry.get("registered_at", datetime.now(timezone.utc).isoformat()),
                    entry.get("version", "unknown"),
                    entry.get("status", "healthy"),
                    entry.get("last_healthcheck"),
                    int(entry.get("fail_count", 0)),
                    1 if entry.get("takeover_active") else 0,
                    int(entry.get("fast_poll_remaining", entry.get("_fast_poll_remaining", 0))),
                )
            )
            count += 1
        except sqlite3.Error:
            pass

    conn.commit()
    try:
        os.rename(json_path, migrated_path)
    except OSError:
        pass

    return count


def db_read_all(conn):
    """Read all registry entries."""
    rows = conn.execute("SELECT * FROM registry ORDER BY registered_at").fetchall()
    return [dict(row) for row in rows]


def db_write_poll_updates(conn, entries):
    """Batch-update poll fields for modified entries."""
    if not entries:
        return
    cursor = conn.cursor()
    cursor.execute("BEGIN")
    for entry in entries:
        cursor.execute(
            """UPDATE registry SET
               status = ?, last_healthcheck = ?, fail_count = ?,
               takeover_active = ?, fast_poll_remaining = ?
               WHERE content_address = ?""",
            (
                entry.get("status", "healthy"),
                entry.get("last_healthcheck"),
                int(entry.get("fail_count", 0)),
                1 if entry.get("takeover_active") else 0,
                int(entry.get("fast_poll_remaining", 0)),
                entry["content_address"],
            )
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Healthcheck via local curl + Arti SOCKS
# ---------------------------------------------------------------------------

def check_healthcheck(healthcheck_address):
    """Check if a healthcheck .onion address is reachable."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--socks5-hostname", SOCKS_ADDR,
             "--max-time", "15",
             f"http://{healthcheck_address}/"],
            capture_output=True, text=True, timeout=25
        )
        return result.returncode == 0
    except Exception:
        return False


def check_content(content_address):
    """Check if a content .onion address is reachable."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--socks5-hostname", SOCKS_ADDR,
             "--max-time", "15",
             f"http://{content_address}/"],
            capture_output=True, text=True, timeout=25
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Takeover / Release via cellar-tor-manager.sh (local)
# ---------------------------------------------------------------------------

def do_takeover(entry):
    """Take over a failed instance's .onion address.
    Returns: 'ok' or 'failed'."""
    content_addr = entry["content_address"]
    log(f"Taking over {content_addr}")

    try:
        result = subprocess.run(
            [TOR_MANAGER, "takeover", content_addr],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Takeover complete for {content_addr}")
            return "ok"
        else:
            log(f"Takeover failed for {content_addr}: {result.stdout.strip()}")
            return "failed"
    except Exception as e:
        log(f"Takeover error for {content_addr}: {e}")
        return "failed"


def do_release(entry):
    """Release a recovered instance's .onion address."""
    content_addr = entry["content_address"]
    log(f"Releasing {content_addr} — original is back online")

    try:
        result = subprocess.run(
            [TOR_MANAGER, "release", content_addr],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Released {content_addr}")
            return True
        else:
            log(f"Release failed for {content_addr}: {result.stdout.strip()}")
            return False
    except Exception as e:
        log(f"Release error for {content_addr}: {e}")
        return False


# ---------------------------------------------------------------------------
# Poll a single entry
# ---------------------------------------------------------------------------

def poll_entry(entry):
    """Poll a single registry entry. Returns (entry, modified, sleep_interval)."""
    content_addr = entry.get("content_address", "")
    hc_addr = entry.get("healthcheck_address", "")
    fail_count = int(entry.get("fail_count", 0))
    takeover_active = bool(entry.get("takeover_active"))
    fast_poll_remaining = int(entry.get("fast_poll_remaining", 0))

    if not content_addr or not hc_addr:
        return entry, False, HEALTHY_INTERVAL

    modified = False

    # Check healthcheck
    hc_ok = check_healthcheck(hc_addr)

    if hc_ok:
        if takeover_active:
            # Instance recovered — release
            if do_release(entry):
                entry["takeover_active"] = False
                entry["status"] = "healthy"
                entry["fail_count"] = 0
                entry["fast_poll_remaining"] = FAST_POLL_COUNT
            else:
                log(f"WARNING — release failed for {content_addr}, keeping takeover_active")
                entry["status"] = "release_failed"
            modified = True
        elif fail_count > 0:
            # Was failing, now recovering
            entry["fail_count"] = 0
            entry["status"] = "healthy"
            entry["fast_poll_remaining"] = FAST_POLL_COUNT
            modified = True
        else:
            entry["status"] = "healthy"
    else:
        # Healthcheck failed
        new_fail_count = fail_count + 1
        entry["fail_count"] = new_fail_count
        entry["status"] = "failing"
        entry["fast_poll_remaining"] = FAST_POLL_COUNT
        modified = True

        if new_fail_count >= FAIL_THRESHOLD and not takeover_active:
            # Double-check: also test the content address
            content_ok = check_content(content_addr)
            if not content_ok:
                result = do_takeover(entry)
                if result == "ok":
                    entry["takeover_active"] = True
                    entry["status"] = "taken_over"
                else:
                    entry["status"] = "takeover_failed"
                modified = True

    # Update timestamp
    entry["last_healthcheck"] = datetime.now(timezone.utc).isoformat()
    modified = True

    # Determine sleep interval
    sleep_interval = HEALTHY_INTERVAL
    if fast_poll_remaining > 0:
        entry["fast_poll_remaining"] = fast_poll_remaining - 1
        sleep_interval = FAST_POLL_INTERVAL
        modified = True
    elif takeover_active:
        sleep_interval = LONG_FAIL_INTERVAL

    return entry, modified, sleep_interval


# ---------------------------------------------------------------------------
# Main poller loop
# ---------------------------------------------------------------------------

def wait_for_socks():
    """Wait for Arti SOCKS proxy to accept connections."""
    import socket
    log("Waiting for Arti SOCKS proxy...")
    for _ in range(60):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(("127.0.0.1", 9050))
            s.close()
            log("Arti SOCKS proxy is ready")
            return True
        except (ConnectionRefusedError, OSError):
            time.sleep(2)
    log("WARNING: Arti SOCKS proxy not ready after 120s")
    return False


def main():
    log("healthcheck poller starting")

    # Wait for Arti SOCKS to be available
    wait_for_socks()

    # Wait for the DB directory to exist (shared volume may take a moment)
    for _ in range(30):
        if os.path.isdir(CELLAR_DATA_DIR):
            break
        time.sleep(2)
    else:
        log(f"WARNING: {CELLAR_DATA_DIR} not found after 60s, creating it")
        os.makedirs(CELLAR_DATA_DIR, exist_ok=True)

    # Initialize DB
    conn = db_connect()
    db_ensure_schema(conn)
    migrated = db_migrate_json(conn)
    if migrated > 0:
        log(f"migrated {migrated} entries from JSON to SQLite")
    conn.close()

    log("healthcheck poller started")

    while True:
        try:
            conn = db_connect()
            registry = db_read_all(conn)

            if not registry:
                log("poll pass complete — 0 entries in 0.0s")
                conn.close()
                time.sleep(HEALTHY_INTERVAL)
                continue

            pass_start = time.monotonic()
            modified_entries = []
            min_sleep = HEALTHY_INTERVAL

            # Immediate release: if entry re-registered while taken over,
            # registration resets status='healthy' but leaves takeover_active=1.
            for entry in registry:
                if entry.get("takeover_active") and entry.get("status") == "healthy":
                    log(f"{entry['content_address']} re-registered — immediate release")
                    if do_release(entry):
                        entry["takeover_active"] = False
                        entry["fail_count"] = 0
                        entry["fast_poll_remaining"] = 0
                    else:
                        log(f"WARNING — immediate release failed for {entry['content_address']}, keeping takeover_active")
                        entry["status"] = "release_failed"
                    entry["last_healthcheck"] = datetime.now(timezone.utc).isoformat()
                    modified_entries.append(entry)

            workers = min(MAX_POLL_WORKERS, len(registry))

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(poll_entry, entry): i
                    for i, entry in enumerate(registry)
                }
                for future in as_completed(futures):
                    try:
                        entry, modified, sleep_interval = future.result()
                        if modified:
                            modified_entries.append(entry)
                        min_sleep = min(min_sleep, sleep_interval)
                    except Exception as e:
                        log(f"entry poll error: {e}")

            elapsed = time.monotonic() - pass_start
            log(f"poll pass complete — {len(registry)} entries in {elapsed:.1f}s")

            if modified_entries:
                db_write_poll_updates(conn, modified_entries)

            conn.close()
            time.sleep(min_sleep)

        except Exception as e:
            log(f"poller error: {e}")
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(60)


if __name__ == "__main__":
    main()
