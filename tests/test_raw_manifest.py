"""The checked-out raw files must retain the registered byte versions."""

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class RawManifestTest(unittest.TestCase):
    def test_checked_out_data_matches_registered_bytes(self) -> None:
        manifest = json.loads((ROOT / "raw_data_manifest.json").read_text(encoding="utf-8"))
        for name, entry in manifest["files"].items():
            with self.subTest(name=name):
                data = (ROOT / entry["path"]).read_bytes()
                self.assertEqual(len(data), entry["size_bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest().upper(), entry["sha256"])
