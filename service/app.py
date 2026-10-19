"""Dependency-free local HTTP API and static frontend."""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .classification import ClassificationError, ClassificationService
from .preferences import PreferenceError, PreferenceService
from .agent import DataGovernanceAgent
from .chat import ChatService
from .model import ModelError, ResponsesClient, normalize_base_url
from .tasks import TaskError, TaskService


ROOT = Path(__file__).resolve().parent.parent
PUBLIC = Path(__file__).resolve().parent / "static"


def create_server(project: Path = ROOT, host: str = "127.0.0.1", port: int = 8765,
                  runner=None, agent: DataGovernanceAgent | None = None) -> ThreadingHTTPServer:
    service = TaskService(project, runner=runner, agent=agent)
    classifiers = ClassificationService(service)
    preferences = PreferenceService(service)
    settings_lock = threading.RLock()
    model_client = getattr(service.agent, "model", None)
    chat = ChatService(project, service, lambda: model_client, classifiers=classifiers, preferences=preferences)

    def model_settings() -> dict:
        with settings_lock:
            return {"model": model_client.model if model_client else "",
                    "base_url": model_client.base_url if model_client else "https://api.openai.com/v1",
                    "configured": bool(model_client and model_client.configured),
                    "key_present": bool(model_client and model_client.api_key),
                    "provider": "Responses-compatible API",
                    "key_storage": "server_memory"}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: bytes, content_type: str, download: bool | str = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if download:
                filename = download if isinstance(download, str) else "report.json"
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, code: int, value: dict | list, download: bool | str = False) -> None:
            self._send(code, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", download)

        def _body(self) -> dict:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise TaskError("Content-Length 必须是整数。", 400) from error
            if size < 1 or size > 16384:
                raise TaskError("请求体必须是小于 16 KB 的 JSON。", 400)
            try:
                value = json.loads(self.rfile.read(size))
            except (ValueError, UnicodeError) as error:
                raise TaskError("请求体不是有效 JSON。", 400) from error
            if not isinstance(value, dict):
                raise TaskError("请求体必须是 JSON 对象。", 400)
            return value

        def do_GET(self) -> None:
            route = urlsplit(self.path).path
            try:
                if route == "/api/tasks":
                    self._json(200, service.list_tasks())
                    return
                if route == "/api/agent":
                    self._json(200, service.agent.configuration)
                    return
                if route == "/api/model-config":
                    self._json(200, model_settings())
                    return
                if route == "/api/conversations":
                    self._json(200, chat.list())
                    return
                parts = route.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "activity":
                    self._json(200, chat.activity(parts[2]))
                    return
                if len(parts) == 3 and parts[:2] == ["api", "conversations"]:
                    self._json(200, chat.get(parts[2]))
                    return
                if len(parts) >= 3 and parts[:2] == ["api", "tasks"]:
                    task_id = parts[2]
                    if len(parts) == 3:
                        self._json(200, service.status(task_id))
                        return
                    if len(parts) == 4 and parts[3] == "report":
                        self._json(200, service.report(task_id), "download=1" in urlsplit(self.path).query)
                        return
                    if len(parts) == 4 and parts[3] == "samples":
                        self._json(200, service.samples(task_id))
                        return
                    if len(parts) == 5 and parts[3] == "classification":
                        if parts[4] == "status":
                            self._json(200, classifiers.status(task_id))
                            return
                        if parts[4] == "report":
                            self._json(200, classifiers.result(task_id),
                                       "classification-report.json" if "download=1" in urlsplit(self.path).query else False)
                            return
                    if len(parts) >= 5 and parts[3] == "preferences":
                        if len(parts) == 5 and parts[4] == "status":
                            self._json(200, preferences.status(task_id))
                            return
                        if len(parts) == 5 and parts[4] == "report":
                            self._json(200, preferences.result(task_id),
                                       "preferences-report.json" if "download=1" in urlsplit(self.path).query else False)
                            return
                        if len(parts) == 6 and parts[4] == "data":
                            self._json(200, preferences.data(task_id, parts[5]),
                                       parts[5] if "download=1" in urlsplit(self.path).query else False)
                            return
                    if len(parts) == 5 and parts[3] == "analyses":
                        self._json(200, service.analysis_result(task_id, parts[4]),
                                   "statistics.json" if "download=1" in urlsplit(self.path).query else False)
                        return
                files = {"/": ("index.html", "text/html; charset=utf-8"),
                         "/preferences.js": ("preferences.js", "text/javascript; charset=utf-8"),
                         "/preferences.css": ("preferences.css", "text/css; charset=utf-8"),
                         "/markdown-it.min.js": ("vendor/markdown-it.min.js", "text/javascript; charset=utf-8"),
                         "/markdown.js": ("markdown.js", "text/javascript; charset=utf-8"),
                         "/workflow.js": ("workflow.js", "text/javascript; charset=utf-8"),
                         "/workflow.css": ("workflow.css", "text/css; charset=utf-8"),
                         "/charts.js": ("charts.js", "text/javascript; charset=utf-8"),
                         "/chart-renderers.js": ("chart-renderers.js", "text/javascript; charset=utf-8"),
                         "/charts.css": ("charts.css", "text/css; charset=utf-8"),
                         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                         "/style.css": ("style.css", "text/css; charset=utf-8")}
                if route == "/classification.js":
                    self._send(200, (PUBLIC / "classification.js").read_bytes(), "text/javascript; charset=utf-8")
                    return
                if route == "/classification.css":
                    self._send(200, (PUBLIC / "classification.css").read_bytes(), "text/css; charset=utf-8")
                    return
                if route in files:
                    name, content_type = files[route]
                    self._send(200, (PUBLIC / name).read_bytes(), content_type)
                    return
                raise TaskError("页面或接口不存在。", 404)
            except TaskError as error:
                self._json(error.code, {"error": str(error)})
            except (ClassificationError, PreferenceError) as error:
                self._json(422, {"error": str(error)})

        def do_POST(self) -> None:
            route = urlsplit(self.path).path
            try:
                body = self._body()
                if route == "/api/tasks":
                    prompt = body.get("prompt")
                    if not isinstance(prompt, str):
                        raise TaskError("prompt 必须是字符串。", 400)
                    self._json(202, service.start(prompt))
                    return
                if route == "/api/conversations":
                    self._json(201, chat.create())
                    return
                parts = route.strip("/").split("/")
                if len(parts) == 5 and parts[:2] == ["api", "tasks"] and parts[3] == "preferences":
                    if parts[4] == "train":
                        self._json(202, preferences.start(parts[2], body))
                        return
                    if parts[4] == "query":
                        if set(body) - {"category", "algorithm", "user_ids", "parameters", "comparison_algorithm"} or not {"category", "algorithm"} <= set(body):
                            raise TaskError("查询需提供category和algorithm，可选user_ids及parameters。", 400)
                        self._json(200, preferences.query(parts[2], **body))
                        return
                if len(parts) == 5 and parts[:2] == ["api", "tasks"] and parts[3] == "classification":
                    if parts[4] == "train":
                        if body:
                            raise TaskError("训练接口不接受额外参数。", 400)
                        self._json(202, classifiers.start(parts[2]))
                        return
                    if parts[4] == "predict":
                        if (set(body) != {"user_id", "movie_id", "algorithm"}
                                or type(body["user_id"]) is not int or type(body["movie_id"]) is not int
                                or body["algorithm"] not in ("logistic_regression", "decision_tree")):
                            raise TaskError("预测参数需要 user_id、movie_id 和 algorithm。", 400)
                        self._json(200, classifiers.predict(parts[2], body["user_id"], body["movie_id"], body["algorithm"]))
                        return
                if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "charts":
                    self._json(200, service.chart_data(parts[2], body.get("kind")))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "analysis":
                    self._json(200, service.analyze(parts[2], body))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "messages":
                    self._json(200, chat.send(parts[2], body.get("message"), body.get("task_id")))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "ask":
                    question = body.get("question")
                    if not isinstance(question, str) or not question.strip() or len(question) > 1000:
                        raise TaskError("请输入不超过 1000 字的追问。", 400)
                    self._json(200, service.answer(parts[2], question))
                    return
                raise TaskError("接口不存在。", 404)
            except TaskError as error:
                self._json(error.code, {"error": str(error)})
            except (ClassificationError, PreferenceError) as error:
                self._json(422, {"error": str(error)})

        def do_PUT(self) -> None:
            nonlocal model_client
            if urlsplit(self.path).path != "/api/model-config":
                self._json(404, {"error": "接口不存在。"})
                return
            try:
                body = self._body()
                model_name = body.get("model")
                base_url = body.get("base_url")
                api_key = body.get("api_key", "")
                if not isinstance(model_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}", model_name):
                    raise TaskError("模型名称格式无效。", 400)
                if not isinstance(api_key, str) or len(api_key) > 4096 or "\n" in api_key or "\r" in api_key:
                    raise TaskError("API Key 格式无效。", 400)
                normalized = normalize_base_url(base_url)
                with settings_lock:
                    key = api_key or (model_client.api_key if model_client else "")
                    if not key:
                        raise TaskError("请填写 API Key。", 400)
                    model_client = ResponsesClient(api_key=key, model=model_name, base_url=normalized)
                    if isinstance(service.agent, DataGovernanceAgent):
                        service.agent.model = model_client
                self._json(200, model_settings())
            except (TaskError, ModelError) as error:
                self._json(error.code, {"error": str(error)})

        def do_DELETE(self) -> None:
            nonlocal model_client
            if urlsplit(self.path).path != "/api/model-config":
                self._json(404, {"error": "接口不存在。"})
                return
            with settings_lock:
                if model_client:
                    model_client = ResponsesClient(api_key="", model=model_client.model,
                                                   base_url=model_client.base_url)
                    if isinstance(service.agent, DataGovernanceAgent):
                        service.agent.model = model_client
            self._json(200, model_settings())

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="MovieLens iteration-one local service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(ROOT, args.host, args.port)
    print(f"MovieLens service: http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
