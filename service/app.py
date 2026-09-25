"""Dependency-free local HTTP API and static frontend."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .agent import DataGovernanceAgent
from .tasks import TaskError, TaskService


ROOT = Path(__file__).resolve().parent.parent
PUBLIC = Path(__file__).resolve().parent / "static"


def create_server(project: Path = ROOT, host: str = "127.0.0.1", port: int = 8765,
                  runner=None, agent: DataGovernanceAgent | None = None) -> ThreadingHTTPServer:
    service = TaskService(project, runner=runner, agent=agent)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: bytes, content_type: str, download: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if download:
                self.send_header("Content-Disposition", 'attachment; filename="report.json"')
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, code: int, value: dict | list, download: bool = False) -> None:
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
                parts = route.strip("/").split("/")
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
                files = {"/": ("index.html", "text/html; charset=utf-8"),
                         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                         "/style.css": ("style.css", "text/css; charset=utf-8")}
                if route in files:
                    name, content_type = files[route]
                    self._send(200, (PUBLIC / name).read_bytes(), content_type)
                    return
                raise TaskError("页面或接口不存在。", 404)
            except TaskError as error:
                self._json(error.code, {"error": str(error)})

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
                parts = route.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "ask":
                    question = body.get("question")
                    if not isinstance(question, str) or not question.strip() or len(question) > 1000:
                        raise TaskError("请输入不超过 1000 字的追问。", 400)
                    self._json(200, service.answer(parts[2], question))
                    return
                raise TaskError("接口不存在。", 404)
            except TaskError as error:
                self._json(error.code, {"error": str(error)})

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
