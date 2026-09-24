"""Hadoop Streaming mapper for raw input and cleaned result input."""

from __future__ import annotations

import json
import os
import sys
from pathlib import PurePosixPath


def input_table() -> str:
    source = (
        os.environ.get("mapreduce_map_input_file")
        or os.environ.get("map_input_file")
        or os.environ.get("MAPREDUCE_MAP_INPUT_FILE")
    )
    name = PurePosixPath(source or "").name
    table = name.removesuffix(".dat")
    if table not in {"ratings", "users", "movies"}:
        raise RuntimeError(f"Unrecognized Hadoop input file: {source!r}")
    return table


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"raw", "clean", "export"}:
        raise SystemExit("usage: mapper.py raw|clean|export")
    mode = sys.argv[1]
    table = input_table() if mode == "raw" else None
    for line in sys.stdin.buffer:
        if mode == "raw":
            payload = {
                "table": table,
                "raw": line.rstrip(b"\r\n").decode("latin-1"),
            }
        else:
            if not line.startswith(b"C\t"):
                continue
            if mode == "export":
                sys.stdout.buffer.write(line)
                continue
            item = json.loads(line.split(b"\t", 1)[1].decode("utf-8"))
            if item.get("kind") != "clean":
                raise ValueError("C-tagged record is not clean")
            payload = {"table": item["table"], "fields": item["fields"]}
        sys.stdout.write("0\t" + json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
