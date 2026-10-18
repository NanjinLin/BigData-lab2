"""Hand-computable fixtures for joins, counts, time boundaries and evidence."""

import hashlib
import json
import shutil
import unittest
import uuid
from pathlib import Path

from service.tasks import TaskError, TaskService


ROOT = Path(__file__).resolve().parent.parent


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.project = ROOT / f".test-analysis-{uuid.uuid4().hex}"
        self.directory = self.project / "runs" / "fixture"
        self.directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.project))
        self.tasks = TaskService(self.project)
        self.records = [
            ("ratings", ["1", "1", "5", "1"]), ("ratings", ["1", "1", "3", "2"]),
            ("ratings", ["2", "2", "1", "3"]), ("ratings", ["3", "3", "4", "4"]),
            ("ratings", ["4", "4", "2", "5"]),
            ("movies", ["1", "First (2000)", "Action|Drama"]),
            ("movies", ["2", "Second (2000)", "Drama"]),
            ("movies", ["3", "Third (2000)", "Comedy"]),
            ("movies", ["4", "Fourth (2000)", "\\N"]),
            *[("users", [str(i), "M", "25", "0", "10000"]) for i in range(1, 5)],
        ]
        self.write_fixture()

    def write_fixture(self, minimum=1):
        content = "".join("C\t" + json.dumps({"kind": "clean", "table": table, "fields": fields}) + "\n"
                          for table, fields in self.records).encode()
        (self.directory / "cleaned.tsv").write_bytes(content)
        self.report = {"task_id": "fixture", "status": "completed", "rule_version": "quality-clean-v1.0.0",
                       "clean_data_version": "clean-fixture", "clean_data_sha256": hashlib.sha256(content).hexdigest(),
                       "rule_parameters": {"min_retained_rating": minimum},
                       "T1": "1970-01-01T00:00:02Z", "T2": "1970-01-01T00:00:04Z",
                       "cleaning": {"tables": {table: {"output": sum(t == table for t, _ in self.records)}
                                               for table in ("ratings", "movies", "users")}}}
        self.save_report()

    def save_report(self):
        (self.directory / "report.json").write_text(json.dumps(self.report), encoding="utf-8")

    def test_movie_mean_distribution_distinct_users_and_cache(self):
        result = self.tasks.analyze("fixture", {"group_by": "movie"})
        first = result["rows"][0]
        self.assertEqual((first["key"], first["mean_rating"], first["rating_count"], first["user_count"]), ("1", 4, 2, 1))
        self.assertEqual(first["distribution"], {"1": 0, "2": 0, "3": 1, "4": 0, "5": 1})
        self.assertFalse(result["reused"])
        repeated = self.tasks.analyze("fixture", {"group_by": "movie"})
        self.assertTrue(repeated["reused"])
        self.assertEqual(result["analysis_id"], repeated["analysis_id"])
        self.assertEqual(self.tasks.analysis_result("fixture", result["analysis_id"])["rows"], result["rows"])
        self.assertTrue(all(check["passed"] for check in result["validation"]["checks"]))

    def test_genre_join_multilabel_and_missing_metadata_are_explicit(self):
        result = self.tasks.analyze("fixture", {})
        by_name = {row["key"]: row for row in result["rows"]}
        self.assertEqual(by_name["Drama"]["mean_rating"], 3)
        self.assertEqual(by_name["Drama"]["user_count"], 2)
        self.assertEqual(result["coverage"]["matched_ratings"], 4)
        self.assertEqual(result["coverage"]["missing_genre_ratings"], 1)
        self.assertEqual(sum(row["rating_count"] for row in result["rows"]), 6)
        self.assertIn("每个类型", result["method"]["genre_counting"])
        self.assertEqual(result["validation"]["status"], "limited")

    def test_filters_minimum_and_empty_results(self):
        result = self.tasks.analyze("fixture", {"group_by": "movie", "movie_query": "FIRST", "genre": "Drama", "min_ratings": 2})
        self.assertEqual([row["key"] for row in result["rows"]], ["1"])
        self.assertEqual(result["coverage"]["filtered_ratings"], 3)
        empty = self.tasks.analyze("fixture", {"min_ratings": 10})
        self.assertEqual(empty["rows"], [])
        self.assertEqual(empty["validation"]["status"], "insufficient")

    def test_time_boundaries_use_report_cutoffs(self):
        for period, expected in (("train", 2), ("validation", 2), ("test", 1)):
            result = self.tasks.analyze("fixture", {"group_by": "movie", "period": period})
            self.assertEqual(result["coverage"]["matched_ratings"], expected)

    def test_tampered_or_missing_table_never_falls_back_to_report_samples(self):
        self.assertTrue(self.tasks.list_tasks()[0]["clean_table_available"])
        with (self.directory / "cleaned.tsv").open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaises(TaskError):
            self.tasks.analyze("fixture", {})
        (self.directory / "cleaned.tsv").unlink()
        self.assertFalse(self.tasks.list_tasks()[0]["clean_table_available"])
        with self.assertRaisesRegex(TaskError, "完整净表"):
            self.tasks.analyze("fixture", {})

    def test_policy_bias_invalid_parameters_and_report_counts(self):
        self.write_fixture(minimum=3)
        result = self.tasks.analyze("fixture", {})
        self.assertTrue(any("过滤部分合法低分" in warning for warning in result["validation"]["warnings"]))
        for options in ({"limit": 0}, {"min_ratings": True}, {"sort_by": "random"}, {"sql": "anything"}, {"genre": "unknown"}):
            with self.subTest(options=options), self.assertRaises(TaskError):
                self.tasks.analyze("fixture", options)
        self.report["cleaning"]["tables"]["ratings"]["output"] += 1
        self.save_report()
        with self.assertRaisesRegex(TaskError, "行数"):
            self.tasks.analyze("fixture", {})

    def test_report_changes_invalidate_cache_and_update_disclosed_source(self):
        first = self.tasks.analyze("fixture", {})
        self.report["cleaning"]["tables"]["ratings"].update(input=10, quarantine=5)
        self.save_report()
        updated = self.tasks.analyze("fixture", {})
        self.assertFalse(updated["reused"])
        self.assertNotEqual(first["analysis_id"], updated["analysis_id"])
        self.assertNotEqual(first["source"]["report_content_sha256"], updated["source"]["report_content_sha256"])
        self.assertEqual(updated["source"]["rating_disposition"]["quarantine"], 5)

    def test_chart_distribution_counts_each_rating_once_including_untyped_movies(self):
        result = self.tasks.chart_data("fixture", "distribution")
        self.assertEqual(result["rating_distribution"], {str(i): 1 for i in range(1, 6)})
        self.assertEqual(sum(result["rating_distribution"].values()), result["coverage"]["matched_ratings"])
        genres = self.tasks.chart_data("fixture", "genre")
        self.assertEqual(sum(genres["rating_distribution"].values()), 4)
        self.assertEqual(sum(row["rating_count"] for row in genres["rows"]), 6)

    def test_scatter_includes_all_movies_without_changing_agent_ranking_limit(self):
        self.records = [("ratings", ["1", str(i), str(i % 5 + 1), str(i)]) for i in range(1, 71)]
        self.records += [("movies", [str(i), f"Movie {i} (2000)", "Drama"]) for i in range(1, 71)]
        self.records += [("users", ["1", "M", "25", "0", "10000"])]
        self.write_fixture()
        self.assertEqual(len(self.tasks.analyze("fixture", {"group_by": "movie", "limit": 50})["rows"]), 50)
        chart = self.tasks.chart_data("fixture", "scatter")
        self.assertEqual(len(chart["rows"]), 70)
        self.assertEqual(chart["groups"]["returned"], 70)
        self.assertIsNone(chart["query"]["limit"])
        self.assertEqual(sum(chart["rating_distribution"].values()), 70)
        with self.assertRaises(TaskError):
            self.tasks.analyze("fixture", {"limit": None})

    def test_chart_errors_are_explicit_and_do_not_create_hadoop_tasks(self):
        with self.assertRaisesRegex(TaskError, "图表类型"):
            self.tasks.chart_data("fixture", "unknown")
        (self.directory / "cleaned.tsv").unlink()
        with self.assertRaisesRegex(TaskError, "完整净表"):
            self.tasks.chart_data("fixture", "scatter")
        self.assertFalse(list(self.project.rglob("*.rules.json")))


if __name__ == "__main__":
    unittest.main()
