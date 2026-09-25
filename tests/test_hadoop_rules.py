"""Small semantic checks for the same code shipped to Hadoop reducers."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hadoop_jobs"))

from clean_reducer import clean_dataset  # noqa: E402
from quality import NULL, RULES, score_dataset  # noqa: E402
from service.rules import build_rules  # noqa: E402


class RulesTest(unittest.TestCase):
    def test_custom_threshold_and_title_policy_change_actual_disposition(self) -> None:
        base = datetime(2000, 5, 1, tzinfo=timezone.utc)
        ratings = [f"1::1::4::{int((base + timedelta(days=day)).timestamp())}" for day in range(20)]
        ratings += [f"1::1::3::{int((base + timedelta(days=20)).timestamp())}",
                    f"1::2::4::{int((base + timedelta(days=21)).timestamp())}"]
        raw = {"users": ["1::M::25::1::01234"],
               "movies": ["1::Valid (2000)::Drama", "2::No Year::Drama"],
               "ratings": ratings}
        options = {"min_retained_rating": 4, "missing_title_year_policy": "quarantine",
                   "table_weights": {"ratings": 1, "users": 1, "movies": 1}}
        emitted: list[tuple[str, dict]] = []
        with patch.dict(RULES, build_rules(options), clear=True):
            report = clean_dataset(raw, lambda tag, item: emitted.append((tag, item)), "test-custom")
        self.assertEqual(report["tables"]["ratings"]["output"], 20)
        self.assertEqual(report["tables"]["ratings"]["reasons"]["policy_rating_below_minimum"], 1)
        self.assertEqual(report["tables"]["ratings"]["reasons"]["policy_filtered_movie_reference"], 1)
        self.assertEqual(report["tables"]["movies"]["quarantine"], 1)
        self.assertEqual(report["tables"]["movies"]["reasons"]["policy_missing_title_year"], 1)
        self.assertEqual(len([item for tag, item in emitted if tag == "C" and item["table"] == "ratings"]), 20)

    def test_threshold_cannot_resolve_a_conflicting_rating_event(self) -> None:
        base = datetime(2000, 5, 1, tzinfo=timezone.utc)
        ratings = [f"1::1::4::{int((base + timedelta(days=day)).timestamp())}" for day in range(20)]
        event = int((base + timedelta(days=20)).timestamp())
        ratings += [f"1::1::3::{event}", f"1::1::4::{event}"]
        raw = {"users": ["1::M::25::1::01234"],
               "movies": ["1::Valid (2000)::Drama"], "ratings": ratings}
        options = {"min_retained_rating": 4, "missing_title_year_policy": "flag",
                   "table_weights": {"ratings": 1, "users": 1, "movies": 1}}
        with patch.dict(RULES, build_rules(options), clear=True):
            report = clean_dataset(raw, lambda tag, item: None, "test-conflict")
        self.assertEqual(report["tables"]["ratings"]["output"], 20)
        self.assertEqual(report["tables"]["ratings"]["reasons"]["same_event_conflicting_rating"], 2)
        self.assertEqual(report["tables"]["ratings"]["reasons"].get("policy_rating_below_minimum", 0), 0)

    def test_custom_weights_change_four_dimension_aggregate(self) -> None:
        rows = {"ratings": ["1::1::4::957139200"],
                "users": ["1::M::25::1::01234", "2::X::25::1::01234"],
                "movies": ["1::Valid (2000)::Drama"]}
        options = {"min_retained_rating": 1, "missing_title_year_policy": "flag",
                   "table_weights": {"ratings": 2, "users": 1, "movies": 1}}
        with patch.dict(RULES, build_rules(options), clear=True):
            result = score_dataset(rows, "raw")
        self.assertAlmostEqual(result["overall"]["Accurate"], 87.5)
        self.assertEqual(result["table_weights"], options["table_weights"])

    def test_zero_weight_and_empty_table_do_not_distort_available_score(self) -> None:
        rows = {"ratings": [], "users": ["1::M::25::1::01234"], "movies": []}
        options = {"min_retained_rating": 1, "missing_title_year_policy": "flag",
                   "table_weights": {"ratings": 0, "users": 1, "movies": 0}}
        with patch.dict(RULES, build_rules(options), clear=True):
            result = score_dataset(rows, "raw")
        self.assertEqual(result["overall"]["Accurate"], 100.0)
        self.assertIsNone(result["overall"]["Up-to-date"])

    def test_conflicts_duplicates_and_temporal_split(self) -> None:
        base = datetime(2000, 5, 1, tzinfo=timezone.utc)
        ratings = [
            f"1::1::4::{int((base + timedelta(days=day)).timestamp())}"
            for day in range(20)
        ]
        ratings += [
            ratings[0],
            f"1::1::5::{int((base + timedelta(days=1)).timestamp())}",
            "1::1::6::957139200",
            "2::1::4::957139200",
            "not-a-rating",
        ]
        raw = {
            "users": ["1::M::25::1::01234", "1::F::25::1::01234"],
            "movies": ["1::Alpha (2000)::Drama", "1::Alpha (2000)::Comedy", "1::Alpha (2000):: Drama "],
            "ratings": ratings,
        }
        emitted: list[tuple[str, dict]] = []
        report = clean_dataset(raw, lambda tag, item: emitted.append((tag, item)), "test-task")
        self.assertEqual(report["tables"]["ratings"]["input"], 25)
        self.assertEqual(report["tables"]["ratings"]["contributing"], 19)
        self.assertEqual(report["tables"]["ratings"]["duplicate"], 1)
        self.assertEqual(report["tables"]["ratings"]["quarantine"], 5)
        self.assertEqual(report["tables"]["ratings"]["output"], 19)
        self.assertEqual(sum(report["period_counts"].values()), 19)
        self.assertLess(report["T1"], report["T2"])
        clean = [item for tag, item in emitted if tag == "C"]
        users = next(item for item in clean if item["table"] == "users")
        movies = next(item for item in clean if item["table"] == "movies")
        self.assertEqual(users["fields"][1], NULL)
        self.assertEqual(movies["fields"][2], NULL)
        normalized = [
            item for tag, item in emitted
            if tag == "A" and item["reason"] == "normalized_field"
        ]
        self.assertTrue(any(
            item["details"] == {"field": "genres", "before": " Drama ", "after": "Drama"}
            for item in normalized
        ))

        before = score_dataset(raw, "raw")
        after_rows = {
            table: [item["fields"] for item in clean if item["table"] == table]
            for table in ("ratings", "users", "movies")
        }
        after = score_dataset(after_rows, "clean")
        self.assertEqual(before["tables"]["ratings"]["rows"], 25)
        self.assertEqual(after["tables"]["ratings"]["rows"], 19)
        self.assertEqual(set(before["overall"]), {"Accurate", "Complete", "Unique", "Up-to-date", "Consistent"})
        self.assertEqual(after["tables"]["users"]["metrics"]["Complete"]["numerator"], 4)


if __name__ == "__main__":
    unittest.main()
