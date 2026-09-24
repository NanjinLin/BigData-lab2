"""Hadoop Streaming reducer that cleans all three MovieLens tables."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from quality import (
    AGES,
    FIELDS,
    GENDERS,
    GENRES,
    NULL,
    RULES,
    TIME_MAX,
    TIME_MIN,
    decimal,
    is_missing,
    normalized_fields,
    positive,
)

Emit = Callable[[str, dict[str, Any]], None]


def source_hash(table: str, raw: str) -> str:
    return hashlib.sha256((table + "\0" + raw).encode("latin-1")).hexdigest()


def clean_dataset(raw_rows: dict[str, list[str]], emit: Emit, task_id: str) -> dict[str, Any]:
    stats: dict[str, dict[str, Any]] = {}
    for table in FIELDS:
        stats[table] = {
            "input": len(raw_rows[table]),
            "contributing": 0,
            "duplicate": 0,
            "quarantine": 0,
            "output": 0,
            "normalized_fields": 0,
            "masked_source_fields": 0,
            "reasons": {},
        }
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unresolved: Counter[str] = Counter()

    def change_reason(table: str, reason: str, count: int = 1) -> None:
        reasons = stats[table]["reasons"]
        reasons[reason] = reasons.get(reason, 0) + count

    def audit(
        table: str,
        action: str,
        reason: str,
        raw: str | None = None,
        count: int = 1,
        details: dict[str, Any] | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "kind": "audit",
            "table": table,
            "action": action,
            "reason": reason,
            "count": count,
        }
        if raw is not None:
            event["raw"] = raw
            event["source_hash"] = source_hash(table, raw)
        if details:
            event["details"] = details
        emit("A", event)
        if len(samples[reason]) < 3:
            samples[reason].append(event)
        change_reason(table, reason, count)

    def quarantine(table: str, reason: str, raw: str) -> None:
        stats[table]["quarantine"] += 1
        audit(table, "quarantine", reason, raw)

    # Metadata IDs are preserved when optional attributes are bad or conflicting.
    # Each ID becomes one output row; uncertain attributes become the null marker.
    clean_ids: dict[str, set[int]] = {"users": set(), "movies": set()}
    for table in ("users", "movies"):
        variants: dict[int, dict[tuple[str, ...], list[str]]] = defaultdict(lambda: defaultdict(list))
        for raw in raw_rows[table]:
            fields = raw.split(RULES["delimiter"])
            if len(fields) != len(FIELDS[table]):
                quarantine(table, "wrong_field_count", raw)
                continue
            record_id = positive(fields[0])
            if record_id is None:
                quarantine(table, "invalid_id", raw)
                continue
            values = list(normalized_fields(table, fields))
            for field_name, before, after in zip(FIELDS[table], fields, values):
                if before != after:
                    stats[table]["normalized_fields"] += 1
                    audit(
                        table,
                        "normalize",
                        "normalized_field",
                        raw,
                        details={"field": field_name, "before": before, "after": after},
                    )
            values[0] = str(record_id)
            if table == "users":
                if values[1] not in GENDERS:
                    if not is_missing(values[1]):
                        audit(table, "mask", "invalid_gender", raw)
                    values[1] = NULL
                    stats[table]["masked_source_fields"] += 1
                if values[2] not in AGES:
                    if not is_missing(values[2]):
                        audit(table, "mask", "invalid_age", raw)
                    values[2] = NULL
                    stats[table]["masked_source_fields"] += 1
                occupation = decimal(values[3])
                if (
                    occupation is None
                    or not RULES["allowed_values"]["occupation_min"]
                    <= occupation
                    <= RULES["allowed_values"]["occupation_max"]
                ):
                    if not is_missing(values[3]):
                        audit(table, "mask", "invalid_occupation", raw)
                    values[3] = NULL
                    stats[table]["masked_source_fields"] += 1
                if is_missing(values[4]):
                    values[4] = NULL
                    stats[table]["masked_source_fields"] += 1
                elif not re.fullmatch(r"[0-9]{5}(-[0-9]{4})?", values[4]):
                    audit(table, "flag", "non_us_zip_pattern", raw)
                    unresolved["non_us_zip_pattern"] += 1
            else:
                if is_missing(values[1]):
                    values[1] = NULL
                    stats[table]["masked_source_fields"] += 1
                else:
                    if not re.search(r"\([0-9]{4}\)$", values[1]):
                        audit(table, "flag", "missing_title_year", raw)
                        unresolved["missing_title_year"] += 1
                    if "Ã" in values[1] or "Â" in values[1]:
                        audit(table, "flag", "possible_mojibake", raw)
                        unresolved["possible_mojibake"] += 1
                if is_missing(values[2]):
                    values[2] = NULL
                    stats[table]["masked_source_fields"] += 1
                else:
                    tokens = list(dict.fromkeys(token.strip() for token in values[2].split("|")))
                    known = sorted(token for token in tokens if token in GENRES)
                    unknown = sorted(token for token in tokens if token not in GENRES)
                    if unknown:
                        audit(table, "mask", "unknown_genre", raw, details={"tokens": unknown})
                        stats[table]["masked_source_fields"] += 1
                    values[2] = "|".join(known) if known else NULL
            variants[record_id][tuple(values)].append(raw)

        for record_id in sorted(variants):
            variant_map = variants[record_id]
            for value_tuple, originals in variant_map.items():
                stats[table]["contributing"] += 1
                if len(originals) > 1:
                    count = len(originals) - 1
                    stats[table]["duplicate"] += count
                    audit(table, "deduplicate", "same_id_same_attributes", originals[0], count)
            output_fields = [str(record_id)]
            for index in range(1, len(FIELDS[table])):
                known = {variant[index] for variant in variant_map if not is_missing(variant[index])}
                if len(known) == 1:
                    output_fields.append(next(iter(known)))
                elif not known:
                    output_fields.append(NULL)
                else:
                    output_fields.append(NULL)
                    unresolved[f"{table}_{FIELDS[table][index]}_conflict"] += 1
                    audit(
                        table,
                        "mask_conflict",
                        "same_id_conflicting_attribute",
                        details={"id": record_id, "field": FIELDS[table][index], "values": sorted(known)},
                    )
            clean_ids[table].add(record_id)
            stats[table]["output"] += 1
            emit(
                "C",
                {
                    "kind": "clean",
                    "table": table,
                    "fields": output_fields,
                    "source_hashes": sorted(source_hash(table, originals[0])[:24] for originals in variant_map.values()),
                    "source_row_count": sum(len(originals) for originals in variant_map.values()),
                },
            )

    # Ratings are sorted by their event key so all candidates for one event are
    # resolved together. Distinct timestamps for a user/movie pair remain valid.
    candidates: list[tuple[int, int, int, int, str, tuple[str, ...]]] = []
    for raw in raw_rows["ratings"]:
        fields = raw.split(RULES["delimiter"])
        if len(fields) != 4:
            quarantine("ratings", "wrong_field_count", raw)
            continue
        canonical = normalized_fields("ratings", fields)
        user_id, movie_id = positive(canonical[0]), positive(canonical[1])
        rating, timestamp = decimal(canonical[2]), decimal(canonical[3])
        if user_id is None or movie_id is None:
            quarantine("ratings", "invalid_id", raw)
            continue
        if rating not in RULES["allowed_values"]["rating"]:
            quarantine("ratings", "invalid_rating", raw)
            continue
        if timestamp is None or not TIME_MIN <= timestamp <= TIME_MAX:
            quarantine("ratings", "invalid_timestamp", raw)
            continue
        if user_id not in clean_ids["users"]:
            quarantine("ratings", "unknown_user", raw)
            continue
        if movie_id not in clean_ids["movies"]:
            quarantine("ratings", "unknown_movie", raw)
            continue
        candidates.append((user_id, movie_id, timestamp, rating, raw, canonical))

    candidates.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]))
    accepted_timestamps: list[int] = []
    position = 0
    while position < len(candidates):
        first = candidates[position]
        end = position + 1
        while end < len(candidates) and candidates[end][:3] == first[:3]:
            end += 1
        group = candidates[position:end]
        if len({row[3] for row in group}) > 1:
            for row in group:
                quarantine("ratings", "same_event_conflicting_rating", row[4])
        else:
            stats["ratings"]["contributing"] += 1
            if len(group) > 1:
                count = len(group) - 1
                stats["ratings"]["duplicate"] += count
                audit("ratings", "deduplicate", "same_event_same_rating", group[0][4], count)
            user_id, movie_id, timestamp, rating, raw, canonical = group[0]
            for field_name, before, after in zip(
                FIELDS["ratings"], raw.split(RULES["delimiter"]), canonical
            ):
                if before != after:
                    stats["ratings"]["normalized_fields"] += 1
                    audit(
                        "ratings",
                        "normalize",
                        "normalized_field",
                        raw,
                        details={"field": field_name, "before": before, "after": after},
                    )
            accepted_timestamps.append(timestamp)
            stats["ratings"]["output"] += 1
            emit(
                "C",
                {
                    "kind": "clean",
                    "table": "ratings",
                    "fields": list(canonical),
                    "source_hash": source_hash("ratings", raw)[:24],
                    "source_row_count": len(group),
                },
            )
        position = end

    for table in FIELDS:
        info = stats[table]
        if info["input"] != info["contributing"] + info["duplicate"] + info["quarantine"]:
            raise RuntimeError(f"Disposition does not reconcile for {table}: {info}")
        if info["output"] <= 0:
            raise RuntimeError(f"No retained rows for {table}")

    split = temporal_split(accepted_timestamps)
    report = {
        "kind": "clean_report",
        "task_id": task_id,
        "raw_data_version": RULES["raw_data_version"],
        "rule_version": RULES["rule_version"],
        "tables": stats,
        "unresolved": dict(sorted(unresolved.items())),
        "T1": split["T1"],
        "T2": split["T2"],
        "period_counts": split["period_counts"],
        "audit_samples": dict(samples),
    }
    emit("R", report)
    return report


def temporal_split(timestamps: list[int]) -> dict[str, Any]:
    if len(timestamps) < 3:
        raise RuntimeError("Not enough accepted ratings for a three-period split")
    by_day = Counter(datetime.fromtimestamp(value, timezone.utc).date() for value in timestamps)
    total = len(timestamps)
    train_target = (total * 8 + 9) // 10
    validation_target = (total * 9 + 9) // 10
    cumulative = 0
    t1_day = t2_day = None
    for day, count in sorted(by_day.items()):
        cumulative += count
        if t1_day is None and cumulative >= train_target:
            t1_day = day
        if t2_day is None and cumulative >= validation_target:
            t2_day = day
    if t1_day is None or t2_day is None or t1_day >= t2_day:
        raise RuntimeError("Temporal split produced an empty validation period")
    train = sum(count for day, count in by_day.items() if day <= t1_day)
    validation = sum(count for day, count in by_day.items() if t1_day < day <= t2_day)
    test = total - train - validation
    if not train or not validation or not test:
        raise RuntimeError("Temporal split produced an empty period")
    return {
        "T1": f"{t1_day.isoformat()}T23:59:59Z",
        "T2": f"{t2_day.isoformat()}T23:59:59Z",
        "period_counts": {"train": train, "validation": validation, "test": test},
    }


def main() -> None:
    raw_rows: dict[str, list[str]] = {"ratings": [], "users": [], "movies": []}
    for line in sys.stdin:
        _key, payload = line.rstrip("\n").split("\t", 1)
        item = json.loads(payload)
        table = item["table"]
        if table not in raw_rows:
            raise ValueError(f"Unknown table: {table}")
        raw_rows[table].append(item["raw"])
    if any(not raw_rows[table] for table in raw_rows):
        raise RuntimeError("Clean job requires non-empty ratings, users, and movies inputs")

    def emit(tag: str, item: dict[str, Any]) -> None:
        sys.stdout.write(tag + "\t" + json.dumps(item, ensure_ascii=True, separators=(",", ":")) + "\n")

    clean_dataset(raw_rows, emit, os.environ.get("ML1M_TASK_ID", "unknown"))


if __name__ == "__main__":
    main()
