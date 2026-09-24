"""Small semantic checks for the same code shipped to Hadoop reducers."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hadoop_jobs"))

from clean_reducer import clean_dataset  # noqa: E402
from quality import NULL, score_dataset  # noqa: E402


class RulesTest(unittest.TestCase):
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
