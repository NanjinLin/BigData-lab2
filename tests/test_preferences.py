"""Real fitted models, temporal isolation, version checks and complete API flow."""
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

from service.app import create_server
from service.preferences import (PreferenceError, PreferenceService, artifact, build_profiles,
                                 options, train, training_report, _quality)
from service.tasks import TaskService


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.task_id = "preferences-test"
        self.directory = self.project / "runs" / self.task_id
        self.directory.mkdir(parents=True)
        records = []
        for uid in range(1, 27):
            records.append(("users", [str(uid), "F", "25", "4", "01001"]))
        for mid, genre in ((1, "Action"), (2, "Drama"), (3, "Comedy"), (4, "Action|Comedy")):
            records.append(("movies", [str(mid), f"Film {mid} (1999)", genre]))
        for uid in range(1, 25):
            for t in range(1, 25):
                mid = ((uid // 8) % 3 + 1) if t % 4 else (t % 3 + 1)
                rating = 5 if (uid + t) % 3 else 2
                records.append(("ratings", list(map(str, (uid, mid, rating, t)))))
            for t in (50, 70):
                records.append(("ratings", list(map(str, (uid, 4, 5, t)))))
        # User25 has only validation history, user26 has only two train ratings.
        records += [("ratings", ["25", "1", "5", "50"]), ("ratings", ["26", "1", "3", "1"]),
                    ("ratings", ["26", "2", "4", "2"])]
        content = "".join("C\t" + json.dumps({"table": t, "fields": f}) + "\n" for t, f in records).encode()
        (self.directory / "cleaned.tsv").write_bytes(content)
        self.report = {"task_id": self.task_id, "status": "completed", "rule_version": "fixture-v1",
                       "clean_data_version": "fixture-clean-v1", "clean_data_sha256": hashlib.sha256(content).hexdigest(),
                       "T1": "1970-01-01T00:00:40Z", "T2": "1970-01-01T00:01:00Z",
                       "period_counts": {"train": 578, "validation": 25, "test": 24},
                       "cleaning": {"tables": {t: {"output": sum(k == t for k, _ in records)} for t in ("users", "movies", "ratings")}}}
        (self.directory / "report.json").write_text(json.dumps(self.report), encoding="utf-8")
        self.config = {"n_clusters": 3, "min_samples": 2, "eps": 2.0}

    def fitted(self):
        return train(self.directory, self.report, self.config)

    def test_all_four_algorithms_persist_actual_artifacts(self):
        result = self.fitted()
        self.assertEqual(result["eligible_users"], 24)
        self.assertEqual(result["excluded_users"], 2)
        self.assertEqual(set(result["clusterings"]), {"kmeans", "dbscan"})
        self.assertEqual(set(result["reductions"]), {"pca", "truncated_svd"})
        for name, registered in result["artifacts"].items():
            path = self.directory / "preferences" / result["experiment_id"] / name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), registered["sha256"])
        for algorithm, item in result["reductions"].items():
            data = artifact(self.directory, result, algorithm + ".json")
            self.assertEqual(len(data["users"]), 24)
            self.assertTrue(all(len(u["coordinates"]) == 2 for u in data["users"]))
            self.assertTrue(0 <= item["retained_variance"] <= 1.0000001)
            self.assertGreater(item["input_matrix_bytes"], item["coordinate_bytes"])
            self.assertEqual(set(item["comparisons"]), {"kmeans", "dbscan"})
            self.assertTrue(np.isfinite(item["reconstruction_mse"]))
        service = PreferenceService(TaskService(self.project))
        answer = service.query(self.task_id, "cluster", "kmeans", [1, 25, 26, 999])
        self.assertIn("cluster", answer["users"][0])
        self.assertEqual([u.get("status") for u in answer["users"][1:]],
                         ["insufficient_history", "insufficient_history", "unknown_user"])
        reduced = service.query(self.task_id, "reduce", "pca", [1], comparison_algorithm="kmeans")
        self.assertEqual(set(reduced["model"]["comparisons"]), {"kmeans"})
        self.assertEqual(set(reduced["original_clusterings"]), {"kmeans"})
        again = self.fitted()
        self.assertNotEqual(again["experiment_id"], result["experiment_id"])
        self.assertTrue((self.directory / "preferences" / result["experiment_id"] / "report.json").is_file())

    def test_future_ratings_do_not_change_profiles(self):
        users = {1: (), 2: (), 3: ()}
        movies = {1: ("First", ("Drama",), 1999), 2: ("Second", ("Comedy",), 1999)}
        rows = [(1, 1, 5, 10), (1, 2, 1, 20), (2, 2, 4, 30), (3, 1, 2, 50)]
        first = build_profiles(rows, users, movies, 40, 1)
        changed = [(uid, mid, 1 if rating == 5 else 5, stamp) if stamp > 40 else (uid, mid, rating, stamp)
                   for uid, mid, rating, stamp in rows]
        changed.extend([(1, 2, 5, 60), (3, 1, 5, 90)])
        second = build_profiles(changed, users, movies, 40, 1)
        self.assertEqual(first[0], [1, 2])
        np.testing.assert_array_equal(first[1], second[1])
        self.assertEqual(first[2], second[2])
        self.assertEqual(first[3], second[3])

    def test_unavailable_metrics_and_all_noise_are_explicit(self):
        x = np.arange(20).reshape(10, 2)
        for labels in (np.full(10, -1), np.zeros(10), np.arange(10)):
            quality = _quality(x, labels)
            self.assertIsNone(quality["silhouette"])
            self.assertIsNone(quality["calinski_harabasz"])
            self.assertIsNone(quality["davies_bouldin"])
            self.assertIn("unavailable_reason", quality)

    def test_invalid_parameters_versions_hashes_and_missing_data_rejected(self):
        for config in ({"n_clusters": True}, {"eps": float("nan")}, {"retention": 0},
                       {"dimensions": 100}, {"random_state": 7}, {"min_history": -1}):
            with self.assertRaises(PreferenceError):
                options(config)
        result = self.fitted()
        service = PreferenceService(TaskService(self.project))
        with self.assertRaisesRegex(PreferenceError, "参数与请求不同"):
            service.query(self.task_id, "cluster", "kmeans", [1], {"n_clusters": 4})
        for ids in ([1, 1], [True], [0], "1", [{}]):
            with self.assertRaises(PreferenceError):
                service.query(self.task_id, "cluster", "kmeans", ids)
        for key, value in (("clean_data_version", "other"), ("T1", "1970-01-01T00:00:41Z"), ("rule_version", "other")):
            with self.assertRaisesRegex(PreferenceError, "版本不一致"):
                training_report(self.directory, {**self.report, key: value})
        path = self.directory / "preferences" / result["experiment_id"] / "pca.json"
        path.write_text("{}")
        with self.assertRaisesRegex(PreferenceError, "校验失败"):
            service.query(self.task_id, "reduce", "pca", [1])
        with self.assertRaises(PreferenceError):
            artifact(self.directory, result, "../report.json")
        (self.directory / "cleaned.tsv").write_bytes(b"changed")
        with self.assertRaisesRegex(PreferenceError, "SHA-256"):
            training_report(self.directory, self.report)

    def test_retention_threshold_and_single_dimension(self):
        saved = train(self.directory, self.report, {**self.config, "retention": .8})
        for item in saved["reductions"].values():
            self.assertGreaterEqual(item["retained_variance"] + 1e-8, .8)
        saved = train(self.directory, self.report, {**self.config, "dimensions": 1})
        self.assertTrue(all(item["actual_dimensions"] == 1 for item in saved["reductions"].values()))

    def test_http_async_status_query_download_and_static_assets(self):
        server = create_server(self.project, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        prefix = f"/api/tasks/{self.task_id}/preferences"
        def http(path, method="GET", body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = Request(base + path, data=data, headers={"Content-Type": "application/json"}, method=method)
            return urlopen(req, timeout=20)
        with http(prefix + "/status") as response:
            self.assertEqual(json.load(response)["state"], "not_started")
        with http(prefix + "/train", "POST", self.config) as response:
            self.assertEqual(response.status, 202)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with http(prefix + "/status") as response:
                state = json.load(response)
            if state["state"] in ("completed", "failed"):
                break
            time.sleep(.1)
        self.assertEqual(state["state"], "completed", state)
        with http(prefix + "/report?download=1") as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(json.load(response)["eligible_users"], 24)
        with http(prefix + "/data/profiles.json?download=1") as response:
            self.assertEqual(len(json.load(response)["excluded_users"]), 2)
        for algo in ("kmeans", "dbscan", "pca", "truncated_svd"):
            with http(prefix + "/query", "POST", {"category": "cluster" if algo in ("kmeans", "dbscan") else "reduce",
                                                  "algorithm": algo, "user_ids": [1, 999]}) as response:
                value = json.load(response)
                self.assertEqual(value["algorithm"], algo)
                self.assertEqual(value["users"][-1]["status"], "unknown_user")
        for path in ("/preferences.js", "/preferences.css", "/"):
            with http(path) as response:
                self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as context:
            http(prefix + "/query", "POST", {"algorithm": "bad", "category": "cluster"})
        self.assertEqual(context.exception.code, 422)
        context.exception.close()

    def test_agent_independent_queries_are_grounded_and_errors_override_text(self):
        from service.chat import ChatService
        from test_chat import FakeModel, plan_reply, tool_reply, assessment_reply, text_reply
        result = self.fitted()
        queries = ["cluster_users_kmeans", "cluster_users_dbscan", "reduce_users_pca", "reduce_users_truncated_svd"]
        replies = [plan_reply(*queries)] + [tool_reply(name, {"task_id": "", "user_ids": [1], "parameters": result["parameters"]}) for name in queries]
        client = FakeModel(*replies, assessment_reply(), text_reply("根据四种实际算法结果完成对照。"))
        tasks = TaskService(self.project)
        chat = ChatService(self.project, tasks, lambda: client, preferences=PreferenceService(tasks))
        message = chat.send(chat.create()["id"], "比较用户1的聚类与降维", self.task_id)["message"]
        self.assertEqual(len(message["preferences"]), 4)
        self.assertEqual(message["workflow"]["validation"]["status"], "passed")
        self.assertEqual(len(message["evidence"]), 1)
        (self.directory / "preferences" / "report.json").unlink()
        client = FakeModel(plan_reply(queries[0]), tool_reply(queries[0], {"task_id": "", "user_ids": [1], "parameters": result["parameters"]}),
                           assessment_reply(False), text_reply("用户1属于群体0。"))
        chat = ChatService(self.project, tasks, lambda: client, preferences=PreferenceService(tasks))
        message = chat.send(chat.create()["id"], "查询用户1", self.task_id)["message"]
        self.assertIn("未完成", message["content"])
        self.assertNotIn("属于群体0", message["content"])
        self.assertEqual(message["preferences"], [])


if __name__ == "__main__":
    unittest.main()
