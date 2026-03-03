#!/usr/bin/env python3
"""
OnionHeaven Takeover Worker — runs inside onionheaven-takeover-N containers.

Watches the registry DB for rows assigned to this container with
takeover_pending or release_pending flags set. Executes the actual
tor-manager takeover/release commands locally (this container has its
own Arti instance with keystore).

Each takeover container has its own Arti guard pool, preventing the
circuit exhaustion cascade that occurs when a single Arti handles
too many onion services.

Startup:
  1. Register self in takeover_containers table
  2. Reconcile stale assignments (release any services from previous run)

Main loop (every 2s):
  - Process takeover_pending rows assigned to this container
  - Process release_pending rows assigned to this container
  - Heartbeat every 30s
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from onionheaven_common import (
    db_connect, db_ensure_schema, log,
    _takeover_local, _release_local, flush_sighup_arti,
    TOR_MANAGER, ONIONHEAVEN_DATA_DIR,
)

CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "unknown")
MAX_SERVICES = int(os.environ.get("MAX_TAKEOVER_SERVICES", "50"))
LOOP_INTERVAL = 2  # seconds between DB checks
HEARTBEAT_INTERVAL = 30  # seconds between heartbeats


def register_self(conn):
    """Register this container in takeover_containers table."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO takeover_containers (container_name, max_services, active_services, last_heartbeat, status) "
        "VALUES (?, ?, 0, ?, 'active') "
        "ON CONFLICT(container_name) DO UPDATE SET "
        "max_services = excluded.max_services, active_services = 0, "
        "last_heartbeat = excluded.last_heartbeat, status = 'active'",
        (CONTAINER_NAME, MAX_SERVICES, now)
    )
    conn.commit()
    log(f"takeover-worker: registered as {CONTAINER_NAME} (max {MAX_SERVICES} services)")


def startup_reconciliation(conn):
    """Release stale assignments from previous container run.

    Container restart wipes Arti config (takeover service entries lost)
    but the DB still has rows assigned to us. Release them so the poller
    re-evaluates from scratch.
    """
    stale = conn.execute(
        "SELECT DISTINCT content_address FROM registry "
        "WHERE takeover_container = ? AND status = 'taken-over'",
        (CONTAINER_NAME,)
    ).fetchall()

    if not stale:
        return

    addrs = [row[0] for row in stale]
    log(f"takeover-worker: reconciling {len(addrs)} stale assignment(s)")
    for addr in addrs:
        # Clean up keystore dirs left from previous run
        subprocess.run([TOR_MANAGER, "release", addr],
                       capture_output=True, text=True, timeout=30)
        log(f"  released stale takeover for {addr}")

    # Clear assignment and pending flags, reset to online so poller re-evaluates
    conn.execute(
        "UPDATE registry SET status = 'online', takeover_container = NULL, "
        "takeover_pending = NULL, release_pending = NULL "
        "WHERE takeover_container = ? AND status = 'taken-over'",
        (CONTAINER_NAME,)
    )
    conn.commit()
    log("takeover-worker: reconciliation complete")


def process_takeovers(conn):
    """Process pending takeover requests assigned to this container."""
    rows = conn.execute(
        "SELECT content_address, healthcheck_address FROM registry "
        "WHERE takeover_container = ? AND takeover_pending IS NOT NULL",
        (CONTAINER_NAME,)
    ).fetchall()

    if not rows:
        return 0

    count = 0
    for row in rows:
        ca = row["content_address"]
        ha = row["healthcheck_address"]
        log(f"takeover-worker: executing takeover for {ca}")
        _takeover_local(ca)

        # Clear pending flag
        conn.execute(
            "UPDATE registry SET takeover_pending = NULL "
            "WHERE content_address = ? AND healthcheck_address = ?",
            (ca, ha)
        )
        conn.commit()
        count += 1

    if count > 0:
        flush_sighup_arti()
        update_active_count(conn)
    return count


def process_releases(conn):
    """Process pending release requests assigned to this container."""
    rows = conn.execute(
        "SELECT content_address, healthcheck_address FROM registry "
        "WHERE takeover_container = ? AND release_pending IS NOT NULL",
        (CONTAINER_NAME,)
    ).fetchall()

    if not rows:
        return 0

    count = 0
    for row in rows:
        ca = row["content_address"]
        ha = row["healthcheck_address"]
        log(f"takeover-worker: executing release for {ca}")
        _release_local(ca)

        # Clear pending and assignment flags
        conn.execute(
            "UPDATE registry SET release_pending = NULL, takeover_container = NULL "
            "WHERE content_address = ? AND healthcheck_address = ?",
            (ca, ha)
        )
        conn.commit()
        count += 1

    if count > 0:
        flush_sighup_arti()
        update_active_count(conn)
    return count


def update_active_count(conn):
    """Update active_services count for this container."""
    count = conn.execute(
        "SELECT COUNT(DISTINCT content_address) FROM registry "
        "WHERE takeover_container = ? AND status = 'taken-over'",
        (CONTAINER_NAME,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE takeover_containers SET active_services = ? WHERE container_name = ?",
        (count, CONTAINER_NAME)
    )
    conn.commit()


def heartbeat(conn):
    """Update heartbeat timestamp."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE takeover_containers SET last_heartbeat = ? WHERE container_name = ?",
        (now, CONTAINER_NAME)
    )
    conn.commit()


def wait_for_db():
    """Wait for the shared DB directory to exist."""
    for _ in range(30):
        if os.path.isdir(ONIONHEAVEN_DATA_DIR):
            return True
        time.sleep(2)
    log("WARNING: data dir not found after 60s, creating it")
    os.makedirs(ONIONHEAVEN_DATA_DIR, exist_ok=True)
    return True


def main():
    log(f"takeover-worker starting: {CONTAINER_NAME}")

    wait_for_db()

    conn = db_connect()
    db_ensure_schema(conn)
    register_self(conn)
    startup_reconciliation(conn)
    conn.close()

    log(f"takeover-worker ready: {CONTAINER_NAME}")

    last_heartbeat = time.monotonic()

    while True:
        try:
            conn = db_connect()

            takeovers = process_takeovers(conn)
            releases = process_releases(conn)

            if takeovers > 0 or releases > 0:
                log(f"takeover-worker: processed {takeovers} takeover(s), {releases} release(s)")

            # Periodic heartbeat
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                heartbeat(conn)
                update_active_count(conn)
                last_heartbeat = now

            conn.close()
            time.sleep(LOOP_INTERVAL)

        except Exception as e:
            log(f"takeover-worker error: {e}")
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(10)


if __name__ == "__main__":
    main()
