"""Real fitting on isolated verified clean records; no mocked predictions."""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service.app import create_server
from service.classification import (ALGORITHMS, ClassificationError, ClassificationService,
                                    History, _split_features, predict, train, training_report)
from service.tasks import TaskService


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.task_id = "clean-demo"
        self.directory = self.project / "runs" / self.task_id
        self.directory.mkdir(parents=True)
        self.report = self.fixture()

    def fixture(self):
        records = []
        for uid in range(1, 9):
            records.append(("users", [str(uid), "M" if uid % 2 else "F", "25", "4", "01001"]))
        for mid in range(1, 9):
            records.append(("movies", [str(mid), f"Movie {mid} (2000)", "Action|Comedy" if mid % 2 else "Drama"]))
        # Every period contains positive/negative samples, realistic train events.
        for t in range(1, 81):
            for uid in range(1, 9):
                mid = ((t + uid) % 8) + 1
                label = 5 if (uid + mid + t // 4) % 3 else 2
                records.append(("ratings", [str(uid), str(mid), str(label), str(t)]))
        content = "".join("C\t" + json.dumps({"kind": "clean", "table": table, "fields": fields,
                                                 "source_hash": "a", "source_row_count": 1}) + "\n"
                          for table, fields in records).encode()
        (self.directory / "cleaned.tsv").write_bytes(content)
        report = {"task_id": self.task_id, "status": "completed", "T1": "1970-01-01T00:00:40Z", "T2": "1970-01-01T00:01:00Z",
                  "rule_version": "v1", "clean_data_version": "clean-demo-v1", "clean_data_sha256": hashlib.sha256(content).hexdigest(),
                  "cleaning": {"tables": {t: {"output": sum(k == t for k, _ in records)} for t in ("ratings", "users", "movies")}},
                  "period_counts": {"train": 320, "validation": 160, "test": 160}}
        (self.directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    def test_train_both_and_predict_with_saved_models(self):
        result = train(self.directory, self.report)
        self.assertEqual(result["split_counts"], {"train": 320, "validation": 160, "test": 160})
        self.assertEqual(set(result["models"]), set(ALGORITHMS))
        self.assertEqual(result["feature_count"], len(result["feature_names"]))
        self.assertEqual(training_report(self.directory, self.report)["experiment_id"], result["experiment_id"])
        for algo in ALGORITHMS:
            prediction = predict(self.directory, self.report, 1, 2, algo)
            self.assertTrue(0 <= prediction["positive_probability"] <= 1)
            self.assertEqual(prediction["predicted_class"], int(prediction["positive_probability"] >= 0.5))
            self.assertEqual(prediction["user_train_rating_count"], 40)
            self.assertEqual(prediction["model_version"], result["models"][algo]["model_version"])
            self.assertEqual(prediction["test"]["samples"], 160)
        with self.assertRaisesRegex(ClassificationError, "未知 UserID"):
            predict(self.directory, self.report, 999, 2, "logistic_regression")
        with self.assertRaisesRegex(ClassificationError, "未知 MovieID"):
            predict(self.directory, self.report, 1, 999, "logistic_regression")

    def test_masked_user_attributes_from_cleaner_train_and_predict(self):
        """Hadoop cleanup retains user IDs even if gender/age/occupation are the cleaned null marker."""
        source = self.directory / "cleaned.tsv"
        records = []
        for line in source.read_text(encoding="utf-8").splitlines():
            tag, payload = line.split("\t", 1)
            item = json.loads(payload)
            if item["table"] == "users":
                uid = int(item["fields"][0])
                if uid == 1:
                    item["fields"][1] = "\\N"
                elif uid == 2:
                    item["fields"][2] = "\\N"
                elif uid == 3:
                    item["fields"][3] = "\\N"
            records.append(tag + "\t" + json.dumps(item))
        content = ("\n".join(records) + "\n").encode()
        source.write_bytes(content)
        self.report["clean_data_sha256"] = hashlib.sha256(content).hexdigest()
        result = train(self.directory, self.report)
        self.assertEqual(set(result["models"]), set(ALGORITHMS))
        for uid in (1, 2, 3):
            answer = predict(self.directory, self.report, uid, 2, "decision_tree")
            self.assertTrue(0 <= answer["positive_probability"] <= 1)
        from service.classification import _load_clean
        _, users, _ = _load_clean(self.directory, self.report)
        self.assertIsNone(users[1][0])
        self.assertIsNone(users[2][1])
        self.assertIsNone(users[3][2])

    def test_agent_tools_predict_both_without_inventing_results(self):
        from service.chat import ChatService
        from service.classification import ClassificationService
        train(self.directory, self.report)
        class FakeModel:
            model="test-model"
            configured=True
            def __init__(self):self.calls=[]
            def create(self,**kwargs):
                self.calls.append(kwargs)
                n=len(self.calls)
                def tool(name,args):
                    return {"id":f"r{n}","output":[{"type":"function_call","name":name,
                                  "arguments":json.dumps(args),"call_id":f"c{n}"}]}
                if n==1:
                    return tool("set_analysis_plan", {"goal":"对比两种分类算法","needed_data":["已训练的模型"],
                              "expected_result":"返回两种模型预测结果",
                              "steps":[{"tool":name,"purpose":"预测"} for name in (
                                  "predict_high_rating_logistic_regression","predict_high_rating_decision_tree")]})
                if n==2:return tool("predict_high_rating_logistic_regression",{"task_id":"","user_id":1,"movie_id":2})
                if n==3:return tool("predict_high_rating_decision_tree",{"task_id":"","user_id":1,"movie_id":2})
                if n==4:return tool("assess_analysis_goal",{"answerable":True,"reason":"有两项真实预测","limitations":[]})
                return {"id":f"r{n}","output":[{"type":"message","role":"assistant",
                         "content":[{"type":"output_text","text":"已取得两种模型的概率预测。"}]}]}
        tasks=TaskService(self.project)
        client=FakeModel()
        chat=ChatService(self.project,tasks,lambda:client,classifiers=ClassificationService(tasks))
        session=chat.create()
        value=chat.send(session["id"],"请用两个算法预测用户1对电影2评分是否达到4",self.task_id)
        message=value["message"]
        self.assertEqual(len(message["classifications"]),2)
        self.assertEqual(message["workflow"]["validation"]["status"],"passed")
        self.assertEqual([x["algorithm"] for x in message["classifications"]],list(ALGORITHMS))
        self.assertEqual(len(message["evidence"]),1)
        self.assertTrue(any(x["name"]=="predict_high_rating_logistic_regression" for x in client.calls[0]["tools"]))

    def test_no_same_timestamp_or_future_history_leak(self):
        users={1:("F",25,4)}
        movies={1:("Test (2000)", ("Drama",), 2000)}
        rows=[(1,1,5,1), (1,1,1,1), (1,1,5,2), (1,1,3,3), (1,1,4,4), (1,1,2,5)]
        train, validation, test, hist, counts = _split_features(rows,users,movies,(3,4))
        # Both first-timestamp events have no history. T=2 sees 2, T=3 sees 3.
        self.assertEqual(train[0][0,0],0.)
        self.assertEqual(train[0][1,0],0.)
        self.assertAlmostEqual(float(train[0][2,0]),__import__('math').log1p(2),places=5)
        self.assertEqual(hist.users[1][0],4)
        # Validation/test use the exact same frozen T1 history.
        self.assertEqual(validation[0][0].tolist(),test[0][0].tolist())

    def test_hash_or_data_version_mismatch_rejected(self):
        broken={**self.report,"clean_data_sha256":"0"*64}
        with self.assertRaisesRegex(ClassificationError,"SHA-256"):
            train(self.directory,broken)
        train(self.directory,self.report)
        changed={**self.report,"clean_data_version":"other"}
        with self.assertRaisesRegex(ClassificationError,"不一致"):
            training_report(self.directory,changed)

    def test_http_async_training_and_validation(self):
        server=create_server(self.project,host="127.0.0.1",port=0)
        import threading
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base=f"http://127.0.0.1:{server.server_port}/api/tasks/{self.task_id}/classification"
        def http(path, method="GET", body=None):
            data=json.dumps(body).encode() if body is not None else None
            req=Request(base+path, data=data, headers={"Content-Type":"application/json"} if data else {},method=method)
            with urlopen(req,timeout=20) as response:
                return response.status,json.load(response)
        self.assertEqual(http("/status")[1]["state"],"not_started")
        self.assertEqual(http("/train","POST",{})[0],202)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            status=http("/status")[1]
            if status["state"] in ("completed","failed"):
                break
            time.sleep(.05)
        self.assertEqual(status["state"],"completed",status)
        self.assertEqual(set(http("/report")[1]["models"]),set(ALGORITHMS))
        self.assertEqual(http("/predict","POST",{"algorithm":"decision_tree","user_id":1,"movie_id":2})[1]["status"],"completed")
        with self.assertRaises(HTTPError) as ctx:
            http("/predict","POST",{"algorithm":"decision_tree","user_id":-1,"movie_id":2})
        self.assertEqual(ctx.exception.code,422)


if __name__ == "__main__": unittest.main()
