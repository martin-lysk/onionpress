#!/usr/bin/env python3
"""
Key Management for OnionPress
Extract and write Arti Ed25519 expanded private keys to/from the onionpress-tor container.
Keys are stored in OpenSSH format at the Arti keystore path.
"""

import base64
import struct
import subprocess

ARTI_KEYSTORE_PATH = "/var/lib/arti/state/keystore/hss/wordpress/ks_hs_id.ed25519_expanded_private"
ARTI_KEY_TYPE = b"ed25519-expanded@spec.torproject.org"
CONTAINER = "onionpress-tor"

OPENSSH_MAGIC = b"openssh-key-v1\x00"


def _pack_string(data):
    """Pack bytes as uint32 big-endian length + data."""
    return struct.pack(">I", len(data)) + data


def _unpack_string(buf, offset):
    """Unpack a uint32-length-prefixed string from buf at offset. Returns (data, new_offset)."""
    if offset + 4 > len(buf):
        raise ValueError("Truncated length field")
    length = struct.unpack(">I", buf[offset:offset + 4])[0]
    offset += 4
    if offset + length > len(buf):
        raise ValueError(f"Truncated data: need {length} bytes at offset {offset}, have {len(buf) - offset}")
    return buf[offset:offset + length], offset + length


def parse_openssh_key(data):
    """
    Parse an OpenSSH private key file (PEM format, unencrypted).
    Returns (private_key_64bytes, public_key_32bytes).
    """
    # Strip PEM headers and decode base64
    text = data.decode("utf-8", errors="replace")
    lines = text.strip().splitlines()
    b64_lines = []
    in_body = False
    for line in lines:
        if line.startswith("-----BEGIN"):
            in_body = True
            continue
        if line.startswith("-----END"):
            break
        if in_body:
            b64_lines.append(line.strip())
    if not b64_lines:
        raise ValueError("No PEM body found in key file")

    raw = base64.b64decode("".join(b64_lines))

    # Parse binary format
    if not raw.startswith(OPENSSH_MAGIC):
        raise ValueError("Missing openssh-key-v1 magic")
    offset = len(OPENSSH_MAGIC)

    cipher, offset = _unpack_string(raw, offset)
    kdf, offset = _unpack_string(raw, offset)
    kdf_options, offset = _unpack_string(raw, offset)

    if cipher != b"none" or kdf != b"none":
        raise ValueError("Encrypted keys are not supported")

    nkeys = struct.unpack(">I", raw[offset:offset + 4])[0]
    offset += 4
    if nkeys != 1:
        raise ValueError(f"Expected 1 key, got {nkeys}")

    # Skip public key blob (we'll get it from the private section)
    _pub_blob, offset = _unpack_string(raw, offset)

    # Parse private key blob
    priv_blob, offset = _unpack_string(raw, offset)
    p = 0

    check1 = struct.unpack(">I", priv_blob[p:p + 4])[0]
    p += 4
    check2 = struct.unpack(">I", priv_blob[p:p + 4])[0]
    p += 4
    if check1 != check2:
        raise ValueError("Check integers mismatch — key may be corrupt or encrypted")

    key_type, p = _unpack_string(priv_blob, p)
    if key_type != ARTI_KEY_TYPE:
        raise ValueError(f"Unexpected key type: {key_type!r}")

    public_key, p = _unpack_string(priv_blob, p)
    if len(public_key) != 32:
        raise ValueError(f"Public key is {len(public_key)} bytes, expected 32")

    secret_blob, p = _unpack_string(priv_blob, p)
    # Arti writes just the 64-byte expanded private key (no appended public key),
    # unlike standard OpenSSH ed25519 which concatenates private+public.
    if len(secret_blob) == 64:
        private_key = secret_blob
    elif len(secret_blob) == 96:
        private_key = secret_blob[:64]
        if secret_blob[64:] != public_key:
            raise ValueError("Embedded public key in private blob does not match")
    else:
        raise ValueError(f"Secret blob is {len(secret_blob)} bytes, expected 64 or 96")

    return private_key, public_key


def build_openssh_key(private_key, public_key):
    """
    Build an OpenSSH private key file (PEM format, unencrypted) for Arti.
    private_key: 64 bytes (expanded Ed25519)
    public_key: 32 bytes
    Returns bytes (PEM-encoded).
    """
    if len(private_key) != 64:
        raise ValueError(f"Private key must be 64 bytes, got {len(private_key)}")
    if len(public_key) != 32:
        raise ValueError(f"Public key must be 32 bytes, got {len(public_key)}")

    # Build public key blob
    pub_blob = _pack_string(ARTI_KEY_TYPE) + _pack_string(public_key)

    # Build private key blob
    import os
    check = struct.pack(">I", int.from_bytes(os.urandom(4), "big"))
    priv_blob = (
        check + check +  # checkint1 == checkint2
        _pack_string(ARTI_KEY_TYPE) +
        _pack_string(public_key) +
        _pack_string(private_key) +  # Arti uses just 64-byte expanded privkey
        _pack_string(b"")  # empty comment
    )
    # Pad to 8-byte boundary with 1,2,3,4,...
    pad_len = (8 - len(priv_blob) % 8) % 8
    priv_blob += bytes(range(1, pad_len + 1))

    # Assemble full binary
    binary = (
        OPENSSH_MAGIC +
        _pack_string(b"none") +       # cipher
        _pack_string(b"none") +       # kdf
        _pack_string(b"") +           # kdf options
        struct.pack(">I", 1) +        # nkeys
        _pack_string(pub_blob) +
        _pack_string(priv_blob)
    )

    # PEM-wrap
    b64 = base64.b64encode(binary).decode("ascii")
    lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    pem += "\n".join(lines) + "\n"
    pem += "-----END OPENSSH PRIVATE KEY-----\n"
    return pem.encode("utf-8")


def extract_keys():
    """
    Extract both Ed25519 keys from the Arti keystore in one docker exec.
    Returns (private_key_64bytes, public_key_32bytes).
    """
    try:
        result = subprocess.run(
            ["docker", "exec", CONTAINER, "cat", ARTI_KEYSTORE_PATH],
            capture_output=True,
            timeout=10
        )
        if result.returncode != 0:
            raise Exception(f"Could not read key file: {result.stderr.decode().strip()}")

        return parse_openssh_key(result.stdout)

    except Exception as e:
        raise Exception(f"Failed to extract keys: {e}")


def extract_private_key():
    """
    Extract the Ed25519 expanded private key from the Arti keystore.
    Returns the raw 64-byte private key.
    """
    private_key, _public_key = extract_keys()
    return private_key


def extract_public_key():
    """
    Extract the Ed25519 public key from the Arti keystore.
    Returns the raw 32-byte public key.
    """
    _private_key, public_key = extract_keys()
    return public_key


def write_private_key(private_key, public_key):
    """
    Write a new key pair to the Arti keystore in OpenSSH format.
    Deletes derived keys and restarts the tor container.
    This will change your onion address!
    """
    import tempfile
    import os

    try:
        pem_data = build_openssh_key(private_key, public_key)

        # Write to a temporary file with restricted permissions
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as temp_file:
            temp_path = temp_file.name
            os.chmod(temp_path, 0o600)
            temp_file.write(pem_data)

        try:
            # Ensure keystore directory exists
            result = subprocess.run(
                ["docker", "exec", CONTAINER, "mkdir", "-p",
                 "/var/lib/arti/state/keystore/hss/wordpress"],
                capture_output=True,
                timeout=10
            )

            # Copy file to container
            result = subprocess.run(
                ["docker", "cp", temp_path,
                 f"{CONTAINER}:{ARTI_KEYSTORE_PATH}"],
                capture_output=True,
                timeout=10
            )
            if result.returncode != 0:
                raise Exception(f"Failed to copy key to container: {result.stderr.decode()}")

            # Set proper permissions inside container
            result = subprocess.run(
                ["docker", "exec", CONTAINER, "chmod", "600", ARTI_KEYSTORE_PATH],
                capture_output=True,
                timeout=10
            )
            if result.returncode != 0:
                raise Exception(f"Failed to set key permissions: {result.stderr.decode()}")

            # Delete derived keys so Arti regenerates them for the new identity
            keystore_dir = "/var/lib/arti/state/keystore/hss/wordpress"
            for derived in ["ks_hss_blind_id", "ks_hss_desc_sign", "ks_hss_ipts"]:
                subprocess.run(
                    ["docker", "exec", CONTAINER, "sh", "-c",
                     f"rm -f {keystore_dir}/{derived}*"],
                    capture_output=True,
                    timeout=10
                )

            # Restart tor container to pick up the new key
            subprocess.run(
                ["docker", "restart", CONTAINER],
                capture_output=True,
                timeout=30
            )

            return True

        finally:
            # Securely delete temporary file (multi-pass overwrite)
            if os.path.exists(temp_path):
                file_len = len(pem_data)
                with open(temp_path, "wb") as f:
                    f.write(os.urandom(file_len))
                    f.flush()
                    os.fsync(f.fileno())
                with open(temp_path, "wb") as f:
                    f.write(b"\x00" * file_len)
                    f.flush()
                    os.fsync(f.fileno())
                with open(temp_path, "wb") as f:
                    f.write(b"\xff" * file_len)
                    f.flush()
                    os.fsync(f.fileno())
                os.unlink(temp_path)

    except Exception as e:
        raise Exception(f"Failed to write private key: {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "extract":
        try:
            key_bytes = extract_private_key()
            print(f"Successfully extracted {len(key_bytes)}-byte private key")
            pub_bytes = extract_public_key()
            print(f"Successfully extracted {len(pub_bytes)}-byte public key")
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Usage: key_manager.py extract")
