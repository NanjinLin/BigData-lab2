"""Bounded, read-only checks for HDFS write readiness (also used by the launcher)."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import time
from collections.abc import Callable


def wait_for_hdfs(hdfs: str, timeout: float = 120, *,
                  notify: Callable[[str], None] = print) -> None:
    if not 1 <= timeout <= 1800:
        raise ValueError("HDFS readiness timeout must be between 1 and 1800 seconds")
    deadline = time.monotonic() + timeout
    detail = "HDFS has not responded"
    env = {**os.environ, "LC_ALL": "C"}
    while time.monotonic() < deadline:
        ready = True
        for args, pattern in [
            (["dfsadmin", "-safemode", "get"], r"Safe mode is OFF\b"),
            (["dfsadmin", "-report", "-live"], r"Live datanodes\s*\([1-9][0-9]*\)"),
        ]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                ready = False
                break
            try:
                result = subprocess.run(
                    [hdfs, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", env=env,
                    timeout=min(10, remaining), check=False,
                )
                detail = result.stdout.strip() or f"HDFS check exited with {result.returncode}"
                if result.returncode != 0 or not re.search(pattern, result.stdout):
                    ready = False
                    break
            except subprocess.TimeoutExpired:
                detail = "HDFS readiness command timed out"
                ready = False
                break
        if ready:
            notify("HDFS is ready: safe mode OFF, live DataNode available.")
            return
        notify(f"Waiting for HDFS: {detail[-1500:]}")
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise RuntimeError(
        f"HDFS did not become writable within {timeout:g} seconds. "
        "Check NameNode/DataNode logs and dfsadmin -report; safe mode was not forced off. "
        f"Last check: {detail[-1500:]}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hdfs", required=True)
    parser.add_argument("--timeout", type=float,
                        default=os.environ.get("ML1M_HDFS_READY_TIMEOUT_SECONDS", "120"))
    args = parser.parse_args()
    try:
        wait_for_hdfs(args.hdfs, args.timeout, notify=lambda message: print(message, flush=True))
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
