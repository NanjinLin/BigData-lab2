"""Versioned profile rules for a user-requested Hadoop run."""

from __future__ import annotations

import json
import hashlib
import shutil
import uuid
from unittest.mock import patch
import unittest
from pathlib import Path

from service.rules import RuleConfigError, build_rules, snapshot_bytes, validate_rules
import service.rules as rules_module
from scripts.run_iteration1 import Pipeline, verify_rules_file


ROOT = Path(__file__).resolve().parent.parent
BASE = json.loads((ROOT / "config/quality_rules_v1.json").read_text(encoding="utf-8"))


class RuleConfigTests(unittest.TestCase):
    def test_default_uses_registered_bytes_and_version(self) -> None:
        rules = build_rules(None)
        self.assertEqual(rules, BASE)
        self.assertEqual(snapshot_bytes(rules), (ROOT / "config/quality_rules_v1.json").read_bytes())

    def test_custom_options_have_stable_derived_version_and_round_trip(self) -> None:
        options = {"min_retained_rating": 4, "missing_title_year_policy": "quarantine",
                   "table_weights": {"ratings": 5, "users": 3, "movies": 2}}
        first = build_rules(options)
        second = build_rules({"table_weights": {"movies": 2, "users": 3, "ratings": 5},
                              "missing_title_year_policy": "quarantine", "min_retained_rating": 4})
        self.assertEqual(first, second)
        self.assertTrue(first["rule_version"].startswith("quality-clean-v1.0.0+"))
        self.assertEqual(first["processing_policy"]["min_retained_rating"], 4)
        self.assertEqual(first["table_weights"]["movies"], 2)
        self.assertEqual(validate_rules(json.loads(snapshot_bytes(first))), first)

    def test_derived_version_changes_when_cleaning_implementation_changes(self) -> None:
        options = {"min_retained_rating": 4, "missing_title_year_policy": "flag",
                   "table_weights": {"ratings": 1, "users": 1, "movies": 1}}
        with patch.object(rules_module, "_implementation_fingerprint", return_value="code-a"):
            before = build_rules(options)["rule_version"]
        with patch.object(rules_module, "_implementation_fingerprint", return_value="code-b"):
            after = build_rules(options)["rule_version"]
        self.assertNotEqual(before, after)

    def test_nonnegative_integer_weights_have_no_arbitrary_maximum(self) -> None:
        options = {"min_retained_rating": 1, "missing_title_year_policy": "flag",
                   "table_weights": {"ratings": 101, "users": 1, "movies": 1}}
        rules = build_rules(options)
        self.assertEqual(rules["table_weights"], options["table_weights"])
        self.assertEqual(validate_rules(rules), rules)

    def test_invalid_options_and_modified_snapshot_are_rejected(self) -> None:
        valid = {"min_retained_rating": 4, "missing_title_year_policy": "flag",
                 "table_weights": {"ratings": 1, "users": 1, "movies": 1}}
        for changed in (
            {**valid, "min_retained_rating": 6},
            {**valid, "min_retained_rating": True},
            {**valid, "table_weights": {"ratings": 0, "users": 0, "movies": 0}},
            {**valid, "table_weights": {"ratings": 1, "users": 1, "movies": 1, "extra": 1}},
            {**valid, "extra": "ignore-me"},
        ):
            with self.subTest(changed=changed), self.assertRaises(RuleConfigError):
                build_rules(changed)
        altered = build_rules(valid)
        altered["allowed_values"]["rating"] = [5]
        with self.assertRaises(RuleConfigError):
            validate_rules(altered)

    def test_driver_rejects_modified_rule_snapshot(self) -> None:
        options = {"min_retained_rating": 4, "missing_title_year_policy": "flag",
                   "table_weights": {"ratings": 2, "users": 1, "movies": 1}}
        data = snapshot_bytes(build_rules(options))
        directory = ROOT / f".test-rule-snapshot-{uuid.uuid4().hex}"
        directory.mkdir()
        try:
            path = directory / "rules.json"
            path.write_bytes(data)
            expected_hash = hashlib.sha256(data).hexdigest()
            self.assertEqual(verify_rules_file(path, expected_hash)["processing_policy"]["min_retained_rating"], 4)
            path.write_bytes(data + b" ")
            with self.assertRaisesRegex(RuntimeError, "hash"):
                verify_rules_file(path, expected_hash)
        finally:
            shutil.rmtree(directory)

    def test_pipeline_freezes_snapshot_and_rejects_source_mutation(self) -> None:
        options = {"min_retained_rating": 4, "missing_title_year_policy": "flag",
                   "table_weights": {"ratings": 2, "users": 1, "movies": 1}}
        rules = build_rules(options)
        data = snapshot_bytes(rules)
        digest = hashlib.sha256(data).hexdigest()
        task_id = f"freeze-test-{uuid.uuid4().hex}"
        source = ROOT / "runs" / f"{task_id}.rules.json"
        run_dir = ROOT / "runs" / task_id
        try:
            source.write_bytes(data)
            pipeline = Pipeline(task_id, {}, rules, source, digest)
            self.assertEqual(pipeline.rule_path.read_bytes(), data)
            source.write_bytes(data + b" ")
            self.assertEqual(pipeline.rule_path.read_bytes(), data)
            with self.assertRaisesRegex(RuntimeError, "hash"):
                pipeline.assert_snapshot_unchanged()
        finally:
            source.unlink(missing_ok=True)
            for name in ("status.json", "rules.json"):
                (run_dir / name).unlink(missing_ok=True)
            if run_dir.is_dir():
                run_dir.rmdir()


if __name__ == "__main__":
    unittest.main()
