import base64
import hashlib
import os
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

salt = b"0123456789abcdef"
digest = hashlib.pbkdf2_hmac("sha256", b"test-password", salt, 100_000, dklen=32)
encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
os.environ.setdefault(
    "MINING_CONTROL_PASSWORD_HASH",
    f"pbkdf2_sha256$100000${encode(salt)}${encode(digest)}",
)
os.environ.setdefault("MINING_CONTROL_TLS", "0")

import control  # noqa: E402


class ControlHelpersTest(unittest.TestCase):
    def test_normalize_mnemonic_requires_exactly_24_words(self):
        phrase = " ".join(["abandon"] * 23 + ["art"])
        self.assertEqual(len(control.normalize_mnemonic(phrase).split()), 24)
        with self.assertRaises(control.MiningControlError):
            control.normalize_mnemonic("one two three")

    def test_keygen_output_parser_returns_public_values(self):
        output = """
Deriving wormhole HD path: m/44'/189189189'/0'/0'/0'
Address: qzodMHCJCWPrwVwCNhdKKKwaheRYj5vuAKQRQbwk9JGE36Pfu
Inner Hash: 0xf90f46696371490042e3e605662fc222d98055f14c12b7fa8fc4f7aaa7c15c77
"""
        address, inner_hash = control.parse_wormhole_keygen_output(output)
        self.assertTrue(address.startswith("qz"))
        self.assertEqual(len(inner_hash), 66)

    def test_parser_rejects_missing_identity(self):
        with self.assertRaises(control.MiningControlError):
            control.parse_wormhole_keygen_output("Address: qz123")

    def test_effective_cpu_count_is_positive(self):
        self.assertGreaterEqual(control.effective_cpu_count(), 1)


if __name__ == "__main__":
    unittest.main()
