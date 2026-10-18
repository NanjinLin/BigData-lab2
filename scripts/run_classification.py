#!/usr/bin/env python3
"""Train both classification algorithms from a verified Hadoop-cleaned task."""
import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from service.classification import ClassificationError, train
from service.tasks import TaskError, TaskService


def main():
    parser = argparse.ArgumentParser(description="Train MovieLens high-rating classifiers")
    parser.add_argument("--task-id", help="Completed Hadoop task ID (uses most recent clean table when omitted)")
    args = parser.parse_args()
    tasks = TaskService(PROJECT)
    try:
        task_id = args.task_id
        if not task_id:
            choices = [t for t in tasks.list_tasks() if t.get("state") == "completed" and t.get("clean_table_available")]
            if not choices:
                raise ClassificationError("找不到带完整 cleaned.tsv 的已完成清洗任务。")
            task_id = choices[0]["task_id"]
        report = tasks.report(task_id)
        print(f"训练任务: {task_id}", flush=True)
        saved = train(tasks._directory(task_id), report, notify=lambda stage: print(f"阶段: {stage}", flush=True))
        print(json.dumps({"status": saved["status"], "task_id": task_id,
                          "experiment_id": saved["experiment_id"],
                          "models": {name: {"version": v["model_version"],
                                              "parameters": v["parameters"],
                                              "validation": v["validation"], "test": v["test"]}
                                     for name, v in saved["models"].items()},
                          "report": str(tasks._directory(task_id) / "classification" / "report.json")},
                         ensure_ascii=False, indent=2))
    except (TaskError, ClassificationError, ImportError, OSError) as error:
        print(f"分类训练失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
