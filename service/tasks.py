"""Single-flight task execution and versioned artifact access."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .agent import CUSTOM_TOOL, RUN_TOOL, DataGovernanceAgent, RULE_VERSION
from .model import ModelError
from .rules import RuleConfigError, build_rules, snapshot_bytes


TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def summarize_failure(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "流水线退出但没有输出错误详情，请查看服务日志。"
    detail = lines[-1]
    if detail.startswith(("RuntimeError: ", "FileNotFoundError: ")):
        detail = detail.partition(": ")[2]
    return detail[-500:]


class TaskError(Exception):
    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code


class TaskService:
    def __init__(self, project: Path, runner: Callable[[str], int] | None = None,
                 agent: DataGovernanceAgent | None = None) -> None:
        self.project = project.resolve()
        self.runner = runner or self._run_pipeline
        self.agent = agent or DataGovernanceAgent()
        self.lock = threading.Lock()
        self.active: str | None = None
        self.pending: dict[str, dict] = {}

    @property
    def lock_dir(self) -> Path:
        return self.project / "runs" / ".active-task"

    def _claim_lock(self, task_id: str) -> None:
        self.lock_dir.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self.lock_dir.mkdir()
            except FileExistsError:
                owner_file = self.lock_dir / "task_id"
                owner = owner_file.read_text(encoding="ascii").strip() if owner_file.is_file() else ""
                state_file = self.project / "runs" / owner / "status.json" if TASK_ID.fullmatch(owner) else None
                state = ""
                if state_file and state_file.is_file():
                    try:
                        state = json.loads(state_file.read_text(encoding="utf-8")).get("state", "")
                    except ValueError:
                        pass
                if state in ("completed", "failed"):
                    try:
                        owner_file.unlink()
                        self.lock_dir.rmdir()
                    except OSError:
                        pass
                    continue
                raise TaskError(f"任务 {owner or '未知'} 可能仍在 Hadoop 中执行；请核查状态后再提交。", 409)
            else:
                (self.lock_dir / "task_id").write_text(task_id, encoding="ascii")
                return
        raise TaskError("无法取得 Hadoop 任务锁，请检查 runs/.active-task。", 409)

    def _release_lock(self, task_id: str) -> None:
        owner_file = self.lock_dir / "task_id"
        try:
            if owner_file.read_text(encoding="ascii").strip() == task_id:
                owner_file.unlink()
                self.lock_dir.rmdir()
        except OSError:
            pass

    def _directory(self, task_id: str) -> Path:
        if not TASK_ID.fullmatch(task_id):
            raise TaskError("无效的任务 ID。", 400)
        return self.project / "runs" / task_id

    def start(self, prompt: str) -> dict:
        with self.lock:
            if self.active:
                raise TaskError(f"任务 {self.active} 正在运行；请等待完成后再提交。", 409)
        try:
            decision = self.agent.plan(prompt)
        except ModelError as error:
            raise TaskError(str(error), error.code) from error
        if not decision["accepted"]:
            raise TaskError(decision["reason"], 422)
        tool = decision.get("tool")
        options = decision.get("options")
        if tool == RUN_TOOL["name"]:
            if options is not None or decision.get("rule_version") != RULE_VERSION:
                raise TaskError("模型选择了未登记的任务工具或规则。", 502)
        elif tool == CUSTOM_TOOL["name"]:
            if not isinstance(options, dict):
                raise TaskError("模型没有提供可执行的自定义规则参数。", 502)
        else:
            raise TaskError("模型选择了未登记的任务工具或规则。", 502)
        try:
            rules = build_rules(options)
            rule_data = snapshot_bytes(rules)
        except RuleConfigError as error:
            raise TaskError(f"规则参数无效：{error}", 502) from error
        if decision.get("rule_version") != rules["rule_version"]:
            raise TaskError("模型规则版本与服务端登记的配置不一致。", 502)
        rule_sha256 = hashlib.sha256(rule_data).hexdigest()
        with self.lock:
            if self.active:
                raise TaskError(f"任务 {self.active} 正在运行；请等待完成后再提交。", 409)
            task_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
            self._claim_lock(task_id)
            self.active = task_id
            self.pending[task_id] = {
                "task_id": task_id, "state": "queued", "stage": "waiting_for_worker",
                "started_at": utc_now(), "rule_version": rules["rule_version"],
            }
            metadata = {"model": decision.get("model"),
                        "model_response_id": decision.get("model_response_id"),
                        "tool": tool, "rule_version": rules["rule_version"],
                        "options": options, "rule_sha256": rule_sha256}
            rules_path = self.project / "runs" / f"{task_id}.rules.json"
            metadata_path = self.project / "runs" / f"{task_id}.agent.json"
            try:
                rules_path.write_bytes(rule_data)
                metadata_path.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as error:
                rules_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                self.pending.pop(task_id, None)
                self.active = None
                self._release_lock(task_id)
                raise TaskError("无法保存模型决策记录，任务未启动。", 500) from error
        threading.Thread(target=self._work, args=(task_id,), daemon=True).start()
        return self.status(task_id)

    def _with_agent(self, task_id: str, status: dict) -> dict:
        path = self.project / "runs" / f"{task_id}.agent.json"
        if path.is_file():
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return status
            return {**status, "agent": metadata}
        return status

    def answer(self, task_id: str, question: str) -> dict:
        report = self.report(task_id)
        try:
            result = self.agent.answer(question, report)
        except ModelError as error:
            raise TaskError(str(error), error.code) from error
        return {"task_id": task_id, **result,
                "evidence": f"runs/{task_id}/report.json"}

    def _work(self, task_id: str) -> None:
        with self.lock:
            self.pending[task_id]["state"] = "running"
            self.pending[task_id]["stage"] = "starting_pipeline"
        try:
            exit_code = self.runner(task_id)
            directory = self._directory(task_id)
            status_path = directory / "status.json"
            report_path = directory / "report.json"
            if exit_code != 0 or not status_path.is_file() or not report_path.is_file():
                log_path = self.project / "runs" / f"{task_id}.service.log"
                detail = summarize_failure(log_path.read_text(encoding="utf-8", errors="replace")) if log_path.is_file() else f"流水线退出码 {exit_code}。"
                if status_path.is_file():
                    prior = json.loads(status_path.read_text(encoding="utf-8"))
                    detail = prior.get("error", detail)
                self._record_failure(task_id, detail)
            else:
                actual = json.loads(status_path.read_text(encoding="utf-8"))
                if actual.get("state") != "completed":
                    self._record_failure(task_id, actual.get("error", "流水线未报告完成。"))
        except Exception as error:
            self._record_failure(task_id, f"无法启动或执行流水线：{error}")
        finally:
            with self.lock:
                self._release_lock(task_id)
                self.active = None
                self.pending.pop(task_id, None)

    def _record_failure(self, task_id: str, error: str) -> None:
        directory = self._directory(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "status.json"
        prior = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        prior.update({"task_id": task_id, "state": "failed", "stage": prior.get("stage", "startup"),
                      "updated_at": utc_now(), "error": error})
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(prior, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _run_pipeline(self, task_id: str) -> int:
        metadata = json.loads((self.project / "runs" / f"{task_id}.agent.json").read_text(encoding="utf-8"))
        rule_sha256 = metadata["rule_sha256"]
        if os.name == "nt":
            project_path = os.environ.get("ML1M_WSL_PROJECT")
            if not project_path:
                drive = self.project.drive[0].lower()
                rest = self.project.as_posix()[2:]
                project_path = f"/mnt/{drive}{rest}"
            distro = os.environ.get("ML1M_WSL_DISTRO")
            prefix = ["wsl"] + (["-d", distro] if distro else []) + ["--"]
            launcher = f"{project_path}/scripts/run_from_wsl.sh"
            command = prefix + ["bash", launcher, task_id,
                                os.environ.get("ML1M_AUTO_START_HADOOP", "1"),
                                os.environ.get("ML1M_HADOOP_HOME", ""),
                                os.environ.get("ML1M_HADOOP_CONF_DIR", ""),
                                os.environ.get("ML1M_HDFS_ROOT", ""),
                                os.environ.get("ML1M_HADOOP_USER", ""), rule_sha256]
        else:
            command = ["bash", str(self.project / "scripts" / "run_from_wsl.sh"), task_id,
                       os.environ.get("ML1M_AUTO_START_HADOOP", "1"), "", "", "", "", rule_sha256]
        timeout = int(os.environ.get("ML1M_TASK_TIMEOUT_SECONDS", "3600"))
        # The driver requires a fresh directory. Its own status file starts after input checks.
        # Keep the service log outside that directory until the process exits.
        (self.project / "runs").mkdir(parents=True, exist_ok=True)
        log_path = self.project / "runs" / f"{task_id}.service.log"
        try:
            with log_path.open("w", encoding="utf-8") as log:
                completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                           timeout=timeout, check=False)
            return completed.returncode
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"流水线超过 {timeout} 秒超时；检查 YARN 作业状态。") from error

    def status(self, task_id: str) -> dict:
        directory = self._directory(task_id)
        path = directory / "status.json"
        if path.is_file():
            result = json.loads(path.read_text(encoding="utf-8"))
            if result.get("state") in ("created", "running") and self.active != task_id:
                result = {**result, "state": "unknown", "error": "服务重启后无法确认原任务状态，请检查 Hadoop 与任务日志。"}
            return self._with_agent(task_id, result)
        if (directory / "report.json").is_file():
            report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
            if report.get("status") == "completed" and report.get("task_id") == task_id:
                return self._with_agent(task_id, {"task_id": task_id, "state": "completed", "stage": "historical_report",
                                                  "updated_at": report.get("completed_at"), "historical": True})
        with self.lock:
            if task_id in self.pending:
                return self._with_agent(task_id, self.pending[task_id].copy())
        raise TaskError("任务不存在。", 404)

    def report(self, task_id: str) -> dict:
        if self.status(task_id)["state"] != "completed":
            raise TaskError("任务尚未完成，正式报告不可用。", 409)
        path = self._directory(task_id) / "report.json"
        if not path.is_file():
            raise TaskError("任务标记已完成，但正式报告文件缺失。", 409)
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise TaskError("正式报告无法读取或 JSON 已损坏。", 409) from error
        if report.get("task_id") != task_id or report.get("status") != "completed":
            raise TaskError("报告与任务 ID 或状态不一致。", 409)
        return report

    def samples(self, task_id: str, limit: int = 5) -> dict:
        report = self.report(task_id)
        path = self._directory(task_id) / "cleaned.tsv"
        if not path.is_file():
            return {"state": "unavailable", "reason": "本机没有该任务的完整净表；请从原运行机器转交或重新执行 Hadoop 任务。",
                    "clean_data_version": report["clean_data_version"], "rows": []}
        rows: list[dict] = []
        with path.open(encoding="utf-8") as source:
            for line in source:
                tag, sep, payload = line.partition("\t")
                if sep and tag == "C":
                    rows.append(json.loads(payload))
                    if len(rows) >= limit:
                        break
        return {"state": "available", "clean_data_version": report["clean_data_version"], "rows": rows}

    def list_tasks(self) -> list[dict]:
        root = self.project / "runs"
        if not root.is_dir():
            return []
        found = []
        for directory in sorted(root.iterdir(), reverse=True):
            if directory.is_dir() and TASK_ID.fullmatch(directory.name):
                try:
                    found.append(self.status(directory.name))
                except (TaskError, OSError, ValueError):
                    continue
        return found[:20]
