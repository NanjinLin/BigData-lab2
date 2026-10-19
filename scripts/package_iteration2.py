#!/usr/bin/env python3
"""Build a portable source/data/model submission after verifying all results."""
import argparse
import json
import os
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from scripts.verify_iteration2 import verify


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--output", type=Path, default=PROJECT.parent / "交付" / "迭代二_完整提交包.zip")
    args = parser.parse_args()
    from threadpoolctl import threadpool_limits
    with threadpool_limits(limits=1):
        verification = verify(PROJECT, args.task_id)
    directory = PROJECT / "runs" / args.task_id
    files = []
    excluded = {".git", ".venv", "__pycache__", "runs", "tmp", "output", "node_modules", ".pytest_cache"}
    for current, dirs, names in os.walk(PROJECT):
        dirs[:] = [d for d in dirs if d not in excluded and not d.startswith(".test-")]
        for name in names:
            if name.startswith(".env") or name.endswith((".pyc", ".pyo", ".log", ".zip")):
                continue
            files.append(Path(current) / name)
    for name in ("report.json", "cleaned.tsv", "rules.json", "score_before.tsv", "score_after.tsv", "status.json"):
        if (directory / name).is_file():
            files.append(directory / name)
    pref = json.loads((directory / "preferences" / "report.json").read_text(encoding="utf-8"))
    cls = json.loads((directory / "classification" / "report.json").read_text(encoding="utf-8"))
    files += [directory / "preferences" / "report.json", directory / "classification" / "report.json"]
    files += list((directory / "preferences" / pref["experiment_id"]).glob("*"))
    files += list((directory / "classification" / "models" / cls["experiment_id"]).glob("*"))
    history = directory / "classification" / "reports" / f"{cls['experiment_id']}.json"
    if history.is_file():
        files.append(history)
    # Preserve the additional actual 90%-variance experiment referenced in the report.
    for path in (directory / "preferences").glob("pref-*/report.json"):
        result = json.loads(path.read_text(encoding="utf-8"))
        if (result.get("implementation_sha256") == pref["implementation_sha256"]
                and result.get("source") == pref["source"] and result.get("parameters", {}).get("retention") == .9):
            files += list(path.parent.glob("*"))
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_suffix(".pending.zip")
    with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(set(files)):
            if path.is_file():
                archive.write(path, "MovieLens-iteration2/" + path.relative_to(PROJECT).as_posix())
        archive.writestr("MovieLens-iteration2/交付校验记录.json", json.dumps(verification, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(pending) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"压缩包CRC校验失败: {bad}")
        count = len(archive.namelist())
    pending.replace(output)
    print(json.dumps({"output": str(output), "files": count, "bytes": output.stat().st_size,
                      "verification": verification["status"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
