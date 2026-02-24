#!/usr/bin/env python3
"""
OnionCellar — failover system for OnionPress

When an OnionPress instance's .onion address goes offline, the cellar takes over
the address and serves 302 redirects to the Internet Archive Wayback Machine.

This module handles the registration client (normal instances send keys to the cellar)
and cellar mode detection for the menubar UI.

The cellar poller (healthcheck monitoring, takeover, release) runs inside the
tor-polling container as cellar-poller.py — not in this module.
"""

import hashlib
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

# The cellar's .onion address — placeholder until a real address is generated
CELLAR_ADDRESS = "ocellarg3xj7hpw25etw34glkjsels5q6knyxe6rmomsjplckwnexdqd.onion"

# Wayback Machine .onion address
WAYBACK_ONION = "web.archivep75mbjunhxcn6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion"

# Paths inside containers
CELLAR_DATA_DIR = "/var/lib/onionpress/cellar"


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
# Online/offline notifications (runs on normal OnionPress instances)
# ---------------------------------------------------------------------------

def _send_cellar_notification(app, endpoint, log_label, max_attempts=1, max_time=10):
    """Send a lifecycle notification (/online or /offline) to the cellar.

    Builds payload with content_address, healthcheck_address, and proof.
    Uses docker exec curl through the Tor SOCKS proxy.

    Returns True on success, False on failure.
    """
    if getattr(app, 'is_cellar', False):
        return False

    # Check if we ever registered
    status = _load_registration_status(app)
    if not status.get("registered"):
        return False

    content_addr = getattr(app, 'onion_address', None)
    hc_addr = getattr(app, 'healthcheck_address', None)

    if not content_addr or not content_addr.endswith('.onion'):
        return False
    if not hc_addr or not hc_addr.endswith('.onion'):
        return False

    # Compute proof = sha256(secret_key_bytes)
    try:
        import key_manager
        secret_key_bytes = key_manager.extract_private_key()
        proof = hashlib.sha256(secret_key_bytes).hexdigest()
    except Exception as e:
        app.log(f"OnionCellar: failed to extract key for /{endpoint} proof: {e}")
        return False

    payload = json.dumps({
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "proof": proof,
    })

    backoff = [10, 30]
    last_output = ""

    for attempt in range(max_attempts):
        ok, output = _run_docker(app, [
            "exec", "onionpress-wordpress",
            "curl", "-s", "-X", "POST",
            "--socks5-hostname", "onionpress-tor:9050",
            "-H", "Content-Type: application/json",
            "-d", payload,
            "--max-time", str(max_time),
            f"http://{CELLAR_ADDRESS}/{endpoint}"
        ], timeout=max_time + 15)
        last_output = output

        if ok and output:
            try:
                resp = json.loads(output)
                if resp.get("online" if endpoint == "online" else "offline"):
                    app.log(f"OnionCellar: /{endpoint} notification sent")
                    return True
                error_msg = resp.get("error", "unknown error")
                app.log(f"OnionCellar: /{endpoint} rejected: {error_msg}")
                return False
            except json.JSONDecodeError:
                pass

        if attempt < max_attempts - 1:
            delay = backoff[min(attempt, len(backoff) - 1)]
            app.log(f"OnionCellar: /{endpoint} attempt {attempt + 1} failed, retrying in {delay}s...")
            time.sleep(delay)

    app.log(f"OnionCellar: /{endpoint} failed after {max_attempts} attempts — last response: {last_output!r}")
    return False


def notify_cellar_offline(app):
    """Notify the cellar that this instance is going offline (sleep/quit).

    Runs synchronously with a single attempt and short timeout —
    must complete before the system sleeps or the app quits.
    """
    if getattr(app, 'is_cellar', False):
        return False
    app.log("Notifying cellar: going offline")
    return _send_cellar_notification(app, "offline", "offline", max_attempts=1, max_time=10)


def notify_cellar_online(app):
    """Notify the cellar that this instance is back online (wake/reconnect).

    Retries with backoff since Tor circuits may take time to rebuild after wake.
    """
    if getattr(app, 'is_cellar', False):
        return False
    app.log("Notifying cellar: coming online")
    return _send_cellar_notification(app, "online", "online", max_attempts=3, max_time=30)


def start_online_notification_thread(app):
    """Spawn a daemon thread to send /online notification."""
    thread = threading.Thread(target=notify_cellar_online, args=(app,), daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Cellar mode detection and UI helpers (poller runs in tor-polling container)
# ---------------------------------------------------------------------------

def is_cellar_instance(onion_address):
    """Check if this instance's address matches the cellar address."""
    if not onion_address or not onion_address.endswith('.onion'):
        return False
    return onion_address.strip() == CELLAR_ADDRESS.strip()


