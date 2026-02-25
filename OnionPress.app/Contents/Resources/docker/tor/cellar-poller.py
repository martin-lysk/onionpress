#!/usr/bin/env python3
"""
OnionCellar Poller — containerized healthcheck monitor

Runs inside the onioncellar container alongside Arti (SOCKS + keystore),
cellar-server.py (registration API), and cellar-redirect.sh (302 redirects).
Monitors registered OnionPress instances, takes over failed addresses,
and releases them when they recover.

All operations are local:
  - SQLite via Python sqlite3 (shared volume)
  - Healthchecks via curl through local Arti SOCKS (127.0.0.1:9050)
  - Takeover/release via /cellar-tor-manager.sh (same container)

Schema uses composite primary key (content_address, healthcheck_address) to
support multiple instances registering the same .onion address. Takeover
triggers when ALL rows for a content_address are failing; release triggers
when ANY row becomes healthy.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# Paths (inside the container, on shared onionpress-data volume)
CELLAR_DB_PATH = "/var/lib/onionpress/cellar/registry.db"
CELLAR_DATA_DIR = "/var/lib/onionpress/cellar"
TOR_MANAGER = "/cellar-tor-manager.sh"

# SOCKS proxy (Arti running in same container)
SOCKS_ADDR = "127.0.0.1:9050"

# Healthcheck intervals (seconds) — override via env for testing
HEALTHY_INTERVAL = int(os.environ.get("CELLAR_HEALTHY_INTERVAL", "15"))
FAST_POLL_INTERVAL = int(os.environ.get("CELLAR_FAST_POLL_INTERVAL", "15"))
LONG_FAIL_INTERVAL = int(os.environ.get("CELLAR_LONG_FAIL_INTERVAL", "1800"))

# Thresholds — override via env for testing
FAIL_THRESHOLD = int(os.environ.get("CELLAR_FAIL_THRESHOLD", "10"))
FAST_POLL_COUNT = int(os.environ.get("CELLAR_FAST_POLL_COUNT", "20"))

# Parallel polling
MAX_POLL_WORKERS = int(os.environ.get("CELLAR_MAX_POLL_WORKERS", "20"))


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
    """Create registry table if it doesn't exist, and migrate old schema."""
    # Check if table exists and needs migration
    cols = []
    try:
        cols = conn.execute("PRAGMA table_info(registry)").fetchall()
    except sqlite3.Error:
        pass

    col_names = [row[1] for row in cols]
    needs_migration = False

    if col_names:
        # Table exists — check for old schema (single PK, last_healthcheck)
        if "last_healthcheck" in col_names and "last_contact" not in col_names:
            needs_migration = True
        # Check if single PK (old schema)
        pk_cols = [row for row in cols if row[5] > 0]  # pk column index
        if len(pk_cols) == 1:
            needs_migration = True

    if needs_migration:
        conn.execute("""CREATE TABLE IF NOT EXISTS registry_new (
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

        # Copy data — map last_healthcheck → last_contact
        kh_col = ", key_hash" if "key_hash" in col_names else ", NULL"
        conn.execute(f"""INSERT OR IGNORE INTO registry_new
            (content_address, healthcheck_address, registered_at, version,
             status, last_contact, fail_count, takeover_active, fast_poll_remaining, key_hash)
            SELECT content_address, healthcheck_address, registered_at, version,
                   status, last_healthcheck, fail_count, takeover_active, fast_poll_remaining
                   {kh_col}
            FROM registry""")
        conn.execute("DROP TABLE registry")
        conn.execute("ALTER TABLE registry_new RENAME TO registry")
        conn.commit()
        return

    # Fresh install or already migrated
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

    # Add columns that may be missing on older new-schema tables
    if col_names:
        if "key_hash" not in col_names:
            conn.execute("ALTER TABLE registry ADD COLUMN key_hash TEXT")
        if "last_redirect" not in col_names:
            conn.execute("ALTER TABLE registry ADD COLUMN last_redirect TEXT")
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
                    status, last_contact, fail_count, takeover_active, fast_poll_remaining)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ca, ha,
                    entry.get("registered_at", datetime.now(timezone.utc).isoformat()),
                    entry.get("version", "unknown"),
                    entry.get("status", "healthy"),
                    entry.get("last_healthcheck", entry.get("last_contact")),
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
               status = ?, last_contact = ?, fail_count = ?,
               takeover_active = ?, fast_poll_remaining = ?
               WHERE content_address = ? AND healthcheck_address = ?""",
            (
                entry.get("status", "healthy"),
                entry.get("last_contact"),
                int(entry.get("fail_count", 0)),
                1 if entry.get("takeover_active") else 0,
                int(entry.get("fast_poll_remaining", 0)),
                entry["content_address"],
                entry["healthcheck_address"],
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

def do_takeover(content_addr):
    """Take over a failed instance's .onion address.
    Returns: 'ok' or 'failed'."""
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


def do_release(content_addr):
    """Release a recovered instance's .onion address."""
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
    """Poll a single registry entry. Returns (entry, modified, sleep_interval).

    Note: takeover/release decisions are made in the main loop after grouping
    entries by content_address. This function only does the healthcheck and
    updates per-row state (fail_count, status).
    """
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
        if fail_count > 0 or entry.get("status") != "healthy":
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

    # Update timestamp
    entry["last_contact"] = datetime.now(timezone.utc).isoformat()
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
            # Group by content_address — release if ANY row is healthy.
            by_ca = defaultdict(list)
            for entry in registry:
                by_ca[entry["content_address"]].append(entry)

            for ca, rows in by_ca.items():
                any_taken_over = any(r.get("takeover_active") for r in rows)
                any_healthy = any(r.get("status") == "healthy" for r in rows)
                if any_taken_over and any_healthy:
                    log(f"{ca} re-registered — immediate release")
                    if do_release(ca):
                        for r in rows:
                            r["takeover_active"] = False
                            r["fail_count"] = 0
                            r["fast_poll_remaining"] = 0
                            r["last_contact"] = datetime.now(timezone.utc).isoformat()
                            modified_entries.append(r)
                    else:
                        log(f"WARNING — immediate release failed for {ca}, keeping takeover_active")
                        for r in rows:
                            if r.get("status") == "healthy":
                                r["status"] = "release_failed"
                                r["last_contact"] = datetime.now(timezone.utc).isoformat()
                                modified_entries.append(r)

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

            # After polling, make takeover/release decisions grouped by content_address.
            # Re-group because poll_entry updated individual row state.
            by_ca_post = defaultdict(list)
            for entry in registry:
                by_ca_post[entry["content_address"]].append(entry)

            for ca, rows in by_ca_post.items():
                any_taken_over = any(r.get("takeover_active") for r in rows)
                all_failing = all(
                    int(r.get("fail_count", 0)) >= FAIL_THRESHOLD for r in rows
                )
                any_healthy = any(r.get("status") == "healthy" for r in rows)

                if any_taken_over and any_healthy:
                    # At least one instance came back — release
                    log(f"{ca} instance recovered — releasing")
                    if do_release(ca):
                        for r in rows:
                            r["takeover_active"] = False
                            r["fast_poll_remaining"] = FAST_POLL_COUNT
                            modified_entries.append(r)
                    else:
                        log(f"WARNING — release failed for {ca}")
                        for r in rows:
                            if r.get("status") == "healthy":
                                r["status"] = "release_failed"
                                modified_entries.append(r)

                elif all_failing and not any_taken_over:
                    # ALL instances failing — double-check content address, then takeover
                    content_ok = check_content(ca)
                    if not content_ok:
                        result = do_takeover(ca)
                        if result == "ok":
                            for r in rows:
                                r["takeover_active"] = True
                                r["status"] = "taken_over"
                                modified_entries.append(r)
                        else:
                            for r in rows:
                                r["status"] = "takeover_failed"
                                modified_entries.append(r)

            elapsed = time.monotonic() - pass_start
            log(f"poll pass complete — {len(registry)} entries in {elapsed:.1f}s")

            if modified_entries:
                # Deduplicate: keep last update for each (ca, ha) pair
                seen = {}
                for e in modified_entries:
                    key = (e["content_address"], e["healthcheck_address"])
                    seen[key] = e
                db_write_poll_updates(conn, list(seen.values()))

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
