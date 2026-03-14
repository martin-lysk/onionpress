#!/usr/bin/env python3
"""
OnionHeaven Heartbeat Monitor — passive takeover orchestrator

Runs inside the onionheaven container alongside Arti (SOCKS + keystore),
onionheaven-server.py (registration API), and onionheaven-redirect.sh (302 redirects).

Unlike the old poller, this does NOT actively ping OnionPress instances.
Instead, OnionPress instances send periodic /online heartbeats to the server,
which updates last_healthy timestamps. This monitor:

  1. Scans the DB for stale heartbeats (missed 3+ beats = 180s) → takeover
  2. Audits recent takeovers by pinging the healthcheck address → detect false positives
  3. Manages farm scaling for takeover workers

All operations are local:
  - SQLite via Python sqlite3 (shared volume)
  - Post-takeover audits via curl through local Arti SOCKS (127.0.0.1:9050)
  - Takeover/release via /onionheaven-tor-manager.sh (same container)
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from onionheaven_common import (
    db_connect, db_commit_with_retry, db_ensure_schema, log,
    takeover_function, release_function, flush_sighup_arti,
    is_farm_mode, check_farm_scaling, invalidate_farm_cache,
    _check_arti_key_errors,
    PROPAGATION_DELAY, ONIONHEAVEN_PEER_GRACE, TOR_MANAGER,
)

# SOCKS proxy for post-takeover audit pings
SOCKS_ADDR = os.environ.get("ONIONHEAVEN_SOCKS_ADDR", "onionpress-tor-client:9050")

def wall_sleep(seconds):
    """Sleep using wall-clock busy-wait — time.sleep() is unreliable under qemu."""
    deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    while datetime.now(timezone.utc) < deadline:
        time.sleep(10)


# How often the monitor scans the DB (seconds)
HEARTBEAT_INTERVAL = int(os.environ.get("ONIONHEAVEN_HEARTBEAT_INTERVAL", "15"))


# ---------------------------------------------------------------------------
# Post-takeover audit — ping healthcheck to detect false positives
# ---------------------------------------------------------------------------

def check_healthcheck(healthcheck_address, socks_addr=None):
    """Check if a healthcheck .onion address is reachable."""
    proxy = socks_addr or SOCKS_ADDR
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--socks5-hostname", proxy,
             "--max-time", os.environ.get("ONIONHEAVEN_CURL_TIMEOUT", "8"),
             f"http://{healthcheck_address}/"],
            capture_output=True, text=True, timeout=15
        )
        http_code = result.stdout.strip()
        return result.returncode == 0 and http_code in ("200", "301")
    except Exception:
        return False


def audit_entry(entry):
    """Post-takeover audit: ping healthcheck to detect false positives.

    Returns True if the healthcheck responds (false positive detected).
    """
    hc_addr = entry["healthcheck_address"]
    if not hc_addr:
        return False
    return check_healthcheck(hc_addr)


# ---------------------------------------------------------------------------
# Startup
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
    """Reconcile DB state after container restart.

    Taken-over entries: re-execute takeovers (container restart wiped Arti config,
    so the onion services need to be re-added). These stay taken-over — they were
    offline before the restart and probably still are.

    Online entries: reset last_healthy to now, giving OnionPress instances a full
    grace period (180s) to send their first heartbeat before we consider them stale.
    Without this, every restart would trigger a thundering herd of takeovers.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Re-execute takeovers for entries that were taken-over before restart
    # (Arti config was wiped, so we need to re-add the onion services)
    taken_over = conn.execute(
        "SELECT DISTINCT content_address FROM registry "
        "WHERE status = 'taken-over' AND unregistered_at IS NULL"
    ).fetchall()

    if taken_over:
        addrs = [row[0] for row in taken_over]
        log(f"startup reconciliation: re-executing {len(addrs)} takeover(s)")
        for addr in addrs:
            try:
                subprocess.run([TOR_MANAGER, "takeover", "--no-sighup", addr],
                               capture_output=True, text=True, timeout=30)
                log(f"  re-took-over {addr}")
            except Exception as e:
                log(f"  failed to re-takeover {addr}: {e}")

    # Give online entries a fresh grace period
    conn.execute(
        "UPDATE registry SET last_healthy = ?, audit_result = NULL, audit_at = NULL "
        "WHERE status = 'online' AND unregistered_at IS NULL",
        (now,)
    )
    db_commit_with_retry(conn)

    online_count = conn.execute(
        "SELECT COUNT(*) FROM registry WHERE status = 'online' AND unregistered_at IS NULL"
    ).fetchone()[0]
    taken_count = len(taken_over) if taken_over else 0
    log(f"startup reconciliation complete — {online_count} online (grace period reset), {taken_count} taken-over (re-executed)")

    # Clean stale farm container entries
    import socket as _sock
    for table in ("takeover_containers",):
        try:
            rows = conn.execute(
                f"SELECT container_name FROM {table}"
            ).fetchall()
            for row in rows:
                name = row[0]
                try:
                    _sock.getaddrinfo(name, 9050, proto=_sock.IPPROTO_TCP)
                except _sock.gaierror:
                    conn.execute(
                        f"DELETE FROM {table} WHERE container_name = ?", (name,)
                    )
                    log(f"  removed stale {table} entry: {name}")
            db_commit_with_retry(conn)
        except Exception as e:
            log(f"  warning: could not clean {table}: {e}")


# ---------------------------------------------------------------------------
# Main heartbeat monitor loop
# ---------------------------------------------------------------------------

def main():
    log("heartbeat monitor starting")

    wait_for_socks()

    # Wait for the DB directory to exist
    for _ in range(30):
        if os.path.isdir("/var/lib/onionpress/onionheaven"):
            break
        time.sleep(2)
    else:
        log("WARNING: data dir not found after 60s, creating it")
        os.makedirs("/var/lib/onionpress/onionheaven", exist_ok=True)

    conn = db_connect()
    db_ensure_schema(conn)
    startup_reconciliation(conn)
    conn.close()

    # Scan Arti keystore for corrupted keys left over from previous runs
    log("Scanning Arti keystore for corrupted keys...")
    _check_arti_key_errors()

    log("heartbeat monitor started")

    while True:
        try:
            conn = db_connect()

            # Invalidate per-pass caches (farm mode, container discovery)
            invalidate_farm_cache()

            # Get active entries
            rows = conn.execute(
                "SELECT * FROM registry WHERE unregistered_at IS NULL ORDER BY registered_at"
            ).fetchall()

            if not rows:
                log("heartbeat pass complete — 0 entries in 0.0s")
                conn.close()
                wall_sleep(HEARTBEAT_INTERVAL)
                continue

            pass_start = datetime.now(timezone.utc)
            entries = [dict(row) for row in rows]
            now = datetime.now(timezone.utc)
            now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

            stale_count = 0
            audit_count = 0
            false_positive_count = 0
            stale_cleanup_count = 0

            for entry in entries:
                ca = entry["content_address"]
                ha = entry["healthcheck_address"]

                if entry["status"] == "online":
                    # Check if heartbeat is stale
                    last_healthy_stale = True
                    if entry["last_healthy"]:
                        try:
                            lh = datetime.fromisoformat(
                                entry["last_healthy"].replace("Z", "+00:00")
                            )
                            elapsed = (now - lh).total_seconds()
                            last_healthy_stale = elapsed > PROPAGATION_DELAY
                        except (ValueError, TypeError):
                            last_healthy_stale = True

                    if last_healthy_stale:
                        # OnionHeaven peers get a longer grace period before takeover.
                        # They run OnionHeaven themselves, so restarts take longer and
                        # a premature takeover would redirect their hosted sites.
                        if entry.get("is_onionheaven"):
                            if entry["last_healthy"]:
                                try:
                                    lh = datetime.fromisoformat(
                                        entry["last_healthy"].replace("Z", "+00:00")
                                    )
                                    peer_elapsed = (now - lh).total_seconds()
                                except (ValueError, TypeError):
                                    peer_elapsed = ONIONHEAVEN_PEER_GRACE + 1
                            else:
                                peer_elapsed = ONIONHEAVEN_PEER_GRACE + 1
                            if peer_elapsed < ONIONHEAVEN_PEER_GRACE:
                                log(f"Stale heartbeat for {ha} (content: {ca}) — OnionHeaven peer, waiting ({int(peer_elapsed)}s / {ONIONHEAVEN_PEER_GRACE}s grace)")
                                continue
                            log(f"Stale heartbeat for {ha} (content: {ca}) — OnionHeaven peer grace period exceeded ({int(peer_elapsed)}s), triggering takeover")
                        stale_count += 1
                        log(f"Stale heartbeat for {ha} (content: {ca}) — triggering takeover")
                        takeover_function(conn, ca, ha, force=False)

                elif entry["status"] == "taken-over":
                    # Post-takeover audit: check if healthcheck still responds (false positive)
                    # Only audit within 5 minutes of takeover
                    audit_result = entry.get("audit_result")
                    if audit_result:
                        continue  # already audited

                    last_taken_over = entry.get("last_taken_over")
                    if not last_taken_over:
                        continue

                    try:
                        lto = datetime.fromisoformat(
                            last_taken_over.replace("Z", "+00:00")
                        )
                        since_takeover = (now - lto).total_seconds()
                    except (ValueError, TypeError):
                        continue

                    # Don't audit within first 10s (Tor descriptors may linger)
                    if since_takeover < 10:
                        continue
                    # Stop auditing after 5 minutes
                    if since_takeover > 300:
                        conn.execute(
                            "UPDATE registry SET audit_result = 'confirmed_dead', audit_at = ? "
                            "WHERE content_address = ? AND healthcheck_address = ?",
                            (now_str, ca, ha)
                        )
                        db_commit_with_retry(conn)
                        continue

                    audit_count += 1
                    if audit_entry(entry):
                        false_positive_count += 1
                        log(f"FALSE POSITIVE: {ha} (content: {ca}) responded after takeover — releasing")
                        conn.execute(
                            "UPDATE registry SET audit_result = 'false_positive', audit_at = ? "
                            "WHERE content_address = ? AND healthcheck_address = ?",
                            (now_str, ca, ha)
                        )
                        db_commit_with_retry(conn)
                        release_function(conn, ca, ha, force=True)

                    # Auto-cleanup: unregister stress-test entries taken-over for >2 hours
                    # Real users will re-register when they come back online; stress tests won't.
                    version = entry.get("version", "")
                    if version and version.startswith("stress-test") and since_takeover > 7200:
                        stale_cleanup_count += 1
                        log(f"Auto-cleanup stale stress-test entry: {ca} (taken-over {since_takeover/3600:.1f}h ago)")
                        release_function(conn, ca, ha, force=True)
                        conn.execute(
                            "UPDATE registry SET unregistered_at = ?, "
                            "unregistered_reason = 'stale-stress-test-cleanup', status = 'unregistered' "
                            "WHERE content_address = ? AND healthcheck_address = ?",
                            (now_str, ca, ha)
                        )
                        db_commit_with_retry(conn)

                db_commit_with_retry(conn)

            # Flush pending SIGHUPs
            if not is_farm_mode(conn):
                flush_sighup_arti()

            # Check farm scaling (takeover workers only — no more poll workers)
            check_farm_scaling(conn, len(entries))

            elapsed = (datetime.now(timezone.utc) - pass_start).total_seconds()
            parts = [f"{len(entries)} entries"]
            if stale_count:
                parts.append(f"{stale_count} takeovers")
            if audit_count:
                parts.append(f"{audit_count} audits")
            if false_positive_count:
                parts.append(f"{false_positive_count} false positives")
            if stale_cleanup_count:
                parts.append(f"{stale_cleanup_count} stress-test cleanups")
            log(f"heartbeat pass complete — {', '.join(parts)} in {elapsed:.1f}s")

            conn.close()
            wall_sleep(max(HEARTBEAT_INTERVAL, elapsed))

        except Exception as e:
            log(f"heartbeat monitor error: {e}")
            try:
                conn.close()
            except Exception:
                pass
            wall_sleep(60)


if __name__ == "__main__":
    main()
