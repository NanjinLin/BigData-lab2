"""Check that the submitted cleaned data matches the selected Hadoop report."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TASK_ID = "20260929151428Z-submit"
RUN = ROOT / "runs" / TASK_ID
DIMENSIONS = {"Accurate", "Complete", "Unique", "Up-to-date", "Consistent"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    errors: list[str] = []
    try:
        report = json.loads((RUN / "report.json").read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "raw_data_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"无法读取报告或原始数据清单：{error}")
        return 1

    if report.get("task_id") != TASK_ID or report.get("status") != "completed":
        errors.append("任务 ID 或完成状态与选定任务不一致")
    if report.get("raw_data_version") != manifest.get("data_version"):
        errors.append("报告中的原始数据版本与清单不一致")
    status_path = RUN / "status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("task_id") != TASK_ID or status.get("state") != "completed":
            errors.append("任务状态文件与报告不一致")
        if len(status.get("jobs", {})) != 4:
            errors.append("任务状态文件缺少四个 Hadoop 作业")
    except (OSError, ValueError) as error:
        errors.append(f"无法读取任务状态文件：{error}")
    if not report.get("T1") or not report.get("T2"):
        errors.append("报告缺少 T1/T2")
    for stage in ("quality_before", "quality_after"):
        scores = report.get(stage, {}).get("overall", {})
        if not DIMENSIONS.issubset(scores):
            errors.append(f"报告的 {stage} 缺少五维评分")

    for name in ("score_before.tsv", "score_after.tsv"):
        if not (RUN / name).is_file():
            errors.append(f"缺少评分输出：{name}")

    rules = ROOT / "config" / "quality_rules_v1.json"
    if not rules.is_file() or sha256(rules) != report.get("rule_sha256"):
        errors.append("默认规则文件的 SHA-256 与报告不一致")
    rules_snapshot = RUN / "rules.json"
    if not rules_snapshot.is_file() or sha256(rules_snapshot) != report.get("rule_sha256"):
        errors.append("本次任务的规则快照与报告不一致")

    code_hash = hashlib.sha256()
    for name in ("mapper.py", "quality.py", "score_reducer.py", "clean_reducer.py"):
        source = ROOT / "hadoop_jobs" / name
        if not source.is_file():
            errors.append(f"缺少 Hadoop 源码：{name}")
            break
        code_hash.update(source.read_bytes())
    else:
        if code_hash.hexdigest() != report.get("code_sha256"):
            errors.append("Hadoop 源码的 SHA-256 与本次任务报告不一致")

    for name, entry in manifest.get("files", {}).items():
        source = ROOT / entry["path"]
        if not source.is_file() or source.stat().st_size != entry["size_bytes"]:
            errors.append(f"原始输入缺失或大小不符：{name}")
        elif sha256(source).upper() != entry["sha256"]:
            errors.append(f"原始输入 SHA-256 不符：{name}")

    cleaned = RUN / "cleaned.tsv"
    if not cleaned.is_file():
        errors.append(f"缺少清洗后数据：{cleaned}")
    elif sha256(cleaned) != report.get("clean_data_sha256"):
        errors.append("cleaned.tsv 的 SHA-256 与报告不一致，可能属于其他任务")
    else:
        with cleaned.open("rb") as source:
            clean_rows = sum(line.startswith(b"C\t") for line in source)
        expected_rows = sum(entry["output"] for entry in report["cleaning"]["tables"].values())
        if clean_rows != expected_rows:
            errors.append(f"净表行数不符：{clean_rows} != {expected_rows}")

    extra_runs = [path.name for path in (ROOT / "runs").iterdir()
                  if path.is_dir() and path.name != TASK_ID]
    if extra_runs:
        errors.append("提交包仍含其他任务目录：" + ", ".join(extra_runs))

    if errors:
        for error in errors:
            print("未通过：" + error)
        return 1
    print("文件校验通过：净表、报告、规则及原始输入相互匹配。")
    print("注意：此脚本不验证云端模型调用或 Agent 自动选工具；Hadoop 源码字节已与本次任务报告核对。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
