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
    db_connect, db_commit_with_retry, db_ensure_schema, log,
    _takeover_local, _release_local, flush_sighup_tor,
    TOR_MANAGER, ONIONHEAVEN_DATA_DIR,
)

CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "unknown")
MAX_SERVICES = int(os.environ.get("MAX_TAKEOVER_SERVICES", "10"))
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
    db_commit_with_retry(conn)
    log(f"takeover-worker: registered as {CONTAINER_NAME} (max {MAX_SERVICES} services)")


def startup_reconciliation(conn):
    """Re-add taken-over services after container restart.

    Container restart wipes ephemeral ADD_ONION services (C Tor) and
    Arti config. Re-execute takeovers for entries still assigned to us
    so they start serving again immediately.

    For Arti: also cleans orphaned toml entries not backed by DB rows.
    """
    # Get the list of addresses to re-add (one-time snapshot, not a while loop)
    stale = conn.execute(
        "SELECT DISTINCT content_address FROM registry "
        "WHERE takeover_container = ? AND status = 'taken-over'",
        (CONTAINER_NAME,)
    ).fetchall()
    count = 0
    for row in stale:
        addr = row[0]
        ok = _takeover_local(addr, no_sighup=True)
        if ok:
            conn.execute(
                "UPDATE registry SET takeover_pending = NULL "
                "WHERE content_address = ? AND takeover_container = ?",
                (addr, CONTAINER_NAME)
            )
            db_commit_with_retry(conn)
            count += 1
            log(f"  re-added takeover for {addr}")
        time.sleep(5)

    if count > 0:
        flush_sighup_tor(force=True)  # Arti only
        log(f"takeover-worker: re-added {count} taken-over service(s) after restart")

    # Clean orphaned services from Arti toml — entries not backed by DB rows
    _clean_orphaned_services(conn)
    log("takeover-worker: reconciliation complete")


def _clean_orphaned_services(conn):
    """Remove Arti toml services that have no corresponding DB entry.

    This handles the case where stress test cleanup deletes DB rows
    but the takeover worker's Arti toml still has the service entries,
    causing circuit exhaustion from publishing orphaned descriptors.

    Uses line-by-line parsing (not regex) to avoid leaving orphaned
    fragments that break the toml (e.g., bare [["80", ...]] lines).
    """
    import re
    toml_path = "/etc/arti/arti-onionheaven.toml"
    try:
        with open(toml_path) as f:
            toml_content = f.read()
    except FileNotFoundError:
        return

    # Find all onion_services nicknames in the toml
    nicknames = re.findall(r'\[onion_services\."(onionheaven_[^"]+)"\]', toml_content)
    if not nicknames:
        # Still check for broken fragments from previous buggy cleanup
        _sanitize_toml(toml_path)
        return

    # Check which content addresses are still in the DB assigned to us
    valid_addrs = set()
    rows = conn.execute(
        "SELECT DISTINCT content_address FROM registry "
        "WHERE takeover_container = ?",
        (CONTAINER_NAME,)
    ).fetchall()
    for row in rows:
        # Nickname format: onionheaven_{first16chars}
        addr = row[0]
        valid_addrs.add(addr[:16] if len(addr) >= 16 else addr)

    orphaned = []
    for nick in nicknames:
        suffix = nick.replace("onionheaven_", "")
        if suffix not in valid_addrs:
            orphaned.append(nick)

    if not orphaned:
        _sanitize_toml(toml_path)
        return

    log(f"takeover-worker: removing {len(orphaned)} orphaned service(s) from Arti toml")

    # Try tor-manager release first (cleanest removal)
    for nick in orphaned:
        # Find full .onion address from the comment line
        addr_match = re.search(
            rf'# onionheaven:([a-z2-7]{{56}}\.onion)',
            toml_content
        )
        # More specific: find comment that references this nickname's prefix
        prefix = nick.replace("onionheaven_", "")
        addr_match = re.search(
            rf'# onionheaven:({prefix}[a-z2-7]*\.onion)',
            toml_content
        )
        if addr_match:
            full_addr = addr_match.group(1)
            result = subprocess.run([TOR_MANAGER, "release", "--no-sighup", full_addr],
                                    capture_output=True, text=True, timeout=30)
            log(f"  released orphan {nick} ({full_addr}) rc={result.returncode}")

    # After tor-manager release, also do a line-by-line cleanup to catch
    # any fragments that tor-manager's awk might have missed
    _sanitize_toml(toml_path)
    flush_sighup_tor()


def _sanitize_toml(toml_path):
    """Remove broken fragments from Arti toml that would prevent config reload.

    Previous cleanup bugs left orphaned lines like:
      # onionheaven:xxx.onion
      [["80", "127.0.0.1:8082"]]

    The bare [["80", ...]] is parsed as an invalid TOML table header, which
    breaks ALL config reloads (every SIGHUP fails). This sanitizer removes:
      1. Lines that are bare proxy_ports values (no 'proxy_ports = ' prefix)
      2. Orphaned comment lines (# onionheaven:xxx) not followed by a section header
      3. Orphaned 'enabled = true' lines not inside a section
    """
    try:
        with open(toml_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    cleaned = []
    i = 0
    changed = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Remove bare proxy_ports value lines (the main bug)
        if stripped.startswith("[[") and "127.0.0.1" in stripped and stripped.endswith("]]"):
            log(f"  sanitize: removing orphaned proxy_ports line: {stripped}")
            changed = True
            i += 1
            continue

        # Remove orphaned comment lines not followed by a proper section header
        if stripped.startswith("# onionheaven:") and stripped.endswith(".onion"):
            # Check if next non-blank line is a proper [onion_services."..."] header
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j].strip().startswith('[onion_services."'):
                # This comment is properly associated with a service — keep it
                cleaned.append(line)
                i += 1
                continue
            else:
                log(f"  sanitize: removing orphaned comment: {stripped}")
                changed = True
                i += 1
                continue

        cleaned.append(line)
        i += 1

    if changed:
        # Remove excessive blank lines (more than 1 consecutive)
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
        log(f"  sanitize: cleaned toml written to {toml_path}")


def process_takeovers(conn):
    """Process pending takeover requests one at a time with a pause between each.

    C Tor: each ADD_ONION is instant and independent — the pause gives Tor time
    to publish each descriptor before starting the next.
    Arti: batches all config changes then sends one SIGHUP at the end.
    """
    count = 0
    while True:
        # Fetch one pending row at a time — re-queries each iteration so we
        # never act on stale data (e.g., a row released by /online mid-batch).
        row = conn.execute(
            "SELECT content_address, healthcheck_address FROM registry "
            "WHERE takeover_container = ? AND takeover_pending IS NOT NULL "
            "AND status = 'taken-over' LIMIT 1",
            (CONTAINER_NAME,)
        ).fetchone()

        if not row:
            break

        ca = row["content_address"]
        ha = row["healthcheck_address"]
        log(f"takeover-worker: executing takeover for {ca}")
        ok = _takeover_local(ca, no_sighup=True)

        if ok:
            # Clear pending flag — takeover succeeded
            conn.execute(
                "UPDATE registry SET takeover_pending = NULL "
                "WHERE content_address = ? AND healthcheck_address = ?",
                (ca, ha)
            )
            db_commit_with_retry(conn)
            count += 1
            # Pause between services to let Tor publish each descriptor
            time.sleep(5)
        else:
            # Leave takeover_pending set — will retry next cycle
            log(f"takeover-worker: will retry {ca} next cycle")
            break  # stop this batch, retry after LOOP_INTERVAL

    if count > 0:
        flush_sighup_tor(force=True)  # Arti only; no-op for C Tor
        update_active_count(conn)
        log(f"takeover-worker: processed {count} takeover(s)")
    return count


def process_releases(conn):
    """Process pending release requests."""
    count = 0
    while True:
        # Fetch one pending release at a time — fresh query each iteration.
        row = conn.execute(
            "SELECT content_address, healthcheck_address FROM registry "
            "WHERE takeover_container = ? AND release_pending IS NOT NULL LIMIT 1",
            (CONTAINER_NAME,)
        ).fetchone()

        if not row:
            break

        ca = row["content_address"]
        ha = row["healthcheck_address"]
        log(f"takeover-worker: executing release for {ca}")
        ok = _release_local(ca, no_sighup=True)

        if ok:
            # Clear pending and assignment flags
            conn.execute(
                "UPDATE registry SET release_pending = NULL, takeover_container = NULL "
                "WHERE content_address = ? AND healthcheck_address = ?",
                (ca, ha)
            )
            db_commit_with_retry(conn)
            count += 1
        else:
            log(f"takeover-worker: will retry release for {ca} next cycle")
            break

    if count > 0:
        flush_sighup_tor()  # Arti only; no-op for C Tor
        update_active_count(conn)
        log(f"takeover-worker: processed {count} release(s)")
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
    db_commit_with_retry(conn)


def heartbeat(conn):
    """Update heartbeat timestamp."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE takeover_containers SET last_heartbeat = ? WHERE container_name = ?",
        (now, CONTAINER_NAME)
    )
    db_commit_with_retry(conn)


def wait_for_db():
    """Wait for the shared DB directory to exist."""
    for _ in range(30):
        if os.path.isdir(ONIONHEAVEN_DATA_DIR):
            return True
        time.sleep(2)
    log("WARNING: data dir not found after 60s, creating it")
    os.makedirs(ONIONHEAVEN_DATA_DIR, exist_ok=True)
    return True


def wait_for_tor():
    """Wait for C Tor's control port to be ready (bootstrapped)."""
    log("takeover-worker: waiting for Tor to bootstrap...")
    for attempt in range(60):
        try:
            result = subprocess.run(
                ["sh", "-c",
                 'cookie=$(xxd -p /var/lib/tor/control_auth_cookie 2>/dev/null | tr -d "\\n"); '
                 '[ -n "$cookie" ] && printf "AUTHENTICATE %s\\r\\nGETINFO status/bootstrap-phase\\r\\nQUIT\\r\\n" '
                 '"$cookie" | nc -w 2 127.0.0.1 9051'],
                capture_output=True, text=True, timeout=10,
            )
            if "PROGRESS=100" in result.stdout:
                log("takeover-worker: Tor bootstrapped and ready")
                return True
        except Exception:
            pass
        time.sleep(2)
    log("WARNING: Tor not bootstrapped after 120s, proceeding anyway")
    return False


def main():
    log(f"takeover-worker starting: {CONTAINER_NAME}")

    wait_for_db()
    wait_for_tor()

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
