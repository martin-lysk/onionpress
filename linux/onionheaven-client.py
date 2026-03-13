#!/usr/bin/env python3
"""
OnionHeaven Client Daemon for OnionPress (Linux)

Standalone daemon that registers this OnionPress instance with an OnionHeaven hub
and sends periodic heartbeats. Runs as a systemd service.

Reads config from ~/.onionpress/config:
  REGISTER_WITH_ONIONHEAVEN=yes|no  (default: yes)
  ONIONHEAVEN_ADDRESS=<hub .onion>  (default: centralized OH)

Imports onion_auth and key_manager from /opt/onionpress/scripts/.
"""

import base64
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

# Add scripts directory to path for onion_auth and key_manager imports
SCRIPTS_DIR = "/opt/onionpress/scripts"
sys.path.insert(0, SCRIPTS_DIR)

import key_manager
import onion_auth

# Defaults
DEFAULT_HUB = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
API_PORT = 8083
HEARTBEAT_INTERVAL = 60
READY_TIMEOUT = 300  # 5 min

# State
DATA_DIR = os.path.expanduser("~/.onionpress")
CONFIG_PATH = os.path.join(DATA_DIR, "config")
STATE_PATH = os.path.join(DATA_DIR, "onionheaven-registration.json")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DATA_DIR, "onionpress.log")),
    ],
)
log = logging.getLogger("onionheaven-client")

# Global for graceful shutdown
_current_hub = None
_content_addr = None
_hc_addr = None
_priv_key = None
_pub_key = None
_running = True


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def read_config():
    """Parse KEY=VALUE config file. Returns dict."""
    config = {}
    try:
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return config


# ---------------------------------------------------------------------------
# Docker exec helper
# ---------------------------------------------------------------------------

def docker_exec(container, args, timeout=30):
    """Run a command inside a Docker container. Returns (success, stdout)."""
    cmd = ["docker", "exec", container] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout
        )
        if result.returncode != 0 and result.stderr.strip():
            log.debug("docker exec %s failed: %s", container, result.stderr.strip()[:200])
        return result.returncode == 0, result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning("docker exec %s timed out after %ds", container, timeout)
        return False, ""
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Wait for readiness
# ---------------------------------------------------------------------------

def wait_for_ready():
    """Wait for content address, healthcheck address, and keys.
    Returns (content_addr, hc_addr, priv_key, pub_key) or None on timeout.
    """
    deadline = time.time() + READY_TIMEOUT
    backoff = 5

    while time.time() < deadline and _running:
        # Content address
        ok, content = docker_exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
            timeout=10,
        )
        if not ok or not content.endswith(".onion"):
            log.info("Waiting for content address...")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30)
            continue

        # Healthcheck address
        ok, hc = docker_exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/healthcheck/hostname"],
            timeout=10,
        )
        if not ok or not hc.endswith(".onion"):
            log.info("Waiting for healthcheck address...")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30)
            continue

        # Keys
        try:
            priv_key, pub_key = key_manager.extract_keys()
        except Exception as e:
            log.info("Waiting for keys: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30)
            continue

        return content.strip(), hc.strip(), priv_key, pub_key

    return None


# ---------------------------------------------------------------------------
# Signing + POST via tor-client
# ---------------------------------------------------------------------------

def sign_and_post(endpoint, hub_addr, content_addr, hc_addr, priv_key, pub_key, extra=None):
    """Sign payload and POST to hub via docker exec curl through tor-client.
    Returns (success, response_dict_or_None).
    """
    timestamp = onion_auth.make_timestamp()
    signature = onion_auth.sign_payload(
        priv_key, pub_key,
        endpoint, content_addr, hc_addr, timestamp
    )

    payload = {
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "timestamp": timestamp,
        "signature": signature,
    }
    if extra:
        payload.update(extra)

    payload_json = json.dumps(payload)

    ok, output = docker_exec(
        "onionpress-tor-client",
        [
            "curl", "-s", "-X", "POST",
            "--socks5-hostname", "127.0.0.1:9050",
            "-H", "Content-Type: application/json",
            "-d", payload_json,
            "--max-time", "60",
            f"http://{hub_addr}:{API_PORT}/{endpoint}",
        ],
        timeout=75,
    )

    if ok and output:
        try:
            return True, json.loads(output)
        except json.JSONDecodeError:
            log.warning("POST /%s: non-JSON response: %s", endpoint, output[:200])
            return False, None
    log.debug("POST /%s: ok=%s output=%r", endpoint, ok, output[:200] if output else "")
    return False, None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(hub_addr, content_addr, hc_addr, priv_key, pub_key):
    """Register with hub. Retries 4x with backoff. Returns True on success."""
    arti_pem = key_manager.build_openssh_key(priv_key, pub_key)
    extra = {
        "arti_key_pem": base64.b64encode(arti_pem).decode("ascii"),
        "version": _read_version(),
    }

    backoff_delays = [10, 30, 30]
    for attempt in range(4):
        log.info("Registration attempt %d/4 with %s...", attempt + 1, hub_addr)
        ok, resp = sign_and_post("register", hub_addr, content_addr, hc_addr, priv_key, pub_key, extra)

        if ok and resp:
            if resp.get("registered"):
                log.info("Registration successful: %s", resp)
                _save_state({
                    "registered": True,
                    "last_attempt": datetime.now(timezone.utc).isoformat(),
                    "onionheaven_address": hub_addr,
                    "content_address": content_addr,
                })
                return True
            error = resp.get("error", "unknown")
            log.error("Registration rejected: %s", error)
            return False

        if attempt < 3:
            delay = backoff_delays[attempt]
            log.info("Registration failed, retrying in %ds...", delay)
            time.sleep(delay)

    log.error("Registration failed after 4 attempts")
    _save_state({
        "registered": False,
        "last_attempt": datetime.now(timezone.utc).isoformat(),
        "onionheaven_address": hub_addr,
    })
    return False


# ---------------------------------------------------------------------------
# Unregister
# ---------------------------------------------------------------------------

def unregister(hub_addr, content_addr, hc_addr, priv_key, pub_key):
    """Unregister from hub. Retries 4x — critical to avoid false takeover."""
    backoff_delays = [5, 15, 30]
    for attempt in range(4):
        log.info("Unregister attempt %d/4 from %s...", attempt + 1, hub_addr)
        ok, resp = sign_and_post("unregister", hub_addr, content_addr, hc_addr, priv_key, pub_key)

        if ok and resp:
            if resp.get("unregistered"):
                log.info("Unregistered successfully from %s", hub_addr)
                _save_state({
                    "registered": False,
                    "unregistered_at": datetime.now(timezone.utc).isoformat(),
                    "onionheaven_address": hub_addr,
                    "content_address": content_addr,
                })
                return True
            error = resp.get("error", "unknown")
            log.error("Unregister rejected: %s", error)
            if resp.get("error"):
                return False

        if attempt < 3:
            delay = backoff_delays[min(attempt, len(backoff_delays) - 1)]
            log.info("Unregister failed, retrying in %ds...", delay)
            time.sleep(delay)

    log.error("Unregister failed after 4 attempts (best-effort)")
    return False


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def heartbeat(hub_addr, content_addr, hc_addr, priv_key, pub_key):
    """Send a single /online heartbeat. Returns True on success."""
    # Check WordPress health
    wp_healthy = False
    ok, output = docker_exec(
        "onionpress-wordpress",
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", "http://localhost:80/"],
        timeout=10,
    )
    if ok and output.strip() in ("200", "301", "302"):
        wp_healthy = True

    log.debug("WP health check: ok=%s output=%r healthy=%s", ok, output, wp_healthy)

    ok, resp = sign_and_post(
        "online", hub_addr, content_addr, hc_addr, priv_key, pub_key,
        extra={"wordpress_healthy": wp_healthy},
    )

    if ok and resp and resp.get("online"):
        log.info("Heartbeat OK (wp_healthy=%s)", wp_healthy)
        return True
    if ok and resp:
        log.warning("Heartbeat rejected: %s", resp.get("error", "unknown"))
    else:
        log.warning("Heartbeat failed (will retry next cycle)")
    return False


# ---------------------------------------------------------------------------
# Offline notification
# ---------------------------------------------------------------------------

def send_offline(hub_addr, content_addr, hc_addr, priv_key, pub_key):
    """Best-effort /offline notification."""
    try:
        ok, resp = sign_and_post("offline", hub_addr, content_addr, hc_addr, priv_key, pub_key)
        if ok and resp and resp.get("offline"):
            log.info("Sent /offline to %s", hub_addr)
        else:
            log.warning("Failed to send /offline to %s", hub_addr)
    except Exception as e:
        log.warning("Failed to send /offline: %s", e)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _save_state(data):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        log.warning("Failed to save state: %s", e)


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _read_version():
    try:
        with open("/opt/onionpress/VERSION") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

def _signal_handler(signum, frame):
    global _running
    log.info("Received signal %d, shutting down...", signum)
    _running = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _current_hub, _content_addr, _hc_addr, _priv_key, _pub_key, _running

    os.makedirs(DATA_DIR, exist_ok=True)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    log.info("OnionHeaven client starting...")

    # Wait for addresses and keys
    result = wait_for_ready()
    if result is None:
        log.error("Timed out waiting for addresses/keys after %ds", READY_TIMEOUT)
        sys.exit(1)

    _content_addr, _hc_addr, _priv_key, _pub_key = result
    log.info("Content address: %s", _content_addr)
    log.info("Healthcheck address: %s", _hc_addr)

    # Read initial config
    config = read_config()
    enabled = config.get("REGISTER_WITH_ONIONHEAVEN", "yes").lower() != "no"
    _current_hub = config.get("ONIONHEAVEN_ADDRESS", DEFAULT_HUB)

    # Self-registration check: if our address IS the hub, skip all client activity
    if _content_addr == _current_hub:
        log.info("This node IS the OnionHeaven hub (%s) — skipping client registration", _current_hub)
        # Sleep forever (or until signal) — the server container handles everything
        while _running:
            time.sleep(60)
        return

    registered = False

    if enabled:
        registered = register(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
    else:
        log.info("Registration disabled (REGISTER_WITH_ONIONHEAVEN=no)")

    # Heartbeat loop
    while _running:
        time.sleep(HEARTBEAT_INTERVAL)
        if not _running:
            break

        # Re-read config each cycle
        config = read_config()
        new_enabled = config.get("REGISTER_WITH_ONIONHEAVEN", "yes").lower() != "no"
        new_hub = config.get("ONIONHEAVEN_ADDRESS", DEFAULT_HUB)

        # Handle hub address change
        if new_hub != _current_hub and registered:
            log.info("Hub address changed from %s to %s", _current_hub, new_hub)
            # Unregister from old hub to prevent false takeover
            unregister(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
            registered = False
            _current_hub = new_hub
            if new_enabled:
                # Self-check for new hub
                if _content_addr == _current_hub:
                    log.info("New hub is this node — stopping client activity")
                    while _running:
                        time.sleep(60)
                    break
                registered = register(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
            continue
        _current_hub = new_hub

        # Handle enable/disable toggle
        if not new_enabled and registered:
            log.info("Registration disabled — sending /offline")
            send_offline(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
            registered = False
            enabled = False
            continue

        if new_enabled and not enabled and not registered:
            log.info("Registration re-enabled — registering")
            if _content_addr == _current_hub:
                log.info("This node IS the hub — skipping")
                enabled = True
                continue
            registered = register(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
            enabled = True
            continue

        enabled = new_enabled

        # Retry registration if not yet registered
        if enabled and not registered:
            if _content_addr != _current_hub:
                log.info("Retrying registration with %s...", _current_hub)
                registered = register(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
            continue

        # Send heartbeat if registered
        if registered:
            heartbeat(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)

    # Graceful shutdown: send /offline
    if registered and _current_hub:
        log.info("Sending /offline before shutdown...")
        send_offline(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)

    log.info("OnionHeaven client stopped")


if __name__ == "__main__":
    main()
