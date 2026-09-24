"""Single Hadoop reducer for the five documented quality dimensions."""

from __future__ import annotations

import json
import sys

from quality import score_dataset


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"raw", "clean"}:
        raise SystemExit("usage: score_reducer.py raw|clean")
    mode = sys.argv[1]
    rows: dict[str, list[str | list[str]]] = {"ratings": [], "users": [], "movies": []}
    for line in sys.stdin:
        _key, payload = line.rstrip("\n").split("\t", 1)
        record = json.loads(payload)
        table = record["table"]
        if table not in rows:
            raise ValueError(f"Unknown table: {table}")
        rows[table].append(record["raw"] if mode == "raw" else record["fields"])
    if any(not rows[table] for table in rows):
        raise RuntimeError("Score job requires non-empty ratings, users, and movies inputs")
    result = score_dataset(rows, mode)
    sys.stdout.write("R\t" + json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
