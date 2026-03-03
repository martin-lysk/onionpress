#!/usr/bin/env python3
"""
OnionHeaven Poller — containerized healthcheck monitor

Runs inside the onionheaven container alongside Arti (SOCKS + keystore),
onionheaven-server.py (registration API), and onionheaven-redirect.sh (302 redirects).
Monitors registered OnionPress instances, takes over failed addresses,
and releases them when they recover.

All operations are local:
  - SQLite via Python sqlite3 (shared volume)
  - Healthchecks via curl through local Arti SOCKS (127.0.0.1:9050)
  - Takeover/release via /onionheaven-tor-manager.sh (same container)

New design (v2):
  - Timestamp-based takeover decisions (last_healthy + propagation_delay)
    instead of fail_count + threshold
  - Double-ping: single failed ping doesn't trigger takeover
  - takeover_function/release_function from onionheaven_common.py
  - Sequential takeover/release decisions (no races), parallel healthchecks
"""

import os
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from onionheaven_common import (
    db_connect, db_ensure_schema, log,
    takeover_function, release_function,
    PROPAGATION_DELAY, TOR_MANAGER,
)

# SOCKS proxy (Arti running in same container)
SOCKS_ADDR = "127.0.0.1:9050"

# Polling interval (seconds) — override via env for testing
POLL_INTERVAL = int(os.environ.get("ONIONHEAVEN_POLL_INTERVAL", "15"))

# Parallel polling
MAX_POLL_WORKERS = int(os.environ.get("ONIONHEAVEN_MAX_POLL_WORKERS", "20"))


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
        # Verify we got a real HTTP response, not just a connection.
        # 302 is excluded — could be OnionHeaven's own redirect service.
        http_code = result.stdout.strip()
        return result.returncode == 0 and http_code in ("200", "301")
    except Exception:
        return False


def ping(healthcheck_address):
    """Double-ping: try healthcheck, retry once after 5s on failure.

    Returns True if either attempt succeeds.
    """
    ok = check_healthcheck(healthcheck_address)
    if ok:
        return True
    time.sleep(5)
    ok2 = check_healthcheck(healthcheck_address)
    if ok2:
        log(f"{healthcheck_address} succeeded on 2nd try")
    return ok2


# ---------------------------------------------------------------------------
# Poll a single entry (healthcheck only — no takeover/release decisions)
# ---------------------------------------------------------------------------

def poll_entry(entry):
    """Poll a single registry entry. Returns (entry_dict, ping_result).

    Performs the double-ping healthcheck. Takeover/release decisions
    are made sequentially in the main loop to avoid races.
    """
    hc_addr = entry["healthcheck_address"]
    if not hc_addr:
        return dict(entry), False
    result = ping(hc_addr)
    return dict(entry), result


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


def startup_reconciliation(conn):
    """Reset all taken-over rows to online on startup.

    Container restart wipes arti-onionheaven.toml (takeover service entries lost)
    but the DB still has status='taken-over'. Reset so the normal polling loop
    re-evaluates from scratch — the service may have come back while we were down.
    """
    stale = conn.execute(
        "SELECT DISTINCT content_address FROM registry WHERE status = 'taken-over'"
    ).fetchall()

    if not stale:
        return

    addrs = [row[0] for row in stale]
    log(f"startup reconciliation: resetting {len(addrs)} stale takeover(s)")
    for addr in addrs:
        # Clean up keystore dirs left from previous run
        subprocess.run([TOR_MANAGER, "release", addr],
                       capture_output=True, text=True, timeout=30)
        log(f"  released stale takeover for {addr}")

    conn.execute(
        "UPDATE registry SET status = 'online' WHERE status = 'taken-over'"
    )
    conn.commit()
    log("startup reconciliation complete — all entries will be re-polled fresh")


def main():
    log("healthcheck poller starting")

    # Wait for Arti SOCKS to be available
    wait_for_socks()

    # Wait for the DB directory to exist (shared volume may take a moment)
    data_dir = os.path.dirname(os.path.realpath("/var/lib/onionpress/onionheaven/registry.db"))
    for _ in range(30):
        if os.path.isdir("/var/lib/onionpress/onionheaven"):
            break
        time.sleep(2)
    else:
        log("WARNING: data dir not found after 60s, creating it")
        os.makedirs("/var/lib/onionpress/onionheaven", exist_ok=True)

    # Initialize DB
    conn = db_connect()
    db_ensure_schema(conn)

    # Startup reconciliation
    startup_reconciliation(conn)
    conn.close()

    log("healthcheck poller started")

    while True:
        try:
            conn = db_connect()

            # Get active entries (not unregistered)
            rows = conn.execute(
                "SELECT * FROM registry WHERE unregistered_at IS NULL ORDER BY registered_at"
            ).fetchall()

            if not rows:
                log("poll pass complete — 0 entries in 0.0s")
                conn.close()
                time.sleep(POLL_INTERVAL)
                continue

            pass_start = time.monotonic()
            entries = [dict(row) for row in rows]

            # Parallel healthchecks
            workers = min(MAX_POLL_WORKERS, len(entries))
            poll_results = {}  # (ca, ha) -> (entry_dict, ping_ok)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(poll_entry, entry): (entry["content_address"], entry["healthcheck_address"])
                    for entry in entries
                }
                for future in as_completed(futures):
                    ca, ha = futures[future]
                    try:
                        entry_dict, ping_ok = future.result()
                        poll_results[(ca, ha)] = (entry_dict, ping_ok)
                    except Exception as e:
                        log(f"entry poll error for {ha}: {e}")

            # Sequential takeover/release decisions, grouped by content_address
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            by_ca = defaultdict(list)
            for (ca, ha), (entry_dict, ping_ok) in poll_results.items():
                by_ca[ca].append((ha, entry_dict, ping_ok))

            for ca, ha_entries in by_ca.items():
                online_count = 0
                for ha, entry_dict, ping_ok in ha_entries:
                    # Record last_polled for every entry
                    conn.execute(
                        "UPDATE registry SET last_polled = ? "
                        "WHERE content_address = ? AND healthcheck_address = ?",
                        (now, ca, ha)
                    )

                    if ping_ok:
                        # Record last_healthy
                        conn.execute(
                            "UPDATE registry SET last_healthy = ? "
                            "WHERE content_address = ? AND healthcheck_address = ?",
                            (now, ca, ha)
                        )
                        conn.commit()

                        # If this row was taken-over, release it — but only
                        # if the takeover isn't very recent. Tor descriptors
                        # linger briefly after shutdown, so a ping can succeed
                        # right after /offline triggers a takeover. Wait 5s
                        # before trusting poller pings for release.
                        # (Explicit /online and /register still release immediately.)
                        current = conn.execute(
                            "SELECT status, last_taken_over FROM registry "
                            "WHERE content_address = ? AND healthcheck_address = ?",
                            (ca, ha)
                        ).fetchone()
                        if current and current["status"] == "taken-over":
                            takeover_recent = False
                            if current["last_taken_over"]:
                                try:
                                    lto = datetime.fromisoformat(
                                        current["last_taken_over"].replace("Z", "+00:00")
                                    )
                                    takeover_recent = (datetime.now(timezone.utc) - lto).total_seconds() < 5
                                except (ValueError, TypeError):
                                    pass
                            if takeover_recent:
                                log(f"Ping OK for {ha} but takeover is recent — not releasing yet")
                            else:
                                release_function(conn, ca, ha, force=False)

                        online_count += 1
                    else:
                        # Ping failed — check if we should take over
                        conn.commit()
                        current = conn.execute(
                            "SELECT status, last_healthy FROM registry "
                            "WHERE content_address = ? AND healthcheck_address = ?",
                            (ca, ha)
                        ).fetchone()

                        if current and current["status"] == "online":
                            # Check if last_healthy is stale
                            last_healthy_stale = True
                            if current["last_healthy"]:
                                try:
                                    lh = datetime.fromisoformat(
                                        current["last_healthy"].replace("Z", "+00:00")
                                    )
                                    now_dt = datetime.now(timezone.utc)
                                    elapsed = (now_dt - lh).total_seconds()
                                    last_healthy_stale = elapsed > PROPAGATION_DELAY
                                except (ValueError, TypeError):
                                    last_healthy_stale = True

                            if last_healthy_stale:
                                takeover_function(conn, ca, ha, force=False)

                conn.commit()

                # Warn if 2+ rows for same content_address are online
                if online_count >= 2:
                    log(f"WARNING: {online_count} rows for {ca} are online")

            elapsed = time.monotonic() - pass_start
            log(f"poll pass complete — {len(entries)} entries in {elapsed:.1f}s")

            conn.close()
            # Sleep at least as long as the pass took (50% duty cycle max)
            sleep_time = max(POLL_INTERVAL, elapsed)
            time.sleep(sleep_time)

        except Exception as e:
            log(f"poller error: {e}")
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(60)


if __name__ == "__main__":
    main()
