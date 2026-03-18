#!/usr/bin/env python3
"""
Bidirectional key format conversion: Arti PEM <-> C Tor onion service keys.

Arti format:
  OpenSSH PEM with key type "ed25519-expanded@spec.torproject.org"
  Contains 64-byte expanded private key + 32-byte public key.

C Tor format:
  hs_ed25519_secret_key:  32-byte header + 64-byte expanded key
  hs_ed25519_public_key:  32-byte header + 32-byte public key
  Header: "== ed25519v1-{type}: type0 ==\x00\x00\x00"

Usage:
  key-convert.py arti-to-ctor <arti_pem_file> <output_dir>
  key-convert.py ctor-to-arti <ctor_secret_key_file> <output_pem_file>
"""

import base64
import os
import struct
import sys

# C Tor key file headers (exactly 32 bytes each)
CTOR_SECRET_HEADER = b"== ed25519v1-secret: type0 ==\x00\x00\x00"
CTOR_PUBLIC_HEADER = b"== ed25519v1-public: type0 ==\x00\x00\x00"

OPENSSH_MAGIC = b"openssh-key-v1\x00"
KEY_TYPE = b"ed25519-expanded@spec.torproject.org"


def _pack(data):
    """Pack bytes with a 4-byte big-endian length prefix."""
    return struct.pack(">I", len(data)) + data


def _unpack_string(data, offset):
    """Unpack a length-prefixed string from data at offset."""
    if offset + 4 > len(data):
        raise ValueError("Truncated data")
    length = struct.unpack(">I", data[offset:offset + 4])[0]
    offset += 4
    if offset + length > len(data):
        raise ValueError("Truncated string data")
    return data[offset:offset + length], offset + length


def parse_arti_pem(pem_path):
    """Parse an Arti OpenSSH PEM file and return (expanded_key, public_key).

    expanded_key: 64 bytes, public_key: 32 bytes.
    """
    with open(pem_path, "r") as f:
        lines = f.readlines()

    # Strip PEM header/footer and decode base64
    b64_lines = []
    in_key = False
    for line in lines:
        line = line.strip()
        if line == "-----BEGIN OPENSSH PRIVATE KEY-----":
            in_key = True
            continue
        if line == "-----END OPENSSH PRIVATE KEY-----":
            break
        if in_key:
            b64_lines.append(line)

    binary = base64.b64decode("".join(b64_lines))

    # Verify magic
    if not binary.startswith(OPENSSH_MAGIC):
        raise ValueError("Not an OpenSSH private key")

    offset = len(OPENSSH_MAGIC)

    # Skip cipher, kdf, kdf options
    _, offset = _unpack_string(binary, offset)  # cipher
    _, offset = _unpack_string(binary, offset)  # kdf
    _, offset = _unpack_string(binary, offset)  # kdf options

    # Number of keys
    num_keys = struct.unpack(">I", binary[offset:offset + 4])[0]
    offset += 4

    # Skip public key blob
    _, offset = _unpack_string(binary, offset)

    # Private key section
    priv_section, _ = _unpack_string(binary, offset)

    # Parse private section: check1, check2, key_type, pub, priv, comment
    p = 0
    check1 = struct.unpack(">I", priv_section[p:p + 4])[0]
    p += 4
    check2 = struct.unpack(">I", priv_section[p:p + 4])[0]
    p += 4
    if check1 != check2:
        raise ValueError("Check values mismatch — corrupted or encrypted key")

    key_type, p = _unpack_string(priv_section, p)
    if key_type != KEY_TYPE:
        raise ValueError(f"Unexpected key type: {key_type}")

    public_key, p = _unpack_string(priv_section, p)
    if len(public_key) != 32:
        raise ValueError(f"Public key wrong size: {len(public_key)}")

    expanded_key, p = _unpack_string(priv_section, p)
    if len(expanded_key) != 64:
        raise ValueError(f"Expanded key wrong size: {len(expanded_key)}")

    return expanded_key, public_key


def build_arti_pem(expanded_key, public_key):
    """Build an Arti-format OpenSSH PEM from expanded key + public key."""
    pub_blob = _pack(KEY_TYPE) + _pack(public_key)

    check = struct.pack(">I", int.from_bytes(os.urandom(4), "big"))
    priv_blob = (check + check +
                 _pack(KEY_TYPE) + _pack(public_key) +
                 _pack(expanded_key) + _pack(b""))

    # Pad to 8-byte boundary
    pad_len = (8 - len(priv_blob) % 8) % 8
    priv_blob += bytes(range(1, pad_len + 1))

    binary = (OPENSSH_MAGIC +
              _pack(b"none") + _pack(b"none") + _pack(b"") +
              struct.pack(">I", 1) +
              _pack(pub_blob) + _pack(priv_blob))

    b64 = base64.b64encode(binary).decode("ascii")
    lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
    return ("-----BEGIN OPENSSH PRIVATE KEY-----\n" +
            "\n".join(lines) + "\n" +
            "-----END OPENSSH PRIVATE KEY-----\n")


def arti_to_ctor(pem_path, output_dir):
    """Convert Arti PEM to C Tor hs_ed25519_secret_key + hs_ed25519_public_key."""
    expanded_key, public_key = parse_arti_pem(pem_path)

    os.makedirs(output_dir, exist_ok=True)

    secret_path = os.path.join(output_dir, "hs_ed25519_secret_key")
    with open(secret_path, "wb") as f:
        f.write(CTOR_SECRET_HEADER + expanded_key)
    os.chmod(secret_path, 0o600)

    public_path = os.path.join(output_dir, "hs_ed25519_public_key")
    with open(public_path, "wb") as f:
        f.write(CTOR_PUBLIC_HEADER + public_key)
    os.chmod(public_path, 0o600)

    print(f"Wrote {secret_path} ({32 + 64} bytes)")
    print(f"Wrote {public_path} ({32 + 32} bytes)")


def ctor_to_arti(secret_key_path, output_pem_path):
    """Convert C Tor hs_ed25519_secret_key to Arti PEM.

    Also reads hs_ed25519_public_key from the same directory if available,
    otherwise derives the public key from the expanded key's first 32 bytes.
    """
    with open(secret_key_path, "rb") as f:
        data = f.read()

    if len(data) != 96:
        raise ValueError(f"Secret key wrong size: {len(data)} (expected 96)")
    if data[:32] != CTOR_SECRET_HEADER:
        raise ValueError("Invalid C Tor secret key header")

    expanded_key = data[32:]

    # Try to read public key from same directory
    secret_dir = os.path.dirname(secret_key_path)
    public_path = os.path.join(secret_dir, "hs_ed25519_public_key")
    if os.path.exists(public_path):
        with open(public_path, "rb") as f:
            pub_data = f.read()
        if len(pub_data) == 64 and pub_data[:32] == CTOR_PUBLIC_HEADER:
            public_key = pub_data[32:]
        else:
            raise ValueError("Invalid C Tor public key file")
    else:
        # Derive public key — the first 32 bytes of expanded key are the
        # clamped scalar; we need to do scalar multiplication to get the
        # public point. For simplicity, require the public key file.
        raise ValueError(
            f"hs_ed25519_public_key not found in {secret_dir} — "
            "C Tor to Arti conversion requires both key files"
        )

    pem = build_arti_pem(expanded_key, public_key)

    with open(output_pem_path, "w") as f:
        f.write(pem)

    print(f"Wrote {output_pem_path}")


def pem_to_ed25519_base64(pem_path):
    """Extract raw 64-byte expanded ed25519 key from Arti PEM, output as base64.

    This is the format C Tor's ADD_ONION command expects:
        ADD_ONION ED25519-V3:<base64_key> ...
    """
    expanded_key, _ = parse_arti_pem(pem_path)
    print(base64.b64encode(expanded_key).decode("ascii"))


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "arti-to-ctor":
        if len(sys.argv) != 4:
            print("Usage: key-convert.py arti-to-ctor <pem_file> <output_dir>",
                  file=sys.stderr)
            sys.exit(1)
        arti_to_ctor(sys.argv[2], sys.argv[3])
    elif cmd == "ctor-to-arti":
        if len(sys.argv) != 4:
            print("Usage: key-convert.py ctor-to-arti <secret_key_file> <output_pem>",
                  file=sys.stderr)
            sys.exit(1)
        ctor_to_arti(sys.argv[2], sys.argv[3])
    elif cmd == "pem-to-ed25519-base64":
        if len(sys.argv) != 3:
            print("Usage: key-convert.py pem-to-ed25519-base64 <pem_file>",
                  file=sys.stderr)
            sys.exit(1)
        pem_to_ed25519_base64(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
