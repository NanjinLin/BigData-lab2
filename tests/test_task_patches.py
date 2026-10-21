"""Regression coverage for startup readiness and terminal task deletion."""

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.hadoop_ready import wait_for_hdfs
from service.app import create_server
from service.chat import ChatService
from service.classification import ClassificationService
from service.preferences import PreferenceService
from service.tasks import TaskError, TaskService


class ReadinessTests(unittest.TestCase):
    def test_waits_through_safe_mode_and_missing_datanode(self):
        responses = ["Safe mode is ON", "Safe mode is OFF", "Live datanodes (0):",
                     "Safe mode is OFF", "Live datanodes (1):"]
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, responses.pop(0))
        messages = []
        with patch("scripts.hadoop_ready.subprocess.run", side_effect=run) as probe, \
                patch("scripts.hadoop_ready.time.sleep"):
            wait_for_hdfs("/test/hdfs", 120, notify=messages.append)
        self.assertEqual(probe.call_count, 5)
        self.assertEqual(len(messages), 3)
        self.assertIn("ready", messages[-1])
        for call in probe.call_args_list:
            self.assertNotIn("leave", call.args[0])
            self.assertNotIn("forceExit", call.args[0])

    def test_timeout_is_bounded_and_preserves_diagnosis(self):
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        with patch("scripts.hadoop_ready.time.monotonic", side_effect=lambda: clock[0]), \
                patch("scripts.hadoop_ready.time.sleep", side_effect=sleep), \
                patch("scripts.hadoop_ready.subprocess.run", return_value=
                      subprocess.CompletedProcess([], 0, "Safe mode is ON")):
            with self.assertRaisesRegex(RuntimeError, "Last check: Safe mode is ON"):
                wait_for_hdfs("/test/hdfs", 3, notify=lambda _: None)
        self.assertEqual(clock[0], 3)

    def test_hung_command_uses_remaining_deadline(self):
        clock = [0]
        def run(args, **kwargs):
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        with patch("scripts.hadoop_ready.time.monotonic", side_effect=lambda: clock[0]), \
                patch("scripts.hadoop_ready.time.sleep"), \
                patch("scripts.hadoop_ready.subprocess.run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "command timed out"):
                wait_for_hdfs("/test/hdfs", 3, notify=lambda _: None)
        self.assertEqual(clock[0], 3)


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.tasks = TaskService(self.project)
        self.make_task("done")
        self.make_task("other")

    def make_task(self, task_id, state="completed"):
        directory = self.project / "runs" / task_id
        directory.mkdir(parents=True)
        (directory / "status.json").write_text(json.dumps(
            {"task_id": task_id, "state": state}), encoding="utf-8")
        (directory / "report.json").write_text(json.dumps(
            {"task_id": task_id, "status": "completed"}), encoding="utf-8")
        (directory / "models").mkdir()
        (directory / "models" / "model.json").write_text("{}")
        for suffix in (".agent.json", ".rules.json", ".service.log"):
            (directory.parent / f"{task_id}{suffix}").write_text("{}")

    def test_removes_only_requested_local_records_and_sidecars(self):
        chat = ChatService(self.project, self.tasks, lambda: None)
        record = chat.create()
        record["selected_task_id"] = "done"
        chat._save(record)
        self.assertTrue(self.tasks.delete("done")["deleted"])
        self.assertFalse((self.project / "runs/done").exists())
        self.assertFalse(list((self.project / "runs").glob("done.*")))
        self.assertTrue((self.project / "runs/other/models/model.json").exists())
        self.assertIsNone(chat.get(record["id"])["selected_task_id"])
        with self.assertRaises(TaskError) as error:
            self.tasks.delete("done")
        self.assertEqual(error.exception.code, 404)

    def test_active_training_and_uncertain_tasks_cannot_be_deleted(self):
        classifiers = ClassificationService(self.tasks)
        preferences = PreferenceService(self.tasks)
        for running in (classifiers.running, preferences.running):
            running.add("done")
            with self.assertRaises(TaskError) as error:
                self.tasks.delete("done")
            self.assertEqual(error.exception.code, 409)
            running.clear()
        self.tasks.active = "done"
        with self.assertRaises(TaskError):
            self.tasks.delete("done")
        self.tasks.active = None
        self.make_task("uncertain", "running")
        with self.assertRaises(TaskError):
            self.tasks.delete("uncertain")
        self.tasks._claim_lock("done")
        with self.assertRaises(TaskError):
            self.tasks.delete("done")
        self.tasks._release_lock("done")

    def test_rejects_traversal_and_deletes_failed_task(self):
        with self.assertRaises(TaskError):
            self.tasks.delete("../other")
        self.make_task("failed", "failed")
        self.tasks.delete("failed")
        self.assertFalse((self.project / "runs/failed").exists())

    def test_http_delete_and_followup_access(self):
        server = create_server(self.project, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}/api/tasks"
        try:
            with urlopen(Request(base + "/done", method="DELETE")) as response:
                self.assertTrue(json.load(response)["deleted"])
            with urlopen(base) as response:
                self.assertEqual([item["task_id"] for item in json.load(response)], ["other"])
            with self.assertRaises(HTTPError) as error:
                urlopen(base + "/done/report")
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
