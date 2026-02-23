#!/usr/bin/env python3
"""
OnionCellar — failover system for OnionPress

When an OnionPress instance's .onion address goes offline, the cellar takes over
the address and serves 302 redirects to the Internet Archive Wayback Machine.

Two roles:
  1. Registration client (normal instances): sends keys to the cellar
  2. Cellar mode (cellar instance): monitors registered instances, takes over on failure
"""

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# The cellar's .onion address — placeholder until a real address is generated
CELLAR_ADDRESS = "ocellarg3xj7hpw25etw34glkjsels5q6knyxe6rmomsjplckwnexdqd.onion"

# Wayback Machine .onion address
WAYBACK_ONION = "web.archivep75mbjunhxcn6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"

# Paths inside containers
CELLAR_DATA_DIR = "/var/lib/onionpress/cellar"
CELLAR_KEYS_DIR = f"{CELLAR_DATA_DIR}/keys"
CELLAR_DB_PHP = "/var/www/html/wp-content/mu-plugins/onionpress-cellar-db.php"

# Healthcheck intervals (seconds)
HEALTHY_INTERVAL = 15        # 15 seconds between polls
FAST_POLL_INTERVAL = 15      # 15 seconds after recent failure/recovery
LONG_FAIL_INTERVAL = 1800    # 30 minutes after prolonged failure

# Thresholds
FAIL_THRESHOLD = 10          # consecutive failures before takeover (needs to survive Tor propagation delay)
FAST_POLL_COUNT = 20         # how many fast polls before slowing down

# Parallel polling
POLL_CYCLE_TARGET = 60       # target seconds per full poll pass
MAX_POLL_WORKERS = 20        # max concurrent healthcheck threads


def _docker_env(app):
    """Build environment dict for docker commands using app's config."""
    env = os.environ.copy()
    env["DOCKER_HOST"] = f"unix://{app.colima_home}/default/docker.sock"
    env["DOCKER_CONFIG"] = os.path.join(app.app_support, "docker-config")
    return env


def _docker_bin(app):
    """Return path to docker binary."""
    return os.path.join(app.bin_dir, "docker")


def _run_docker(app, args, timeout=15):
    """Run a docker command and return (success, stdout)."""
    try:
        result = subprocess.run(
            [_docker_bin(app)] + args,
            capture_output=True, text=True, timeout=timeout,
            env=_docker_env(app)
        )
        return result.returncode == 0, result.stdout.strip()
    except Exception:
        return False, ""


def _run_docker_raw(app, args, timeout=15):
    """Run a docker command returning raw bytes (for key extraction)."""
    try:
        result = subprocess.run(
            [_docker_bin(app)] + args,
            capture_output=True, timeout=timeout,
            env=_docker_env(app)
        )
        return result.returncode == 0, result.stdout
    except Exception:
        return False, b""


def _run_docker_rc(app, args, timeout=15):
    """Run a docker command and return (returncode, stdout).
    Unlike _run_docker, returns the actual return code rather than a bool."""
    try:
        result = subprocess.run(
            [_docker_bin(app)] + args,
            capture_output=True, text=True, timeout=timeout,
            env=_docker_env(app)
        )
        return result.returncode, result.stdout.strip()
    except Exception:
        return -1, ""


# ---------------------------------------------------------------------------
# Registration client (runs on normal OnionPress instances)
# ---------------------------------------------------------------------------

def _registration_status_file(app):
    """Path to the cellar registration status file."""
    return os.path.join(app.app_support, "cellar-registration.json")


def _load_registration_status(app):
    """Load persisted registration status."""
    path = _registration_status_file(app)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"registered": False, "last_attempt": None, "cellar_address": CELLAR_ADDRESS}


def _save_registration_status(app, status):
    """Persist registration status to disk."""
    path = _registration_status_file(app)
    try:
        with open(path, 'w') as f:
            json.dump(status, f, indent=2)
    except OSError:
        pass


def register_with_cellar(app):
    """
    Register this instance with the OnionCellar.
    Sends content address, healthcheck address, and secret key to the cellar.
    Called from a background thread; tries once per app startup.
    """
    # Check if registration is disabled
    if app.read_config_value("REGISTER_WITH_CELLAR", "yes").lower() == "no":
        app.log("OnionCellar registration disabled (REGISTER_WITH_CELLAR=no)")
        return

    # Don't register with ourselves
    if getattr(app, 'is_cellar', False):
        return

    content_addr = app.onion_address
    hc_addr = app.healthcheck_address

    if not content_addr or not content_addr.endswith('.onion'):
        app.log("OnionCellar: no content address available, skipping registration")
        return
    if not hc_addr or not hc_addr.endswith('.onion'):
        app.log("OnionCellar: no healthcheck address available, skipping registration")
        return

    app.log("Registering with OnionCellar...")

    # Extract the secret key (raw bytes)
    try:
        import key_manager
        secret_key_bytes = key_manager.extract_private_key()
    except Exception as e:
        app.log(f"OnionCellar: failed to extract key: {e}")
        return

    # Also get the public key (from same Arti keystore file)
    try:
        public_key_raw = key_manager.extract_public_key()
    except Exception as e:
        app.log(f"OnionCellar: failed to read public key: {e}")
        return

    # Build Arti OpenSSH PEM for cellar storage
    arti_pem = key_manager.build_openssh_key(secret_key_bytes, public_key_raw)

    # Build registration payload (keys as base64-encoded strings)
    import base64
    payload = json.dumps({
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "secret_key": base64.b64encode(secret_key_bytes).decode('ascii'),
        "public_key": base64.b64encode(public_key_raw).decode('ascii'),
        "arti_key_pem": base64.b64encode(arti_pem).decode('ascii'),
        "version": getattr(app, 'version', 'unknown'),
    })

    # Send via wordpress container's curl through tor SOCKS proxy
    # (per CLAUDE.md: use docker exec for all Tor communication)
    # Retry with backoff to handle flaky Tor circuits
    backoff = [10, 30, 60]
    max_attempts = 4
    last_output = ""

    for attempt in range(max_attempts):
        ok, output = _run_docker(app, [
            "exec", "onionpress-wordpress",
            "curl", "-s", "-X", "POST",
            "--socks5-hostname", "onionpress-tor:9050",
            "-H", "Content-Type: application/json",
            "-d", payload,
            "--max-time", "60",
            f"http://{CELLAR_ADDRESS}/register"
        ], timeout=75)
        last_output = output

        if ok and output:
            try:
                resp = json.loads(output)
                if resp.get("registered"):
                    app.log("OnionCellar: registration successful")
                    _save_registration_status(app, {
                        "registered": True,
                        "last_attempt": datetime.now(timezone.utc).isoformat(),
                        "cellar_address": CELLAR_ADDRESS,
                        "content_address": content_addr,
                    })
                    return
                if resp.get("locked"):
                    app.log("OnionCellar: cellar is locked, deferring registration (will retry on next startup)")
                    _save_registration_status(app, {
                        "registered": False,
                        "locked": True,
                        "last_attempt": datetime.now(timezone.utc).isoformat(),
                        "cellar_address": CELLAR_ADDRESS,
                    })
                    return
                # Server returned a structured error — don't retry
                error_msg = resp.get("error", "unknown error")
                app.log(f"OnionCellar: registration rejected: {error_msg}")
                break
            except json.JSONDecodeError:
                pass

        if attempt < max_attempts - 1:
            delay = backoff[min(attempt, len(backoff) - 1)]
            app.log(f"OnionCellar: registration attempt {attempt + 1} failed, retrying in {delay}s...")
            time.sleep(delay)

    app.log(f"OnionCellar: registration failed after {max_attempts} attempts (will retry on next startup) — last response: {last_output!r}")
    _save_registration_status(app, {
        "registered": False,
        "last_attempt": datetime.now(timezone.utc).isoformat(),
        "cellar_address": CELLAR_ADDRESS,
    })


def start_registration_thread(app):
    """Start the registration background thread."""
    thread = threading.Thread(target=register_with_cellar, args=(app,), daemon=True)
    thread.start()
    return thread


def unregister_from_cellar(app, content_address=None):
    """
    Unregister this instance from the OnionCellar.
    Called during uninstall or address prefix change so the cellar stops
    monitoring an address that will never come back.

    Runs synchronously — caller should invoke from a background thread if needed.
    Best-effort: logs failures but does not raise.
    """
    import hashlib

    # Don't unregister the cellar from itself
    if getattr(app, 'is_cellar', False):
        return

    # Check if we ever registered
    status = _load_registration_status(app)
    if not status.get("registered"):
        app.log("OnionCellar: not registered, skipping unregister")
        return

    addr = content_address or status.get("content_address") or getattr(app, 'onion_address', None)
    if not addr or not addr.endswith('.onion'):
        app.log("OnionCellar: no content address for unregister")
        return

    app.log(f"Unregistering {addr} from OnionCellar...")

    # Compute proof = sha256(secret_key_bytes) to authenticate the request
    try:
        import key_manager
        secret_key_bytes = key_manager.extract_private_key()
        proof = hashlib.sha256(secret_key_bytes).hexdigest()
    except Exception as e:
        app.log(f"OnionCellar: failed to extract key for unregister proof: {e}")
        return

    payload = json.dumps({
        "content_address": addr,
        "proof": proof,
    })

    # Retry with backoff — uninstall/address-change are one-shot, reliability > speed
    backoff = [5, 15, 30]
    max_attempts = 4
    last_output = ""

    for attempt in range(max_attempts):
        ok, output = _run_docker(app, [
            "exec", "onionpress-wordpress",
            "curl", "-s", "-X", "POST",
            "--socks5-hostname", "onionpress-tor:9050",
            "-H", "Content-Type: application/json",
            "-d", payload,
            "--max-time", "60",
            f"http://{CELLAR_ADDRESS}/unregister"
        ], timeout=75)
        last_output = output

        if ok and output:
            try:
                resp = json.loads(output)
                if resp.get("unregistered"):
                    app.log("OnionCellar: unregistered successfully")
                    _save_registration_status(app, {
                        "registered": False,
                        "unregistered_at": datetime.now(timezone.utc).isoformat(),
                        "cellar_address": CELLAR_ADDRESS,
                        "content_address": addr,
                    })
                    return
                error_msg = resp.get("error", "unknown error")
                app.log(f"OnionCellar: unregister rejected: {error_msg}")
                # Don't retry on auth/validation errors — they won't fix themselves
                if resp.get("error"):
                    return
            except json.JSONDecodeError:
                pass

        if attempt < max_attempts - 1:
            delay = backoff[min(attempt, len(backoff) - 1)]
            app.log(f"OnionCellar: unregister attempt {attempt + 1} failed, retrying in {delay}s...")
            time.sleep(delay)

    app.log(f"OnionCellar: unregister failed after {max_attempts} attempts (best-effort, continuing) — last response: {last_output!r}")


# ---------------------------------------------------------------------------
# Cellar mode (runs on the OnionCellar instance)
# ---------------------------------------------------------------------------

def is_cellar_instance(onion_address):
    """Check if this instance's address matches the cellar address."""
    if not onion_address or not onion_address.endswith('.onion'):
        return False
    return onion_address.strip() == CELLAR_ADDRESS.strip()


def _is_cellar_unlocked(app):
    """Check if the cellar master key is currently unlocked."""
    ok, _ = _run_docker(app, [
        "exec", "onionpress-tor",
        "test", "-f", f"{CELLAR_DATA_DIR}/.master-key-unlocked"
    ])
    return ok


def _cellar_db_init(app):
    """Initialize the cellar SQLite database (schema + JSON migration).
    Runs as www-data so DB files are owned by the web server user."""
    ok, output = _run_docker(app, [
        "exec", "--user", "www-data", "onionpress-wordpress",
        "php", CELLAR_DB_PHP, "init"
    ])
    if ok and output:
        try:
            result = json.loads(output)
            migrated = result.get("migrated", 0)
            if migrated > 0:
                app.log(f"OnionCellar: migrated {migrated} entries from JSON to SQLite")
            return True
        except json.JSONDecodeError:
            pass
    app.log(f"OnionCellar: DB init output: {output!r}")
    return ok


def _read_registry(app):
    """Read the cellar registry from SQLite via PHP CLI."""
    ok, output = _run_docker(app, [
        "exec", "--user", "www-data", "onionpress-wordpress",
        "php", CELLAR_DB_PHP, "read-all"
    ])
    if ok and output:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            app.log(f"OnionCellar: failed to parse registry JSON: {output[:200]!r}")
    return []


def _write_poll_updates(app, entries):
    """Write poll results to SQLite via PHP CLI (batch update).
    Only updates poll-related fields (status, last_healthcheck,
    fail_count, takeover_active, fast_poll_remaining).
    Uses a heredoc to safely pass JSON without shell quoting issues."""
    if not entries:
        return
    payload = json.dumps(entries)
    ok, output = _run_docker(app, [
        "exec", "-i", "--user", "www-data", "onionpress-wordpress",
        "sh", "-c", f"cat << 'POLLEOF' | php {CELLAR_DB_PHP} batch-upsert-poll\n{payload}\nPOLLEOF"
    ], timeout=30)
    if not ok:
        app.log(f"OnionCellar: batch poll update failed: {output!r}")


def _check_healthcheck(app, healthcheck_address):
    """Check if a healthcheck .onion address is reachable. Returns True if healthy.
    Uses curl in the wordpress container through the dedicated polling Tor's SOCKS
    proxy, keeping HSDir lookups off the main Tor instance that serves our onion."""
    ok, _ = _run_docker(app, [
        "exec", "onionpress-wordpress",
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
        "--socks5-hostname", "onionpress-tor-polling:9050",
        "--max-time", "15",
        f"http://{healthcheck_address}/"
    ], timeout=25)
    return ok


def _check_content(app, content_address):
    """Check if a content .onion address is reachable. Returns True if reachable.
    Uses curl in the wordpress container through the dedicated polling Tor's SOCKS proxy."""
    ok, _ = _run_docker(app, [
        "exec", "onionpress-wordpress",
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
        "--socks5-hostname", "onionpress-tor-polling:9050",
        "--max-time", "15",
        f"http://{content_address}/"
    ], timeout=25)
    return ok


def _do_takeover(app, entry):
    """Take over a failed instance's .onion address.
    Returns: 'ok', 'locked', or 'failed'."""
    content_addr = entry["content_address"]
    app.log(f"OnionCellar: Taking over {content_addr}")

    # Use cellar-tor-manager.sh inside the tor container to add the address
    rc, output = _run_docker_rc(app, [
        "exec", "onionpress-tor",
        "/cellar-tor-manager.sh", "takeover", content_addr
    ], timeout=30)

    if rc == 0:
        app.log(f"OnionCellar: Takeover complete for {content_addr}")
        return "ok"
    elif rc == 2:
        app.log(f"OnionCellar: Cellar locked, deferring takeover for {content_addr}")
        return "locked"
    else:
        app.log(f"OnionCellar: Takeover failed for {content_addr}: {output}")
        return "failed"


def _do_release(app, entry):
    """Release a recovered instance's .onion address."""
    content_addr = entry["content_address"]
    app.log(f"OnionCellar: Releasing {content_addr} — original is back online")

    ok, output = _run_docker(app, [
        "exec", "onionpress-tor",
        "/cellar-tor-manager.sh", "release", content_addr
    ], timeout=30)

    if ok:
        app.log(f"OnionCellar: Released {content_addr}")
    else:
        app.log(f"OnionCellar: Release failed for {content_addr}: {output}")

    return ok


def _poll_entry(app, entry):
    """Poll a single registry entry. Returns (entry, modified_bool, sleep_interval)."""
    content_addr = entry.get("content_address", "")
    hc_addr = entry.get("healthcheck_address", "")
    fail_count = entry.get("fail_count", 0)
    takeover_active = entry.get("takeover_active", False)
    fast_poll_remaining = entry.get("fast_poll_remaining", 0)

    if not content_addr or not hc_addr:
        return entry, False, HEALTHY_INTERVAL

    modified = False

    # Check healthcheck
    hc_ok = _check_healthcheck(app, hc_addr)

    if hc_ok:
        if takeover_active:
            # Instance recovered — the healthcheck address is independent
            # (not taken over), so it being reachable means the original
            # instance is back online. Release the content address.
            if _do_release(app, entry):
                entry["takeover_active"] = False
                entry["status"] = "healthy"
                entry["fail_count"] = 0
                entry["fast_poll_remaining"] = FAST_POLL_COUNT
            else:
                app.log(f"OnionCellar: WARNING — release failed for {content_addr}, keeping takeover_active")
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
            # Check if cellar is unlocked before attempting takeover
            if not _is_cellar_unlocked(app):
                entry["status"] = "takeover_deferred_locked"
                modified = True
            else:
                # Double-check: also test the content address
                content_ok = _check_content(app, content_addr)
                if not content_ok:
                    result = _do_takeover(app, entry)
                    if result == "ok":
                        entry["takeover_active"] = True
                        entry["status"] = "taken_over"
                    elif result == "locked":
                        entry["status"] = "takeover_deferred_locked"
                    else:
                        entry["status"] = "takeover_failed"
                    modified = True

    # Update timestamp (always mark modified so it gets persisted)
    entry["last_healthcheck"] = datetime.now(timezone.utc).isoformat()
    modified = True

    # Determine sleep interval for this entry
    sleep_interval = HEALTHY_INTERVAL
    if fast_poll_remaining > 0:
        entry["fast_poll_remaining"] = fast_poll_remaining - 1
        sleep_interval = FAST_POLL_INTERVAL
        modified = True
    elif takeover_active:
        sleep_interval = LONG_FAIL_INTERVAL

    return entry, modified, sleep_interval


def cellar_poller(app):
    """
    Main cellar polling loop. Monitors registered instances and manages takeover/release.
    Runs as a background thread on the cellar instance.
    Uses a thread pool to check entries in parallel.

    With SQLite, we only UPDATE the rows we polled — no merge logic needed.
    New registrations go through PHP's cellar_db_upsert_register() on separate
    columns, so there are no read-modify-write races.
    """
    app.log("OnionCellar: healthcheck poller started")

    # Wait for services to be ready
    while not app.is_ready:
        time.sleep(10)

    # Initialize the DB (schema + JSON migration) on first run
    _cellar_db_init(app)

    while True:
        try:
            registry = _read_registry(app)
            if not registry:
                app.log("OnionCellar: poll pass complete — 0 entries in 0.0s")
                time.sleep(HEALTHY_INTERVAL)
                continue

            pass_start = time.monotonic()
            modified_entries = []
            min_sleep = HEALTHY_INTERVAL

            # Immediate release: if an entry re-registered while taken over,
            # the PHP upsert resets status='healthy' but leaves takeover_active=1.
            # Registration over Tor proves the instance is alive — release now.
            for entry in registry:
                if entry.get("takeover_active") and entry.get("status") == "healthy":
                    app.log(f"OnionCellar: {entry['content_address']} re-registered — immediate release")
                    if _do_release(app, entry):
                        entry["takeover_active"] = False
                        entry["fail_count"] = 0
                        entry["fast_poll_remaining"] = 0
                    else:
                        app.log(f"OnionCellar: WARNING — immediate release failed for {entry['content_address']}, keeping takeover_active")
                        entry["status"] = "release_failed"
                    entry["last_healthcheck"] = datetime.now(timezone.utc).isoformat()
                    modified_entries.append(entry)

            workers = min(MAX_POLL_WORKERS, len(registry))

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_poll_entry, app, entry): i
                    for i, entry in enumerate(registry)
                }
                for future in as_completed(futures):
                    try:
                        entry, modified, sleep_interval = future.result()
                        if modified:
                            modified_entries.append(entry)
                        min_sleep = min(min_sleep, sleep_interval)
                    except Exception as e:
                        app.log(f"OnionCellar: entry poll error: {e}")

            elapsed = time.monotonic() - pass_start
            app.log(f"OnionCellar: poll pass complete — {len(registry)} entries in {elapsed:.1f}s")

            if modified_entries:
                _write_poll_updates(app, modified_entries)

            time.sleep(min_sleep)

        except Exception as e:
            app.log(f"OnionCellar: poller error: {e}")
            time.sleep(60)


def start_cellar_poller(app):
    """Start the cellar healthcheck polling thread."""
    thread = threading.Thread(target=cellar_poller, args=(app,), daemon=True)
    thread.start()
    return thread
