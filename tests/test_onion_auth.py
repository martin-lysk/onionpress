"""Tests for onion_auth — Ed25519 signatures for OnionHeaven API."""

import hashlib
import os
import sys
import unittest

# Allow importing from src/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import onion_auth


def _expand_seed(seed_hex):
    """Derive expanded key + public key from an ed25519 seed (for test vectors)."""
    seed = bytes.fromhex(seed_hex)
    h = hashlib.sha512(seed).digest()
    a_bytes = bytearray(h[:32])
    # Clamp
    a_bytes[0] &= 248
    a_bytes[31] &= 127
    a_bytes[31] |= 64
    prefix = h[32:]
    expanded = bytes(a_bytes) + prefix
    # Derive public key: A = a * B
    a = int.from_bytes(a_bytes, 'little')
    A = onion_auth._scalar_mult(a, onion_auth._B)
    pub = onion_auth._encode_point(A)
    return expanded, pub


class TestEddsaVerify(unittest.TestCase):
    """Verify against RFC 8032 §7.1 test vectors (verify-only)."""

    def test_rfc8032_vector1_empty_message(self):
        pub = bytes.fromhex(
            "d75a980182b10ab7d54bfed3c964073a"
            "0ee172f3daa62325af021a68f707511a"
        )
        sig = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a"
            "84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46b"
            "d25bf5f0595bbe24655141438e7a100b"
        )
        self.assertTrue(onion_auth.verify(pub, b"", sig))

    def test_rfc8032_vector2_one_byte(self):
        pub = bytes.fromhex(
            "3d4017c3e843895a92b70aa74d1b7ebc"
            "9c982ccf2ec4968cc0cd55f12af4660c"
        )
        sig = bytes.fromhex(
            "92a009a9f0d4cab8720e820b5f642540"
            "a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c"
            "387b2eaeb4302aeeb00d291612bb0c00"
        )
        msg = bytes.fromhex("72")
        self.assertTrue(onion_auth.verify(pub, msg, sig))

    def test_bad_signature_rejected(self):
        pub = bytes.fromhex(
            "d75a980182b10ab7d54bfed3c964073a"
            "0ee172f3daa3f4a18446b0b8d183f8e3"
        )
        sig = bytes(64)  # all zeros
        self.assertFalse(onion_auth.verify(pub, b"", sig))


class TestExpandedKeySignVerify(unittest.TestCase):
    """Sign with expanded keys derived from RFC 8032 seeds, then verify."""

    def test_vector1_sign_verify(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        expanded, pub = _expand_seed(seed)
        sig = onion_auth.sign_expanded(expanded, pub, b"")
        self.assertTrue(onion_auth.verify(pub, b"", sig))

    def test_vector1_matches_nacl_signature(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        expected_sig = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a"
            "84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46b"
            "d25bf5f0595bbe24655141438e7a100b"
        )
        expanded, pub = _expand_seed(seed)
        sig = onion_auth.sign_expanded(expanded, pub, b"")
        self.assertEqual(sig, expected_sig)

    def test_vector2_sign_verify(self):
        seed = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
        expanded, pub = _expand_seed(seed)
        msg = bytes.fromhex("72")
        sig = onion_auth.sign_expanded(expanded, pub, msg)
        self.assertTrue(onion_auth.verify(pub, msg, sig))

    def test_vector2_matches_nacl_signature(self):
        seed = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
        expected_sig = bytes.fromhex(
            "92a009a9f0d4cab8720e820b5f642540"
            "a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c"
            "387b2eaeb4302aeeb00d291612bb0c00"
        )
        expanded, pub = _expand_seed(seed)
        msg = bytes.fromhex("72")
        sig = onion_auth.sign_expanded(expanded, pub, msg)
        self.assertEqual(sig, expected_sig)

    def test_wrong_message_rejected(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        expanded, pub = _expand_seed(seed)
        sig = onion_auth.sign_expanded(expanded, pub, b"hello")
        self.assertFalse(onion_auth.verify(pub, b"world", sig))

    def test_random_key_roundtrip(self):
        """Generate a random expanded key, sign, verify."""
        h = hashlib.sha512(os.urandom(32)).digest()
        a_bytes = bytearray(h[:32])
        a_bytes[0] &= 248
        a_bytes[31] &= 127
        a_bytes[31] |= 64
        expanded = bytes(a_bytes) + h[32:]
        a = int.from_bytes(a_bytes, 'little')
        pub = onion_auth._encode_point(onion_auth._scalar_mult(a, onion_auth._B))
        msg = b"test message for ed25519"
        sig = onion_auth.sign_expanded(expanded, pub, msg)
        self.assertTrue(onion_auth.verify(pub, msg, sig))
        self.assertFalse(onion_auth.verify(pub, b"tampered", sig))


class TestOnionAddress(unittest.TestCase):
    """Test onion address encode / decode round-trip."""

    def test_derive_then_decode_roundtrip(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        _, pub = _expand_seed(seed)
        address = onion_auth.derive_onion_address(pub)
        self.assertTrue(address.endswith('.onion'))
        self.assertEqual(len(address), 62)  # 56 chars + ".onion"
        decoded = onion_auth.decode_onion_address(address)
        self.assertEqual(decoded, pub)

    def test_decode_known_address(self):
        """OnionHeaven address decodes without error."""
        addr = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
        pub = onion_auth.decode_onion_address(addr)
        self.assertEqual(len(pub), 32)
        # Re-derive address to verify
        self.assertEqual(onion_auth.derive_onion_address(pub), addr)

    def test_bad_checksum_rejected(self):
        addr = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa2a.onion"
        with self.assertRaises(ValueError):
            onion_auth.decode_onion_address(addr)


class TestPayloadSignVerify(unittest.TestCase):
    """Test the high-level payload sign/verify helpers."""

    def test_roundtrip(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        expanded, pub = _expand_seed(seed)
        content_addr = onion_auth.derive_onion_address(pub)
        hc_addr = "abc" * 18 + "aa.onion"  # dummy, won't be decoded

        ts = onion_auth.make_timestamp()
        sig_b64 = onion_auth.sign_payload(
            expanded, pub, "register", content_addr, hc_addr, ts
        )

        ok, err = onion_auth.verify_payload(
            content_addr, "register", hc_addr, ts, sig_b64
        )
        self.assertTrue(ok, f"Verification failed: {err}")

    def test_wrong_endpoint_rejected(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        expanded, pub = _expand_seed(seed)
        content_addr = onion_auth.derive_onion_address(pub)

        ts = onion_auth.make_timestamp()
        sig_b64 = onion_auth.sign_payload(
            expanded, pub, "register", content_addr, "", ts
        )

        ok, _ = onion_auth.verify_payload(
            content_addr, "unregister", "", ts, sig_b64
        )
        self.assertFalse(ok)

    def test_expired_timestamp_rejected(self):
        seed = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        expanded, pub = _expand_seed(seed)
        content_addr = onion_auth.derive_onion_address(pub)

        ts = "2020-01-01T00:00:00Z"  # far in the past
        sig_b64 = onion_auth.sign_payload(
            expanded, pub, "online", content_addr, "", ts
        )

        ok, err = onion_auth.verify_payload(
            content_addr, "online", "", ts, sig_b64
        )
        self.assertFalse(ok)
        self.assertIn("expired", err.lower())

    def test_empty_healthcheck_address(self):
        seed = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
        expanded, pub = _expand_seed(seed)
        content_addr = onion_auth.derive_onion_address(pub)

        ts = onion_auth.make_timestamp()
        sig_b64 = onion_auth.sign_payload(
            expanded, pub, "unregister", content_addr, "", ts
        )

        ok, err = onion_auth.verify_payload(
            content_addr, "unregister", "", ts, sig_b64
        )
        self.assertTrue(ok, f"Verification failed: {err}")


if __name__ == "__main__":
    unittest.main()
