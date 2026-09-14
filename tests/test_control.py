import base64
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def test_running_miner_is_syncing_before_hash_rate_is_available(self):
        state = control.classify_mining_state([], False, True, True)
        self.assertEqual(state, "syncing")

    def test_stopped_miner_remains_offline(self):
        state = control.classify_mining_state([], False, False, False)
        self.assertEqual(state, "offline")

    def test_launch_state_round_trip_is_private(self):
        address = "qzodMHCJCWPrwVwCNhdKKKwaheRYj5vuAKQRQbwk9JGE36Pfu"
        config = {
            "enabled": True,
            "inner_hash": "0x" + ("ab" * 32),
            "address": address,
            "node_name": "quantus-miner",
            "cpu_workers": 8,
            "gpu_devices": 8,
            "wallet_index": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            old_data_dir = control.DATA_DIR
            old_state_file = control.LAUNCH_STATE_FILE
            control.DATA_DIR = Path(directory)
            control.LAUNCH_STATE_FILE = Path(directory) / "launch.json"
            try:
                control.persist_launch_state(config)
                loaded = control.load_launch_state()
            finally:
                control.DATA_DIR = old_data_dir
                control.LAUNCH_STATE_FILE = old_state_file
        self.assertEqual(loaded, config)
        self.assertNotIn("mnemonic", loaded)

    def test_heartbeat_accepts_active_miner_without_tcp_probe(self):
        instance = control.MiningControl()
        instance.service_started_at = 0
        config = {
            "enabled": True,
            "inner_hash": "0x" + ("ab" * 32),
            "address": "qzodMHCJCWPrwVwCNhdKKKwaheRYj5vuAKQRQbwk9JGE36Pfu",
            "node_name": "quantus-miner",
            "cpu_workers": 8,
            "gpu_devices": 8,
            "wallet_index": 0,
        }
        node = (
            101,
            [
                "/root/quantus-node",
                "--name",
                "quantus-miner",
                "--rewards-inner-hash",
                config["inner_hash"],
            ],
        )
        miner = (
            202,
            [
                "/root/quantus-miner",
                "serve",
                "--cpu-workers",
                "8",
                "--gpu-devices",
                "8",
            ],
        )
        with (
            patch.object(control, "find_process", side_effect=[node, node, miner]),
            patch.object(instance, "_auth_supported", return_value=False),
            patch.object(
                control,
                "read_http",
                return_value=b"miner_active_jobs 1\nminer_hash_rate 100\n",
            ),
            patch.object(control, "rpc_call", return_value={"isSyncing": False}),
        ):
            healthy, message = instance._heartbeat_probe(config)
        self.assertTrue(healthy)
        self.assertIn("metrics", message)


if __name__ == "__main__":
    unittest.main()
