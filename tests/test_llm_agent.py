"""Model-driven dispatch and report tool tests; no paid API request is made."""

from __future__ import annotations

import json
import shutil
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from service.agent import DataGovernanceAgent
from service.model import ModelError, ResponsesClient
from service.tasks import TaskService


ROOT = Path(__file__).resolve().parent.parent
REPORT = json.loads((ROOT / "runs/20260923T144724Z-3509cc23/report.json").read_text(encoding="utf-8"))


class FakeModel:
    model = "test-model"
    configured = True

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **payload) -> dict:
        self.calls.append(payload)
        return self.responses.pop(0)


def tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {"type": "function_call", "name": name, "arguments": json.dumps(arguments), "call_id": call_id}


def text_response(value: str, response_id: str = "resp_text") -> dict:
    return {"id": response_id, "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": value}]}]}


class AgentTests(unittest.TestCase):
    def test_configuration_advertises_adjustable_rule_tool(self) -> None:
        agent = DataGovernanceAgent(FakeModel())
        self.assertIn("run_hadoop_with_adjusted_rules", agent.configuration["capabilities"])

    def test_model_call_is_required_before_hadoop_task(self) -> None:
        model = FakeModel({"id": "resp_plan", "output": [
            tool_call("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"})]})
        decision = DataGovernanceAgent(model).plan("请清洗并评价 MovieLens 数据质量")
        self.assertTrue(decision["accepted"])
        self.assertEqual(decision["model_response_id"], "resp_plan")
        self.assertEqual(model.calls[0]["tools"][0]["name"], "run_hadoop_clean_and_score")
        self.assertFalse(model.calls[0]["parallel_tool_calls"])

    def test_documented_default_prompt_is_supported(self) -> None:
        model = FakeModel({"id": "resp_plan", "output": [
            tool_call("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"})]})
        prompt = "请使用默认规则清洗 MovieLens 1M，评估清洗前后的五维数据质量，并说明异常处置和局限。"
        self.assertTrue(DataGovernanceAgent(model).plan(prompt)["accepted"])

    def test_model_can_select_adjusted_rules_with_explicit_parameters(self) -> None:
        options = {"min_retained_rating": 4, "missing_title_year_policy": "quarantine",
                   "table_weights": {"ratings": 5, "users": 3, "movies": 2}}
        model = FakeModel({"id": "resp_custom", "output": [
            tool_call("run_hadoop_with_adjusted_rules", options)]})
        prompt = ("清洗 MovieLens 1M，最低保留评分 4，标题缺少年份时隔离；"
                  "三表评分权重：评分 5、用户 3、电影 2。")
        decision = DataGovernanceAgent(model).plan(prompt)
        self.assertTrue(decision["accepted"])
        self.assertEqual(decision["options"], options)
        self.assertEqual(decision["tool"], "run_hadoop_with_adjusted_rules")

    def test_supported_minimum_rating_paraphrases_are_accepted(self) -> None:
        options = {"min_retained_rating": 4, "missing_title_year_policy": "flag",
                   "table_weights": {"ratings": 1, "users": 1, "movies": 1}}
        for prompt in ("清洗 MovieLens 1M，把低于 4 分的评分隔离",
                       "清洗 MovieLens 1M，最低保留评分为四分"):
            with self.subTest(prompt=prompt):
                model = FakeModel({"id": "resp_custom", "output": [
                    tool_call("run_hadoop_with_adjusted_rules", options)]})
                self.assertTrue(DataGovernanceAgent(model).plan(prompt)["accepted"])

    def test_custom_tool_does_not_silently_ignore_another_condition(self) -> None:
        options = {"min_retained_rating": 4, "missing_title_year_policy": "flag",
                   "table_weights": {"ratings": 1, "users": 1, "movies": 1}}
        model = FakeModel({"id": "resp_custom", "output": [
            tool_call("run_hadoop_with_adjusted_rules", options)]})
        decision = DataGovernanceAgent(model).plan("最低保留评分 4，并且只清洗女性用户")
        self.assertFalse(decision["accepted"])

    def test_experiment_manual_example_is_supported(self) -> None:
        model = FakeModel({"id": "resp_plan", "output": [
            tool_call("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"})]})
        prompt = ("请使用默认规则清洗 MovieLens 1M，评估清洗前后的 Accurate、Complete、Unique、"
                  "Up-to-date、Consistent 五个维度，并说明处理了哪些问题、还有哪些问题无法解决。")
        self.assertTrue(DataGovernanceAgent(model).plan(prompt)["accepted"])

    def test_model_refusal_and_unsupported_scope_never_dispatch(self) -> None:
        refused = DataGovernanceAgent(FakeModel(text_response("这不是数据清洗请求。")))
        self.assertFalse(refused.plan("明天的天气如何？")["accepted"])
        model = FakeModel({"id": "resp_wrong", "output": [
            tool_call("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"})]})
        decision = DataGovernanceAgent(model).plan("只保留男性用户再清洗")
        self.assertFalse(decision["accepted"])

    def test_default_deduplication_wording_is_not_treated_as_rule_change(self) -> None:
        model = FakeModel({"id": "resp_plan", "output": [
            tool_call("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"})]})
        self.assertTrue(DataGovernanceAgent(model).plan("清洗数据并说明删除重复评分的影响")["accepted"])

    def test_subset_and_threshold_paraphrases_cannot_be_silently_accepted(self) -> None:
        for prompt in ("对女性用户的数据进行清洗并评分", "将年龄门槛定为 25 后评估",
                       "清洗 Netflix 数据并评分", "按用户性别清洗并评分"):
            with self.subTest(prompt=prompt):
                model = FakeModel({"id": "resp_plan", "output": [
                    tool_call("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"})]})
                self.assertFalse(DataGovernanceAgent(model).plan(prompt)["accepted"])

    def test_rejects_wrong_tool_version_or_multiple_calls(self) -> None:
        wrong = DataGovernanceAgent(FakeModel({"id": "resp_wrong", "output": [
            tool_call("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v2"})]}))
        with self.assertRaises(ModelError):
            wrong.plan("清洗数据")
        multiple = DataGovernanceAgent(FakeModel({"id": "resp_many", "output": [
            tool_call("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"}),
            tool_call("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"}, "call_2")]}))
        with self.assertRaises(ModelError):
            multiple.plan("清洗数据")

    def test_followup_uses_report_tool_before_model_answer(self) -> None:
        first = {"id": "resp_read", "output": [tool_call("read_task_report", {}, "call_read")]}
        model = FakeModel(first, text_response("电影表去重 204 条。", "resp_answer"))
        result = DataGovernanceAgent(model).answer("电影去重多少条？", REPORT)
        self.assertIn("204", result["answer"])
        self.assertEqual(result["model_response_id"], "resp_answer")
        followup = model.calls[1]["input"]
        self.assertIn("已读取", model.calls[1]["instructions"])
        tool_output = next(item for item in followup if item.get("type") == "function_call_output")
        self.assertEqual(tool_output["call_id"], "call_read")
        evidence = json.loads(tool_output["output"])
        self.assertEqual(evidence["task_id"], REPORT["task_id"])
        self.assertEqual(evidence["cleaning"]["tables"]["movies"]["duplicate"], 204)
        self.assertEqual(evidence["clean_data_sha256"], REPORT["clean_data_sha256"])
        self.assertEqual(evidence["cleaning"]["audit_samples"], REPORT["cleaning"]["audit_samples"])

    def test_followup_without_report_tool_is_rejected(self) -> None:
        model = FakeModel(text_response("我猜电影去重 999 条。"))
        with self.assertRaises(ModelError):
            DataGovernanceAgent(model).answer("电影去重多少条？", REPORT)

    def test_malformed_model_output_is_reported_as_model_error(self) -> None:
        malformed = FakeModel({"id": "resp_bad", "output": [None]})
        with self.assertRaises(ModelError):
            DataGovernanceAgent(malformed).plan("请清洗数据")


class ClientTests(unittest.TestCase):
    def test_non_object_http_error_does_not_crash(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = b"[]"
                self.send_response(400)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = ResponsesClient(api_key="test-secret", model="test-model",
                                     base_url=f"http://127.0.0.1:{server.server_port}/v1")
            with self.assertRaises(ModelError) as error:
                client.create(input="hello", instructions="test")
            self.assertIn("HTTP 400", str(error.exception))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_model_http_error_reports_model_cause_without_secret(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.dumps({"error": {"code": "model_not_found",
                                             "message": "The model test-missing does not exist"}}).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = ResponsesClient(api_key="test-secret", model="test-missing",
                                     base_url=f"http://127.0.0.1:{server.server_port}/v1")
            with self.assertRaises(ModelError) as error:
                client.create(input="hello", instructions="test")
            self.assertIn("model_not_found", str(error.exception))
            self.assertIn("test-missing", str(error.exception))
            self.assertNotIn("test-secret", str(error.exception))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_responses_client_posts_to_api_with_server_side_key(self) -> None:
        captured = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured["path"] = self.path
                captured["authorization"] = self.headers.get("Authorization")
                captured["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body = json.dumps(text_response("ok")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = ResponsesClient(api_key="test-secret", model="test-model",
                                     base_url=f"http://127.0.0.1:{server.server_port}/v1")
            response = client.create(input="hello", instructions="test")
            self.assertEqual(response["id"], "resp_text")
            self.assertEqual(captured["path"], "/v1/responses")
            self.assertEqual(captured["authorization"], "Bearer test-secret")
            self.assertIs(captured["body"]["store"], False)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_missing_key_is_explicit(self) -> None:
        client = ResponsesClient(api_key="", model="test-model")
        with self.assertRaisesRegex(ModelError, "OPENAI_API_KEY"):
            client.create(input="hello", instructions="test")

    def test_service_dispatch_and_followup_use_real_http_model_protocol(self) -> None:
        responses = [
            {"id": "resp_dispatch", "output": [tool_call(
                "run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"})]},
            {"id": "resp_read", "output": [tool_call("read_task_report", {}, "call_report")]},
            text_response("电影表去重 204 条。", "resp_final"),
        ]
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps(responses.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        model_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=model_server.serve_forever, daemon=True)
        worker.start()
        project = ROOT / f".test-model-flow-{uuid.uuid4().hex}"
        project.mkdir()
        try:
            model = ResponsesClient(api_key="test-secret", model="test-model",
                                    base_url=f"http://127.0.0.1:{model_server.server_port}/v1")
            agent = DataGovernanceAgent(model)
            service = TaskService(project, runner=lambda task_id: 1, agent=agent)
            task = service.start("请清洗并评估 MovieLens 数据质量")
            self.assertEqual(task["agent"]["model_response_id"], "resp_dispatch")
            self.assertEqual(service.agent.answer("电影去重多少条？", REPORT)["answer"], "电影表去重 204 条。")
            self.assertEqual(len(requests), 3)
            self.assertEqual(requests[0]["tools"][0]["name"], "run_hadoop_clean_and_score")
            self.assertEqual(requests[1]["tools"][0]["name"], "read_task_report")
            self.assertEqual(requests[2]["input"][-1]["type"], "function_call_output")
            for _ in range(100):
                if service.active is None:
                    break
                time.sleep(0.01)
            self.assertIsNone(service.active)
        finally:
            model_server.shutdown()
            model_server.server_close()
            worker.join()
            shutil.rmtree(project)


if __name__ == "__main__":
    unittest.main()
