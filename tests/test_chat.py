"""Conversation and UI model-configuration contracts without paid API calls."""

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
from service.chat import ChatService
from service.model import ResponsesClient
from service.tasks import TaskService


ROOT = Path(__file__).resolve().parent.parent


def text_reply(message: str, response_id: str = "response-text") -> dict:
    return {"id": response_id, "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": message}]}]}


def tool_reply(name: str, arguments: dict, response_id: str = "response-tool") -> dict:
    return {"id": response_id, "output": [{"type": "function_call", "name": name,
            "arguments": json.dumps(arguments), "call_id": "call-1"}]}


class FakeModel:
    model = "fake-chat-model"
    configured = True
    api_key = "test-key"
    base_url = "http://127.0.0.1:11434/v1"

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class ChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = ROOT / f".test-conversation-{uuid.uuid4().hex}"
        self.project.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.project))
        self.tasks = TaskService(self.project, runner=lambda task_id: 1)

    def test_ordinary_conversation_keeps_context_without_forced_report_tool(self) -> None:
        model = FakeModel(text_reply("你好，我可以回答一般问题，也可以按需调用工具。"),
                          text_reply("刚才我们谈到的是项目能力。", "response-second"))
        chat = ChatService(self.project, self.tasks, lambda: model)
        ident = chat.create()["id"]
        first = chat.send(ident, "你好")
        second = chat.send(ident, "我们刚才谈了什么？")
        self.assertIn("刚才", second["message"]["content"])
        self.assertEqual(model.calls[0]["tool_choice"], "auto")
        self.assertEqual(model.calls[1]["input"][0], {"role": "user", "content": "你好"})
        self.assertEqual(len(chat.get(ident)["messages"]), 4)
        self.assertEqual(first["message"]["evidence"], [])

    def test_report_answer_uses_current_task_and_keeps_evidence(self) -> None:
        task_id = "completed-test-task"
        folder = self.project / "runs" / task_id
        folder.mkdir(parents=True)
        (folder / "status.json").write_text(json.dumps({"task_id": task_id, "state": "completed"}), encoding="utf-8")
        report = {"task_id": task_id, "status": "completed", "rule_version": "quality-clean-v1.0.0",
                  "cleaning": {"tables": {"movies": {"duplicate": 204}}, "unresolved": {},
                               "audit_samples": {}}}
        (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
        model = FakeModel(tool_reply("read_task_report", {"task_id": task_id}),
                          text_reply("本次电影表去重 204 条。"))
        chat = ChatService(self.project, self.tasks, lambda: model)
        ident = chat.create()["id"]
        result = chat.send(ident, "电影去重多少条？", task_id)
        self.assertEqual(result["message"]["evidence"], [f"runs/{task_id}/report.json"])
        self.assertIn("204", result["message"]["content"])
        supplied = json.loads(model.calls[1]["input"][-1]["output"])
        self.assertEqual(supplied["cleaning"]["tables"]["movies"]["duplicate"], 204)

    def test_model_selected_job_does_not_require_a_second_planning_call(self) -> None:
        model = FakeModel(tool_reply("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"}),
                          text_reply("任务已提交，稍后可查看状态。"))
        chat = ChatService(self.project, self.tasks, lambda: model)
        ident = chat.create()["id"]
        result = chat.send(ident, "请用默认规则清洗完整 MovieLens 数据并评估质量")
        task_id = result["message"]["task_id"]
        self.assertTrue(task_id)
        self.assertEqual(result["selected_task_id"], task_id)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(model.calls[1]["input"][-1]["type"], "function_call_output")
        for _ in range(100):
            if self.tasks.active is None:
                break
            time.sleep(.01)
        saved = (self.project / "runs" / ".conversations" / f"{ident}.json").read_text(encoding="utf-8")
        self.assertNotIn("api_key", saved)
        self.assertIn(task_id, saved)

    def test_submitted_job_is_recorded_even_if_followup_answer_is_empty(self) -> None:
        model = FakeModel(tool_reply("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"}),
                          text_reply(""))
        chat = ChatService(self.project, self.tasks, lambda: model)
        ident = chat.create()["id"]
        result = chat.send(ident, "请按默认规则运行清洗和评分")
        self.assertTrue(result["message"]["task_id"])
        self.assertIn("已提交", result["message"]["content"])
        self.assertEqual(len(chat.get(ident)["messages"]), 2)

    def test_model_settings_api_never_returns_key_or_writes_it_to_project(self) -> None:
        agent = DataGovernanceAgent(ResponsesClient(api_key="", model="initial"))
        server = create_server(self.project, "127.0.0.1", 0, runner=lambda task_id: 1, agent=agent)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(base + "/api/model-config",
                              data=json.dumps({"model": "demo-model", "base_url": "http://127.0.0.1:11434/v1",
                                               "api_key": "my-secret-test-key"}).encode(),
                              headers={"Content-Type": "application/json"}, method="PUT")
            with urlopen(request) as response:
                saved = json.load(response)
            self.assertTrue(saved["configured"])
            self.assertNotIn("my-secret-test-key", json.dumps(saved))
            with urlopen(base + "/api/model-config") as response:
                self.assertEqual(json.load(response)["model"], "demo-model")
            bad = Request(base + "/api/model-config",
                          data=json.dumps({"model": "demo", "base_url": "http://example.com/v1",
                                           "api_key": "different"}).encode(),
                          headers={"Content-Type": "application/json"}, method="PUT")
            with self.assertRaises(HTTPError) as error:
                urlopen(bad)
            self.assertEqual(error.exception.code, 400)
            with urlopen(Request(base + "/api/model-config", method="DELETE")) as response:
                self.assertFalse(json.load(response)["configured"])
            self.assertFalse(any(self.project.rglob("*key*")))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_conversation_http_route_returns_natural_answer_and_history(self) -> None:
        model = FakeModel(text_reply("你好，我可以讨论项目并按需查看任务。"))
        agent = DataGovernanceAgent(model)
        server = create_server(self.project, "127.0.0.1", 0, runner=lambda task_id: 1, agent=agent)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            create = Request(base + "/api/conversations", data=b"{}",
                             headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(create) as response:
                chat_id = json.load(response)["id"]
            send = Request(base + f"/api/conversations/{chat_id}/messages",
                           data=json.dumps({"message": "你好"}).encode(),
                           headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(send) as response:
                answer = json.load(response)
            self.assertIn("你好", answer["message"]["content"])
            self.assertIsNone(answer["message"]["task_id"])
            with urlopen(base + f"/api/conversations/{chat_id}") as response:
                stored = json.load(response)
            self.assertEqual([item["role"] for item in stored["messages"]], ["user", "assistant"])
            self.assertNotIn("test-key", json.dumps(stored))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
