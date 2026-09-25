"""Consumer-facing tests for the local iteration-one service."""

from __future__ import annotations

import json
import shutil
import threading
import time
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service.agent import DataGovernanceAgent
from service.app import create_server
from service.model import ResponsesClient
from service.tasks import TaskError, TaskService, summarize_failure
from service.rules import build_rules


ROOT = Path(__file__).resolve().parent.parent
REPORT = json.loads((ROOT / "runs/20260923T144724Z-3509cc23/report.json").read_text(encoding="utf-8"))


class StubAgent:
    configuration = {"model": "test-model", "provider": "OpenAI Responses API", "configured": True}

    def plan(self, prompt: str) -> dict:
        if "改成" in prompt:
            return {"accepted": False, "reason": "不支持自定义规则。"}
        return {"accepted": True, "tool": "run_hadoop_clean_and_score",
                "rule_version": "quality-clean-v1.0.0", "model": "test-model",
                "model_response_id": "resp_plan"}

    def answer(self, question: str, report: dict) -> dict:
        return {"answer": f"本次电影去重 {report['cleaning']['tables']['movies']['duplicate']} 条。",
                "model": "test-model", "model_response_id": "resp_answer"}


class CountingAgent(StubAgent):
    def __init__(self) -> None:
        self.plan_calls = 0

    def plan(self, prompt: str) -> dict:
        self.plan_calls += 1
        return super().plan(prompt)


class CustomStubAgent(StubAgent):
    def plan(self, prompt: str) -> dict:
        options = {"min_retained_rating": 4, "missing_title_year_policy": "quarantine",
                   "table_weights": {"ratings": 5, "users": 3, "movies": 2}}
        return {"accepted": True, "tool": "run_hadoop_with_adjusted_rules",
                "rule_version": build_rules(options)["rule_version"], "options": options,
                "model": "test-model", "model_response_id": "resp_custom"}


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = ROOT

    def test_historical_report_is_readable_but_missing_clean_file_is_explicit(self) -> None:
        service = TaskService(self.project, runner=lambda task_id: 0)
        self.assertEqual(service.status(REPORT["task_id"])["state"], "completed")
        self.assertEqual(service.report(REPORT["task_id"])["clean_data_version"], REPORT["clean_data_version"])
        self.assertEqual(service.samples(REPORT["task_id"])["state"], "unavailable")

    def test_failed_start_exposes_the_actual_last_error(self) -> None:
        output = "Checking input\nTraceback (most recent call last):\nRuntimeError: Raw input checksum or size mismatch: ratings.dat\n"
        self.assertEqual(summarize_failure(output), "Raw input checksum or size mismatch: ratings.dat")

    def test_custom_decision_creates_frozen_rules_and_trace(self) -> None:
        project = ROOT / f".test-custom-rules-{uuid.uuid4().hex}"
        project.mkdir()
        self.addCleanup(lambda: shutil.rmtree(project))
        service = TaskService(project, runner=lambda task_id: 1, agent=CustomStubAgent())
        task = service.start("清洗数据，最低保留评分 4")
        task_id = task["task_id"]
        rules_path = project / "runs" / f"{task_id}.rules.json"
        trace_path = project / "runs" / f"{task_id}.agent.json"
        self.assertTrue(rules_path.is_file())
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        self.assertEqual(rules["processing_policy"]["min_retained_rating"], 4)
        self.assertEqual(trace["rule_version"], rules["rule_version"])
        self.assertEqual(trace["options"]["table_weights"], {"ratings": 5, "users": 3, "movies": 2})
        self.assertEqual(len(trace["rule_sha256"]), 64)
        for _ in range(100):
            if service.active is None:
                break
            time.sleep(0.01)

    def test_completed_status_with_missing_or_corrupt_report_has_explicit_error(self) -> None:
        project = ROOT / f".test-report-{uuid.uuid4().hex}"
        task_id = "test-completed-report"
        directory = project / "runs" / task_id
        directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(project))
        (directory / "status.json").write_text(json.dumps({"task_id": task_id, "state": "completed"}),
                                                 encoding="utf-8")
        service = TaskService(project)
        with self.assertRaisesRegex(TaskError, "缺失"):
            service.report(task_id)
        (directory / "report.json").write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(TaskError, "损坏"):
            service.report(task_id)

    def test_http_rejects_custom_rules_and_serves_actual_report(self) -> None:
        server = create_server(self.project, "127.0.0.1", 0, runner=lambda task_id: 0,
                               agent=StubAgent())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(f"{base}/api/tasks/{REPORT['task_id']}/report") as response:
                actual = json.load(response)
            self.assertEqual(actual["quality_after"]["overall"]["Accurate"], REPORT["quality_after"]["overall"]["Accurate"])
            with urlopen(f"{base}/api/agent") as response:
                self.assertEqual(json.load(response)["model"], "test-model")
            ask = Request(f"{base}/api/tasks/{REPORT['task_id']}/ask",
                          data=json.dumps({"question": "电影去重多少条？"}).encode(),
                          headers={"Content-Type": "application/json"})
            with urlopen(ask) as response:
                self.assertEqual(json.load(response)["model_response_id"], "resp_answer")
            request = Request(
                f"{base}/api/tasks",
                data=json.dumps({"prompt": "把评分上限改成 10 再清洗"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 422)
            malformed = Request(f"{base}/api/tasks", data=b"{}", method="POST",
                                headers={"Content-Length": "invalid"})
            with self.assertRaises(HTTPError) as error:
                urlopen(malformed)
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_a_second_service_cannot_start_while_first_owns_the_task_lock(self) -> None:
        project = ROOT / f".test-task-lock-{uuid.uuid4().hex}"
        project.mkdir()
        self.addCleanup(lambda: shutil.rmtree(project))
        release = threading.Event()
        first = TaskService(project, runner=lambda task_id: (release.wait(5) or 0), agent=StubAgent())
        second = TaskService(project, runner=lambda task_id: 0, agent=StubAgent())
        try:
            task = first.start("清洗并评估数据质量")
            self.assertEqual(first.status(task["task_id"])["agent"]["model_response_id"], "resp_plan")
            with self.assertRaises(TaskError) as error:
                second.start("清洗并评估数据质量")
            self.assertEqual(error.exception.code, 409)
        finally:
            release.set()
        for _ in range(100):
            if first.active is None:
                break
            time.sleep(0.01)
        self.assertIsNone(first.active)

    def test_busy_service_does_not_charge_a_second_model_decision(self) -> None:
        project = ROOT / f".test-busy-{uuid.uuid4().hex}"
        project.mkdir()
        self.addCleanup(lambda: shutil.rmtree(project))
        release = threading.Event()
        agent = CountingAgent()
        service = TaskService(project, runner=lambda task_id: (release.wait(5) or 0), agent=agent)
        try:
            service.start("清洗并评估数据质量")
            with self.assertRaises(TaskError) as error:
                service.start("清洗并评估数据质量")
            self.assertEqual(error.exception.code, 409)
            self.assertEqual(agent.plan_calls, 1)
        finally:
            release.set()
        for _ in range(100):
            if service.active is None:
                break
            time.sleep(0.01)

    def test_missing_model_key_rejects_new_task_without_creating_artifacts(self) -> None:
        project = ROOT / f".test-no-key-{uuid.uuid4().hex}"
        project.mkdir()
        self.addCleanup(lambda: shutil.rmtree(project))
        agent = DataGovernanceAgent(ResponsesClient(api_key="", model="test-model"))
        server = create_server(project, "127.0.0.1", 0, runner=lambda task_id: 0, agent=agent)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/api/tasks",
                              data=json.dumps({"prompt": "清洗并评估数据质量"}).encode(),
                              headers={"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 503)
            self.assertFalse((project / "runs").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
