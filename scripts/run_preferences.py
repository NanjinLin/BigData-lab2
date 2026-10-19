#!/usr/bin/env python3
"""Run all real clustering and reduction experiments over an existing clean task."""
import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from service.preferences import DEFAULTS, PreferenceError, train
from service.tasks import TaskError, TaskService


def main():
    parser = argparse.ArgumentParser(description="MovieLens iteration-two preference analysis")
    parser.add_argument("--task-id")
    for key, default in DEFAULTS.items():
        parser.add_argument("--" + key.replace("_", "-"), default=default,
                            type=float if key in ("eps", "retention") else int)
    args = vars(parser.parse_args())
    task_id = args.pop("task_id")
    tasks = TaskService(PROJECT)
    try:
        if not task_id:
            candidates = [t for t in tasks.list_tasks() if t.get("state") == "completed" and t.get("clean_table_available")]
            if not candidates:
                raise PreferenceError("没有可用完整净表。请恢复已完成任务或执行Hadoop清洗。")
            task_id = candidates[0]["task_id"]
        result = train(tasks._directory(task_id), tasks.report(task_id), args,
                       notify=lambda stage: print(f"stage: {stage}", flush=True))
        print(json.dumps({k: result[k] for k in ("task_id", "status", "experiment_id", "eligible_users", "excluded_users")},
                         ensure_ascii=False, indent=2))
        return 0
    except (TaskError, PreferenceError, ImportError, OSError) as exc:
        print(f"画像分析失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
