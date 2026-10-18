"""Conversation and UI model-configuration contracts without paid API calls."""

from __future__ import annotations

import json
import hashlib
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
from service.chat import ChatService, _short_classification_answer
from service.model import ResponsesClient
from service.model import ModelError
from service.tasks import TaskError, TaskService


ROOT = Path(__file__).resolve().parent.parent


def text_reply(message: str, response_id: str = "response-text") -> dict:
    return {"id": response_id, "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": message}]}]}


def tool_reply(name: str, arguments: dict, response_id: str = "response-tool") -> dict:
    return {"id": response_id, "output": [{"type": "function_call", "name": name,
            "arguments": json.dumps(arguments), "call_id": "call-1"}]}


def plan_reply(*tools: str) -> dict:
    return tool_reply("set_analysis_plan", {"goal": "回答用户的数据问题", "needed_data": ["本机任务数据"],
                      "expected_result": "给出有依据的结果和局限",
                      "steps": [{"tool": tool, "purpose": f"执行 {tool}"} for tool in tools]})


def assessment_reply(answerable=True) -> dict:
    return tool_reply("assess_analysis_goal", {"answerable": answerable,
                      "reason": "依据本轮工具结果评估用户目标", "limitations": []})


class FakeModel:
    model = "fake-chat-model"
    configured = True
    api_key = "test-key"
    base_url = "http://127.0.0.1:11434/v1"

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs) -> dict:
        self.calls.append(json.loads(json.dumps(kwargs)))
        return self.responses.pop(0)


class ChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = ROOT / f".test-conversation-{uuid.uuid4().hex}"
        self.project.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.project))
        self.tasks = TaskService(self.project, runner=lambda task_id: 1)

    def tearDown(self):
        for _ in range(200):
            if self.tasks.active is None:
                return
            time.sleep(.01)
        self.fail("测试任务线程未结束，暂不清理其工作目录。")

    def small_task(self, with_clean=True):
        folder = self.project / "runs" / "statistics-fixture"
        folder.mkdir(parents=True)
        records = [("ratings", ["1", "1", "5", "1"]), ("ratings", ["1", "1", "3", "2"]),
                   ("movies", ["1", "Test (2000)", "Drama|Comedy"]),
                   ("users", ["1", "M", "25", "0", "10000"])]
        data = "".join("C\t" + json.dumps({"table": table, "fields": fields}) + "\n" for table, fields in records).encode()
        if with_clean:
            (folder / "cleaned.tsv").write_bytes(data)
        report = {"task_id": folder.name, "status": "completed", "rule_version": "v1",
                  "clean_data_sha256": hashlib.sha256(data).hexdigest(), "rule_parameters": {},
                  "cleaning": {"tables": {table: {"output": sum(t == table for t, _ in records)}
                                          for table in ("ratings", "users", "movies")}}}
        (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return folder.name

    def stats_reply(self, minimum=1):
        return tool_reply("analyze_movielens", {"task_id": "", "group_by": "genre", "sort_by": "mean_rating",
                          "min_ratings": minimum, "limit": 20, "movie_query": "", "genre": "", "period": "all"})

    def test_statistics_plan_reuses_clean_table_and_persists_verified_results(self):
        task_id = self.small_task()
        model = FakeModel(plan_reply("analyze_movielens"), self.stats_reply(), assessment_reply(), text_reply("两个类型均分都是4分，各有2次评分和1位用户，样本较少。"))
        chat = ChatService(self.project, self.tasks, lambda: model)
        ident = chat.create()["id"]
        result = chat.send(ident, "比较不同类型的电影评分", task_id)
        message = result["message"]
        self.assertEqual(message["analyses"][0]["rows"][0]["mean_rating"], 4)
        self.assertEqual(message["workflow"]["validation"]["status"], "limited")
        self.assertTrue(message["workflow"]["validation"]["answerable"])
        self.assertEqual(message["workflow"]["steps"][0]["state"], "completed")
        self.assertTrue(message["evidence"][0].endswith(".json"))
        self.assertEqual(chat.get(ident)["messages"][-1]["analyses"], message["analyses"])
        self.assertFalse(list(self.project.rglob("*.rules.json")))
        self.assertFalse(chat.activity(ident)["running"])

    def test_empty_or_missing_data_overrides_model_claim_that_goal_is_answered(self):
        for with_clean, minimum in ((True, 10), (False, 1)):
            with self.subTest(with_clean=with_clean):
                if (self.project / "runs").exists():
                    shutil.rmtree(self.project / "runs")
                task_id = self.small_task(with_clean)
                model = FakeModel(plan_reply("analyze_movielens"), self.stats_reply(minimum), assessment_reply(), text_reply("本次缺少足够证据。"))
                chat = ChatService(self.project, self.tasks, lambda: model)
                ident = chat.create()["id"]
                result = chat.send(ident, "比较电影类型评分", task_id)
                validation = result["message"]["workflow"]["validation"]
                self.assertFalse(validation["answerable"])
                self.assertEqual(validation["status"], "insufficient")
                self.assertFalse(list(self.project.rglob("*.rules.json")))

    def test_data_tool_requires_plan_and_missing_assessment_is_requested(self):
        task_id = self.small_task()
        model = FakeModel(self.stats_reply(), plan_reply("analyze_movielens"), self.stats_reply(),
                          text_reply("直接回答"), assessment_reply(), text_reply("经过核验后的回答"))
        chat = ChatService(self.project, self.tasks, lambda: model)
        ident = chat.create()["id"]
        result = chat.send(ident, "分析电影类型", task_id)
        error = json.loads(model.calls[1]["input"][-1]["output"])
        self.assertIn("set_analysis_plan", error["error"])
        self.assertEqual(model.calls[4]["tool_choice"], {"type": "function", "name": "assess_analysis_goal"})
        self.assertEqual(result["message"]["content"], "经过核验后的回答")

    def test_activity_is_readable_while_model_request_is_running(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingModel(FakeModel):
            def create(inner, **kwargs):
                if len(inner.calls) == 1:
                    entered.set()
                    release.wait(5)
                return super(BlockingModel, inner).create(**kwargs)
        model = BlockingModel(plan_reply("list_saved_tasks"), tool_reply("list_saved_tasks", {}), assessment_reply(), text_reply("没有任务。"))
        chat = ChatService(self.project, self.tasks, lambda: model)
        ident = chat.create()["id"]
        answers = []
        worker = threading.Thread(target=lambda: answers.append(chat.send(ident, "看看已有数据")))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            activity = chat.activity(ident)
            self.assertTrue(activity["running"])
            self.assertEqual(activity["workflow"]["goal"], "回答用户的数据问题")
        finally:
            release.set()
            worker.join(5)
        self.assertTrue(answers)
        self.assertFalse(chat.activity(ident)["running"])

    def test_statistics_survive_model_connection_failure(self):
        task_id = self.small_task()
        class FailingModel(FakeModel):
            def create(inner, **kwargs):
                if len(inner.calls) == 2:
                    raise ModelError("模拟网络中断")
                return super(FailingModel, inner).create(**kwargs)
        model = FailingModel(plan_reply("analyze_movielens"), self.stats_reply())
        chat = ChatService(self.project, self.tasks, lambda: model)
        result = chat.send(chat.create()["id"], "比较电影类型", task_id)
        self.assertTrue(result["message"]["analyses"])
        self.assertIn("保留", result["message"]["content"])
        self.assertFalse(result["message"]["workflow"]["validation"]["answerable"])

    def test_statistics_survive_malformed_empty_or_unassessed_model_replies(self):
        task_id = self.small_task()
        tails = [
            [{"output": [None]}],
            [assessment_reply(), text_reply("")],
            [text_reply("忽略核验"), text_reply("仍未核验")],
        ]
        for tail in tails:
            with self.subTest(tail=tail):
                model = FakeModel(plan_reply("analyze_movielens"), self.stats_reply(), *tail)
                chat = ChatService(self.project, self.tasks, lambda: model)
                ident = chat.create()["id"]
                message = chat.send(ident, "比较电影类型", task_id)["message"]
                self.assertIn("保留", message["content"])
                self.assertEqual(message["analyses"][0]["rows"][0]["mean_rating"], 4)
                self.assertEqual(chat.get(ident)["messages"][-1]["analyses"], message["analyses"])

    def test_short_classification_answer_includes_all_required_fields(self):
        data = [
            {"algorithm": "logistic_regression", "status": "completed", "user_id": 7,
             "movie_id": 5, "movie_title": "Test Movie", "experiment_id": "cls-abc",
             "positive_probability": .6247, "predicted_class": 1,
             "test": {"f1": .66, "roc_auc": .75},
             "model_version": "cls-abc-logistic_regression",
             "parameters": {"C": 0.1, "max_iter": 250}, "user_train_rating_count": 9},
            {"algorithm": "decision_tree", "status": "completed", "user_id": 7,
             "movie_id": 5, "movie_title": "Test Movie", "experiment_id": "cls-abc",
             "positive_probability": .72, "predicted_class": 1,
             "test": {"f1": .71, "roc_auc": .74},
             "model_version": "cls-abc-decision_tree",
             "parameters": {"max_depth": 10, "min_samples_leaf": 30},
             "user_train_rating_count": 9}]
        for prompt in ("请分析userID7在movieID5里评分超过4分的概率",
                       "预测用户7对电影5，并比较概率与模型版本",
                       "详细解释模型特征和原因"):
            with self.subTest(prompt=prompt):
                result = _short_classification_answer(prompt, data)
                self.assertIn("62.5%", result)
                self.assertIn("72.0%", result)
                self.assertIn("≥4 分", result)
                self.assertIn("0.710", result)
                self.assertIn("0.750", result)
                self.assertIn("cls-abc-logistic_regression", result)
                self.assertIn("cls-abc-decision_tree", result)
                self.assertIn("C=0.1", result)
                self.assertIn("max_depth=10", result)
                self.assertNotIn("局限", result)
        single = _short_classification_answer("只用逻辑回归预测用户7", data[:1])
        self.assertIn("62.5%", single)
        self.assertIn("C=0.1", single)
        self.assertNotIn("决策树", single)
        self.assertIsNone(_short_classification_answer("预测", [{**data[0], "status": "failed"}]))

    def test_classification_answer_explains_metrics_and_prediction(self):
        items = [
            {"algorithm": "logistic_regression", "status": "completed",
             "user_id": 2216, "movie_id": 1193,
             "movie_title": "One Flew Over the Cuckoo's Nest (1975)",
             "experiment_id": "cls-valid", "positive_probability": .679,
             "predicted_class": 1, "model_version": "cls-valid-logistic_regression",
             "parameters": {"C": .1}, "test": {"f1": .736, "roc_auc": .750},
             "user_train_rating_count": 52},
            {"algorithm": "decision_tree", "status": "completed",
             "user_id": 2216, "movie_id": 1193,
             "movie_title": "One Flew Over the Cuckoo's Nest (1975)",
             "experiment_id": "cls-valid", "positive_probability": .75,
             "predicted_class": 1, "model_version": "cls-valid-decision_tree",
             "parameters": {"max_depth": 10}, "test": {"f1": .728, "roc_auc": .746},
             "user_train_rating_count": 52},
        ]
        result = _short_classification_answer("请分析用户2216的评分超过4分的概率", items)
        self.assertIn("**结果分析**", result)
        self.assertIn("7.1 个百分点", result)
        self.assertIn("F1 综合衡量精确率和召回率", result)
        self.assertIn("AUC 衡量区分高分与非高分样本", result)
        self.assertIn("训练期 52 条历史评分", result)
        self.assertIn("≥4 分（包含 4 分）", result)
        self.assertIn("cls-valid-decision_tree", result)
        self.assertIn("C=0.1", result)
        self.assertNotIn("因果关系", result)

        opposite = _short_classification_answer("预测", [items[0], {**items[1],
            "predicted_class": 0, "positive_probability": .25,
            "user_train_rating_count": 0}])
        self.assertIn("两种模型的判断不同", opposite)
        self.assertNotIn("训练期 52 条历史评分", opposite)
        solo = _short_classification_answer("只用逻辑回归", [items[0]])
        self.assertIn("**结果分析**", solo)
        self.assertNotIn("决策树", solo)

    def test_prediction_errors_are_reported_without_fake_probabilities(self):
        from service.classification import ClassificationError

        class BrokenClassifiers:
            def __init__(self, reason):
                self.reason = reason
            def predict(self, task_id, uid, mid, algorithm):
                raise ClassificationError(self.reason)

        task_id = self.small_task()
        for reason in ("未知 UserID：999999。", "未知 MovieID：999999。",
                       "尚无已完成分类训练报告，请先训练两种分类算法。"):
            with self.subTest(reason=reason):
                model = FakeModel(plan_reply("predict_high_rating_logistic_regression"),
                                  tool_reply("predict_high_rating_logistic_regression",
                                             {"task_id": task_id, "user_id": 999999, "movie_id": 1193}),
                                  assessment_reply(False), text_reply("概率为80%。"))
                chat = ChatService(self.project, self.tasks, lambda: model,
                                   classifiers=BrokenClassifiers(reason))
                message = chat.send(chat.create()["id"], "请预测这个用户的评分", task_id)["message"]
                self.assertEqual(message["content"], "无法完成分类预测：" + reason)
                self.assertEqual(message["classifications"], [])
                self.assertFalse(message["workflow"]["validation"]["answerable"])

    def test_two_classification_tools_in_one_model_response(self):
        """A provider may ignore parallel_tool_calls=False; preserve both predictions."""
        task_id = self.small_task()

        class FakeClassification:
            def __init__(self):
                self.calls = []

            def predict(self, tid, uid, mid, algorithm):
                self.calls.append((tid, uid, mid, algorithm))
                return {"status": "completed", "algorithm": algorithm, "task_id": tid,
                        "user_id": uid, "movie_id": mid, "experiment_id": "cls-stub",
                        "positive_probability": 0.8 if algorithm == "decision_tree" else 0.2,
                        "predicted_class": int(algorithm == "decision_tree"),
                        "model_version": f"cls-stub-{algorithm}",
                        "parameters": {"C": 1.0} if algorithm == "logistic_regression" else {"max_depth": 10},
                        "test": {"f1": 0.73 if algorithm == "logistic_regression" else 0.72,
                                 "roc_auc": 0.75},
                        "history_warning": "该用户在训练期无历史评分。",
                        "evidence": f"runs/{tid}/classification/report.json"}

        def paired_calls():
            return {"id": "batch", "output": [
                {"type": "function_call", "name": f"predict_high_rating_{algorithm}",
                 "call_id": f"call-{algorithm}",
                 "arguments": json.dumps({"task_id": task_id, "user_id": 1, "movie_id": 1})}
                for algorithm in ("logistic_regression", "decision_tree")]}

        for batch_plan in (False, True):
            with self.subTest(batch_plan=batch_plan):
                plan = plan_reply("predict_high_rating_logistic_regression", "predict_high_rating_decision_tree")
                responses = [plan, paired_calls()] if not batch_plan else [
                    {"id": "batch-plan", "output": plan["output"] + paired_calls()["output"]}]
                model = FakeModel(*responses, assessment_reply(), text_reply("两种模型预测结果已比较。"))
                classifiers = FakeClassification()
                chat = ChatService(self.project, self.tasks, lambda: model, classifiers=classifiers)
                answer = chat.send(chat.create()["id"], "比较用户1对电影1的两种预测", task_id)["message"]
                self.assertIn("逻辑回归", answer["content"])
                self.assertIn("决策树", answer["content"])
                self.assertIn("cls-stub-logistic_regression", answer["content"])
                self.assertIn("cls-stub-decision_tree", answer["content"])
                self.assertIn("C=1.0", answer["content"])
                self.assertIn("max_depth=10", answer["content"])
                self.assertEqual([r["algorithm"] for r in answer["classifications"]], ["logistic_regression", "decision_tree"])
                self.assertEqual([call[-1] for call in classifiers.calls], ["logistic_regression", "decision_tree"])
                self.assertEqual(answer["workflow"]["validation"]["status"], "limited")
                inputs = model.calls[-2]["input"]
                outputs = [entry for entry in inputs if entry.get("type") == "function_call_output"]
                self.assertEqual([entry["call_id"] for entry in outputs][-2:], ["call-logistic_regression", "call-decision_tree"])
                self.assertEqual(len(answer["evidence"]), 1)

    def test_multi_tool_call_refuses_side_effects_before_any_execution(self):
        """Batch execution must never dispatch Hadoop or classification training."""
        model = FakeModel({"id": "unsafe", "output": [
            {"type": "function_call", "name": "run_hadoop_clean_and_score",
             "call_id": "call-run", "arguments": json.dumps({"rule_version": "quality-clean-v1.0.0"})},
            {"type": "function_call", "name": "list_saved_tasks", "call_id": "call-list", "arguments": "{}"}]})
        chat = ChatService(self.project, self.tasks, lambda: model)
        with self.assertRaisesRegex(TaskError, "不可批量执行"):
            chat.send(chat.create()["id"], "同时执行清洗并查询任务")
        self.assertIsNone(self.tasks.active)

    def test_duplicate_call_ids_in_batch_are_refused(self):
        model = FakeModel({"id": "duplicates", "output": [
            {"type": "function_call", "name": "list_saved_tasks", "call_id": "same", "arguments": "{}"},
            {"type": "function_call", "name": "list_saved_tasks", "call_id": "same", "arguments": "{}"}]})
        chat = ChatService(self.project, self.tasks, lambda: model)
        with self.assertRaisesRegex(TaskError, "标识缺失或重复"):
            chat.send(chat.create()["id"], "查看任务")

    def test_statistics_http_api_and_download(self):
        task_id = self.small_task()
        server = create_server(self.project, "127.0.0.1", 0, runner=lambda task_id: 1,
                               agent=DataGovernanceAgent(FakeModel()))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(base + f"/api/tasks/{task_id}/analysis", data=b'{"group_by":"genre"}',
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request) as response:
                result = json.load(response)
            charts = Request(base + f"/api/tasks/{task_id}/charts", data=b'{"kind":"scatter"}',
                             headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(charts) as response:
                chart_data = json.load(response)
            self.assertEqual(chart_data["rating_distribution"], {"1": 0, "2": 0, "3": 1, "4": 0, "5": 1})
            for asset in ("charts.js", "chart-renderers.js", "charts.css"):
                with urlopen(base + "/" + asset) as response:
                    self.assertEqual(response.status, 200)
            with urlopen(base + f"/api/tasks/{task_id}/analyses/{result['analysis_id']}?download=1") as response:
                self.assertIn("statistics.json", response.headers["Content-Disposition"])
                self.assertEqual(json.load(response)["rows"], result["rows"])
            with urlopen(Request(base + "/api/conversations", data=b"{}", method="POST")) as response:
                ident = json.load(response)["id"]
            with urlopen(base + f"/api/conversations/{ident}/activity") as response:
                self.assertFalse(json.load(response)["running"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

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
        model = FakeModel(plan_reply("read_task_report"), tool_reply("read_task_report", {"task_id": task_id}), assessment_reply(),
                          text_reply("本次电影表去重 204 条。"))
        chat = ChatService(self.project, self.tasks, lambda: model)
        ident = chat.create()["id"]
        result = chat.send(ident, "电影去重多少条？", task_id)
        self.assertEqual(result["message"]["evidence"], [f"runs/{task_id}/report.json"])
        self.assertIn("204", result["message"]["content"])
        supplied = json.loads(model.calls[2]["input"][-1]["output"])
        self.assertEqual(supplied["cleaning"]["tables"]["movies"]["duplicate"], 204)

    def test_model_selected_job_does_not_require_a_second_planning_call(self) -> None:
        model = FakeModel(plan_reply("run_hadoop_clean_and_score"),
                          tool_reply("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"}), assessment_reply(False),
                          text_reply("任务已提交，稍后可查看状态。"))
        chat = ChatService(self.project, self.tasks, lambda: model)
        ident = chat.create()["id"]
        result = chat.send(ident, "请用默认规则清洗完整 MovieLens 数据并评估质量")
        task_id = result["message"]["task_id"]
        self.assertTrue(task_id)
        self.assertEqual(result["selected_task_id"], task_id)
        self.assertEqual(len(model.calls), 4)
        self.assertEqual(model.calls[1]["input"][-1]["type"], "function_call_output")
        self.assertEqual(result["message"]["workflow"]["validation"]["status"], "awaiting_task")
        self.assertFalse(result["message"]["workflow"]["validation"]["answerable"])
        for _ in range(100):
            if self.tasks.active is None:
                break
            time.sleep(.01)
        saved = (self.project / "runs" / ".conversations" / f"{ident}.json").read_text(encoding="utf-8")
        self.assertNotIn("api_key", saved)
        self.assertIn(task_id, saved)

    def test_submitted_job_is_recorded_even_if_followup_answer_is_empty(self) -> None:
        model = FakeModel(plan_reply("run_hadoop_clean_and_score"),
                          tool_reply("run_hadoop_clean_and_score", {"rule_version": "quality-clean-v1.0.0"}), assessment_reply(False),
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
