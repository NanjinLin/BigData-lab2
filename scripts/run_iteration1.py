"""Run the versioned MovieLens cleaning and scoring pipeline on Hadoop YARN.

This driver only verifies inputs, submits Hadoop Streaming jobs, and packages
their outputs. The cleaning rules and all five score calculations run inside
Hadoop tasks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))
from service.rules import RuleConfigError, validate_rules  # noqa: E402

DEFAULT_HADOOP_HOME = Path("/opt/hadoop-3.5.0")
if not DEFAULT_HADOOP_HOME.is_dir():
    DEFAULT_HADOOP_HOME = Path.home() / ".local/opt/hadoop-3.5.0"
HADOOP_HOME = Path(os.environ.get("ML1M_HADOOP_HOME", str(DEFAULT_HADOOP_HOME)))
HADOOP = HADOOP_HOME / "bin" / "hadoop"
HDFS = HADOOP_HOME / "bin" / "hdfs"
STREAMING = HADOOP_HOME / "share/hadoop/tools/lib/hadoop-streaming-3.5.0.jar"
DEFAULT_CONF_DIR = Path("/home/linxinan/ml1m-hadoop-conf")
if not DEFAULT_CONF_DIR.is_dir():
    DEFAULT_CONF_DIR = Path.home() / ".local/share/ml1m-hadoop/conf"
CONF_DIR = os.environ.get("ML1M_HADOOP_CONF_DIR", str(DEFAULT_CONF_DIR))
HDFS_ROOT = os.environ.get("ML1M_HDFS_ROOT", f"/user/{Path.home().name}/ml1m/tasks")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_rules_file(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RuntimeError("Rule snapshot hash must be a lowercase SHA-256 digest")
    if not path.is_file() or sha256(path) != expected_sha256:
        raise RuntimeError("Rule snapshot hash mismatch or file missing")
    try:
        rules = json.loads(path.read_text(encoding="utf-8"))
        return validate_rules(rules)
    except (OSError, ValueError, RuleConfigError) as error:
        raise RuntimeError(f"Invalid rule snapshot: {error}") from error


def verify_input(rule_path: Path | None = None, rule_sha256: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads((PROJECT / "raw_data_manifest.json").read_text(encoding="utf-8"))
    selected_path = rule_path or PROJECT / "config/quality_rules_v1.json"
    rules = verify_rules_file(selected_path, rule_sha256 or sha256(selected_path))
    if rules["raw_data_version"] != manifest["data_version"]:
        raise RuntimeError("Rule configuration is registered for a different raw data version")
    for name, entry in manifest["files"].items():
        path = PROJECT / entry["path"]
        if not path.is_file():
            raise RuntimeError(f"Missing raw input: {path}")
        if path.stat().st_size != entry["size_bytes"] or sha256(path).upper() != entry["sha256"]:
            raise RuntimeError(f"Raw input checksum or size mismatch: {name}")
    if not HADOOP.is_file() or not STREAMING.is_file():
        raise RuntimeError("Hadoop 3.5.0 or Hadoop Streaming is not installed at the registered path")
    return manifest, rules


class Pipeline:
    def __init__(self, task_id: str, manifest: dict[str, Any], rules: dict[str, Any],
                 rule_path: Path, expected_rule_sha256: str) -> None:
        self.task_id = task_id
        self.manifest = manifest
        self.rules = rules
        rule_bytes = rule_path.read_bytes()
        if hashlib.sha256(rule_bytes).hexdigest() != expected_rule_sha256:
            raise RuntimeError("Rule snapshot hash changed before pipeline start")
        self.original_rule_path = rule_path
        self.expected_rule_sha256 = expected_rule_sha256
        self.run_dir = PROJECT / "runs" / task_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.rule_path = self.run_dir / "rules.json"
        self.rule_path.write_bytes(rule_bytes)
        self.hdfs_root = f"{HDFS_ROOT}/{task_id}"
        self.jobs: dict[str, dict[str, Any]] = {}
        self.env = os.environ.copy()
        self.env["HADOOP_CONF_DIR"] = CONF_DIR
        self.stage = "created"
        self.started_at = now()
        self.write_status("created")

    def assert_snapshot_unchanged(self) -> None:
        if (sha256(self.original_rule_path) != self.expected_rule_sha256
                or sha256(self.rule_path) != self.expected_rule_sha256):
            raise RuntimeError("Rule snapshot hash changed during pipeline execution")

    def write_status(self, state: str, **extra: Any) -> None:
        payload = {
            "task_id": self.task_id,
            "state": state,
            "stage": self.stage,
            "started_at": self.started_at,
            "updated_at": now(),
            "jobs": self.jobs,
            **extra,
        }
        temporary = self.run_dir / "status.json.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.run_dir / "status.json")

    def command(self, stage: str, args: list[str], *, capture: bool = False) -> str:
        self.assert_snapshot_unchanged()
        self.stage = stage
        self.write_status("running")
        print(f"[{now()}] {stage}", flush=True)
        log_path = self.run_dir / f"{stage}.log"
        completed = subprocess.run(
            args,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        log_path.write_text(completed.stdout, encoding="utf-8")
        self.assert_snapshot_unchanged()
        if completed.returncode:
            tail = "\n".join(completed.stdout.splitlines()[-20:])
            raise RuntimeError(f"{stage} failed (exit {completed.returncode}):\n{tail}")
        return completed.stdout if capture else ""

    def stream_job(
        self,
        stage: str,
        input_path: str,
        output_path: str,
        mapper_mode: str,
        reducer: str | None,
    ) -> None:
        code_dir = PROJECT / "hadoop_jobs"
        files = [code_dir / "mapper.py"]
        if reducer == "score":
            files += [code_dir / "score_reducer.py", code_dir / "quality.py"]
        elif reducer == "clean":
            files += [code_dir / "clean_reducer.py", code_dir / "quality.py"]
        if reducer is not None:
            files.append(f"{self.rule_path}#quality_rules_runtime.json")
        arguments = [
            str(HADOOP), "jar", str(STREAMING),
            "-D", f"mapreduce.job.name=ml1m-{self.task_id}-{stage}",
            "-D", "mapreduce.input.fileinputformat.split.minsize=16777216",
            "-D", "mapreduce.task.timeout=900000",
            "-D", "mapreduce.map.memory.mb=1536",
            "-D", "mapreduce.reduce.memory.mb=2048",
            "-D", "mapreduce.reduce.java.opts=-Xmx512m",
            "-files", ",".join(str(path) for path in files),
            "-cmdenv", "PYTHONPATH=.",
            "-input", input_path,
            "-output", output_path,
            "-mapper", f"python3 mapper.py {mapper_mode}",
        ]
        if reducer is None:
            arguments += ["-reducer", "NONE"]
        else:
            arguments += ["-numReduceTasks", "1"]
            if reducer == "score":
                arguments += ["-reducer", f"python3 score_reducer.py {mapper_mode}"]
            else:
                arguments += ["-reducer", "python3 clean_reducer.py", "-cmdenv", f"ML1M_TASK_ID={self.task_id}"]
        output = self.command(stage, arguments, capture=True)
        job_ids = re.findall(r"job_[0-9]+_[0-9]+", output)
        self.jobs[stage] = {"job_id": job_ids[-1] if job_ids else None, "hdfs_output": output_path}
        self.write_status("running")

    def hdfs(self, stage: str, *args: str) -> None:
        self.command(stage, [str(HDFS), "dfs", *args])

    def getmerge(self, stage: str, source: str, target: Path) -> None:
        self.hdfs(stage, "-getmerge", source, str(target))
        if not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError(f"HDFS output did not materialize: {source}")

    def check_cluster(self) -> None:
        self.command("check_hdfs", [str(HDFS), "dfs", "-ls", "/"])
        output = self.command("check_yarn", [str(HADOOP_HOME / "bin/yarn"), "node", "-list"], capture=True)
        if "RUNNING" not in output:
            raise RuntimeError("No RUNNING YARN NodeManager is available")

    def upload_raw(self) -> str:
        raw_dir = f"{self.hdfs_root}/raw"
        self.hdfs("create_raw_dir", "-mkdir", "-p", raw_dir)
        for name, entry in self.manifest["files"].items():
            self.hdfs(f"upload_{name}", "-put", str(PROJECT / entry["path"]), f"{raw_dir}/{name}")
        return raw_dir

    def run(self) -> dict[str, Any]:
        self.check_cluster()
        raw_dir = self.upload_raw()
        before_path = f"{self.hdfs_root}/score_before"
        process_path = f"{self.hdfs_root}/process"
        clean_path = f"{self.hdfs_root}/cleaned"
        after_path = f"{self.hdfs_root}/score_after"

        self.stream_job("score_before", raw_dir, before_path, "raw", "score")
        self.stream_job("clean", raw_dir, process_path, "raw", "clean")
        self.stream_job("export_clean", process_path, clean_path, "export", None)
        self.stream_job("score_after", clean_path, after_path, "clean", "score")

        before_file = self.run_dir / "score_before.tsv"
        process_file = self.run_dir / "clean_process.tsv"
        clean_file = self.run_dir / "cleaned.tsv"
        after_file = self.run_dir / "score_after.tsv"
        self.getmerge("download_score_before", before_path, before_file)
        self.getmerge("download_clean_process", process_path, process_file)
        self.getmerge("download_cleaned", clean_path, clean_file)
        self.getmerge("download_score_after", after_path, after_file)

        before = single_record(before_file, "R")
        clean_report = single_record(process_file, "R")
        after = single_record(after_file, "R")
        if before["kind"] != "raw" or after["kind"] != "clean":
            raise RuntimeError("Score jobs returned unexpected dataset kinds")
        if any(item.get("rule_version") != self.rules["rule_version"] for item in (before, clean_report, after)):
            raise RuntimeError("Hadoop jobs did not use the selected rule snapshot")
        for table, entry in self.manifest["files"].items():
            key = table.removesuffix(".dat")
            if before["tables"][key]["rows"] != entry["physical_lines"]:
                raise RuntimeError(f"Raw score row count mismatch for {table}")
            if after["tables"][key]["rows"] != clean_report["tables"][key]["output"]:
                raise RuntimeError(f"Clean score row count mismatch for {table}")
        clean_lines = sum(1 for line in clean_file.open("rb") if line.startswith(b"C\t"))
        output_rows = sum(item["output"] for item in clean_report["tables"].values())
        if clean_lines != output_rows:
            raise RuntimeError(f"Exported clean rows {clean_lines} != reported rows {output_rows}")
        if sum(clean_report["period_counts"].values()) != clean_report["tables"]["ratings"]["output"]:
            raise RuntimeError("Temporal period counts do not reconcile")

        clean_hash = sha256(clean_file)
        code_hash = hashlib.sha256()
        for name in ("mapper.py", "quality.py", "score_reducer.py", "clean_reducer.py"):
            code_hash.update((PROJECT / "hadoop_jobs" / name).read_bytes())
        quality_delta = {
            dimension: (
                after["overall"][dimension] - before["overall"][dimension]
                if after["overall"][dimension] is not None and before["overall"][dimension] is not None
                else None
            )
            for dimension in before["overall"]
        }
        self.assert_snapshot_unchanged()
        report = {
            "task_id": self.task_id,
            "status": "completed",
            "started_at": self.started_at,
            "completed_at": now(),
            "raw_data_version": self.manifest["data_version"],
            "rule_version": self.rules["rule_version"],
            "rule_sha256": self.expected_rule_sha256,
            "rule_parameters": {
                "min_retained_rating": self.rules.get("processing_policy", {}).get("min_retained_rating", 1),
                "missing_title_year_policy": self.rules.get("processing_policy", {}).get("missing_title_year_policy", "flag"),
                "table_weights": self.rules.get("table_weights", {"ratings": 1, "users": 1, "movies": 1}),
            },
            "code_sha256": code_hash.hexdigest(),
            "clean_data_version": "clean-" + clean_hash[:20],
            "clean_data_sha256": clean_hash,
            "T1": clean_report["T1"],
            "T2": clean_report["T2"],
            "period_counts": clean_report["period_counts"],
            "quality_before": before,
            "quality_after": after,
            "quality_delta": quality_delta,
            "cleaning": clean_report,
            "jobs": self.jobs,
            "hdfs_paths": {
                "raw": raw_dir,
                "cleaned": clean_path,
                "audit_and_process": process_path,
                "score_before": before_path,
                "score_after": after_path,
            },
            "local_clean_file": str(clean_file),
        }
        (self.run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stage = "completed"
        self.write_status("completed", report_file=str(self.run_dir / "report.json"))
        return report


def single_record(path: Path, expected_tag: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            tag, separator, payload = line.rstrip("\n").partition("\t")
            if separator and tag == expected_tag:
                matches.append(json.loads(payload))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {expected_tag} record in {path}, got {len(matches)}")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", help="Optional unique task ID for an external Agent")
    parser.add_argument("--rules-file", type=Path, help="Frozen task-specific rule snapshot")
    parser.add_argument("--rules-sha256", help="Expected SHA-256 for --rules-file")
    args = parser.parse_args()
    task_id = args.task_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
        parser.error("task ID must be 1-80 ASCII letters, digits, underscores, or hyphens")
    if bool(args.rules_file) != bool(args.rules_sha256):
        parser.error("--rules-file and --rules-sha256 must be supplied together")
    rule_path = args.rules_file or PROJECT / "config/quality_rules_v1.json"
    expected_rule_sha256 = args.rules_sha256 or sha256(rule_path)
    manifest, rules = verify_input(rule_path, expected_rule_sha256)
    pipeline = Pipeline(task_id, manifest, rules, rule_path, expected_rule_sha256)
    try:
        report = pipeline.run()
    except Exception as error:
        pipeline.write_status("failed", error=str(error))
        print(f"Task failed at {pipeline.stage}: {error}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps({
        "task_id": task_id,
        "report_file": str(pipeline.run_dir / "report.json"),
        "clean_data_version": report["clean_data_version"],
        "T1": report["T1"],
        "T2": report["T2"],
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
