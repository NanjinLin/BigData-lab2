"""Shared, versioned quality predicates used by both Hadoop score jobs."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_rules() -> dict[str, Any]:
    local = Path("quality_rules_v1.json")
    if not local.exists():
        local = Path(__file__).resolve().parent.parent / "config" / "quality_rules_v1.json"
    return json.loads(local.read_text(encoding="utf-8"))


RULES = load_rules()
FIELDS = RULES["tables"]
NULL = RULES["null_marker"]
GENRES = set(RULES["allowed_values"]["genres"])
AGES = {str(value) for value in RULES["allowed_values"]["age"]}
GENDERS = set(RULES["allowed_values"]["gender"])


def epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


TIME_MIN = epoch(RULES["rating_timestamp_window_utc"]["start_inclusive"])
TIME_MAX = epoch(RULES["rating_timestamp_window_utc"]["end_inclusive"])
FRESH_MIN = epoch(RULES["freshness_window_utc"]["start_inclusive"])
FRESH_MAX = epoch(RULES["freshness_window_utc"]["end_inclusive"])
NUMERIC = {
    "ratings": (0, 1, 2, 3),
    "users": (0, 2, 3),
    "movies": (0,),
}


def is_missing(value: str) -> bool:
    return not value.strip() or value.strip() == NULL


def decimal(value: str) -> int | None:
    value = value.strip()
    if not re.fullmatch(r"[0-9]+", value):
        return None
    return int(value)


def positive(value: str) -> int | None:
    result = decimal(value)
    return result if result is not None and result > 0 else None


def normalized_fields(table: str, fields: list[str]) -> tuple[str, ...]:
    result = [field.strip() for field in fields]
    for index in NUMERIC[table]:
        if index < len(result):
            number = decimal(result[index])
            if number is not None:
                result[index] = str(number)
    if table == "movies" and len(result) >= 3 and not is_missing(result[2]):
        tokens = [token.strip() for token in result[2].split("|")]
        result[2] = "|".join(dict.fromkeys(tokens))
    return tuple(result)


def fields_for(row: str | list[str]) -> list[str]:
    return row.split(RULES["delimiter"]) if isinstance(row, str) else row


def accurate(table: str, fields: list[str]) -> bool:
    if len(fields) != len(FIELDS[table]) or any(is_missing(value) for value in fields):
        return False
    if table == "ratings":
        user_id, movie_id, rating, timestamp = fields
        value = decimal(rating)
        when = decimal(timestamp)
        return (
            positive(user_id) is not None
            and positive(movie_id) is not None
            and value in RULES["allowed_values"]["rating"]
            and when is not None
            and TIME_MIN <= when <= TIME_MAX
        )
    if table == "users":
        user_id, gender, age, occupation, _zip_code = fields
        job = decimal(occupation)
        return (
            positive(user_id) is not None
            and gender in GENDERS
            and age in AGES
            and job is not None
            and RULES["allowed_values"]["occupation_min"]
            <= job
            <= RULES["allowed_values"]["occupation_max"]
        )
    movie_id, title, genres = fields
    tokens = genres.split("|")
    return (
        positive(movie_id) is not None
        and bool(title.strip())
        and bool(tokens)
        and all(token in GENRES for token in tokens)
    )


def score_dataset(rows: dict[str, list[str | list[str]]], kind: str) -> dict[str, Any]:
    """Score a raw or cleaned dataset with identical predicates and denominators."""
    user_variants: dict[int, set[tuple[str, ...]]] = defaultdict(set)
    movie_variants: dict[int, set[tuple[str, ...]]] = defaultdict(set)
    event_ratings: dict[tuple[int, int, int], set[str]] = defaultdict(set)

    for table, variants in (("users", user_variants), ("movies", movie_variants)):
        for row in rows[table]:
            fields = fields_for(row)
            if len(fields) == len(FIELDS[table]):
                record_id = positive(fields[0])
                if record_id is not None:
                    variants[record_id].add(normalized_fields(table, fields)[1:])

    for row in rows["ratings"]:
        fields = fields_for(row)
        if len(fields) != 4:
            continue
        user_id, movie_id, _rating, timestamp = fields
        uid, mid, when = positive(user_id), positive(movie_id), decimal(timestamp)
        if uid is not None and mid is not None and when is not None:
            event_ratings[(uid, mid, when)].add(normalized_fields("ratings", fields)[2])

    conflicting_users = {key for key, values in user_variants.items() if len(values) > 1}
    conflicting_movies = {key for key, values in movie_variants.items() if len(values) > 1}
    conflicting_events = {key for key, values in event_ratings.items() if len(values) > 1}

    by_table: dict[str, Any] = {}
    for table in ("ratings", "users", "movies"):
        count = len(rows[table])
        filled = accurate_count = consistent_count = recent_count = 0
        unique_keys: set[tuple[str, ...]] = set()
        width = len(FIELDS[table])
        for row in rows[table]:
            fields = fields_for(row)
            filled += sum(not is_missing(fields[index]) for index in range(min(width, len(fields))))
            accurate_count += accurate(table, fields)
            if len(fields) == width:
                unique_keys.add(normalized_fields(table, fields))
            else:
                unique_keys.add(("!malformed!", str(row)))

            if table == "ratings" and len(fields) == 4:
                when = decimal(fields[3])
                recent_count += when is not None and FRESH_MIN <= when <= FRESH_MAX

            consistent_count += consistent(
                table,
                fields,
                user_variants,
                movie_variants,
                conflicting_users,
                conflicting_movies,
                conflicting_events,
            )

        metrics = {
            "Accurate": measure(accurate_count, count),
            "Complete": measure(filled, count * width),
            "Unique": measure(len(unique_keys), count),
            "Consistent": measure(consistent_count, count),
        }
        if table == "ratings":
            metrics["Up-to-date"] = measure(recent_count, count)
        by_table[table] = {"rows": count, "metrics": metrics}

    overall: dict[str, float | None] = {}
    for dimension in ("Accurate", "Complete", "Unique", "Consistent"):
        values = [
            by_table[table]["metrics"][dimension]["score"]
            for table in ("ratings", "users", "movies")
            if by_table[table]["metrics"][dimension]["score"] is not None
        ]
        overall[dimension] = sum(values) / len(values) if values else None
    overall["Up-to-date"] = by_table["ratings"]["metrics"]["Up-to-date"]["score"]
    return {
        "kind": kind,
        "rule_version": RULES["rule_version"],
        "overall": overall,
        "tables": by_table,
        "diagnostics": {
            "conflicting_user_ids": len(conflicting_users),
            "conflicting_movie_ids": len(conflicting_movies),
            "conflicting_rating_events": len(conflicting_events),
        },
    }


def measure(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "score": 100.0 * numerator / denominator if denominator else None,
    }


def consistent(
    table: str,
    fields: list[str],
    user_variants: dict[int, set[tuple[str, ...]]],
    movie_variants: dict[int, set[tuple[str, ...]]],
    conflicting_users: set[int],
    conflicting_movies: set[int],
    conflicting_events: set[tuple[int, int, int]],
) -> bool:
    if len(fields) != len(FIELDS[table]):
        return False
    if any(field != field.strip() for field in fields):
        return False
    for index in NUMERIC[table]:
        value = fields[index]
        if is_missing(value):
            if table == "ratings" or index == 0:
                return False
            continue
        number = decimal(value)
        if number is None or value != str(number):
            return False

    if table == "users":
        user_id = positive(fields[0])
        return user_id is not None and user_id not in conflicting_users
    if table == "movies":
        movie_id = positive(fields[0])
        if movie_id is None or movie_id in conflicting_movies:
            return False
        genres = fields[2]
        if not is_missing(genres):
            tokens = genres.split("|")
            if len(tokens) != len(set(tokens)) or any(token not in GENRES for token in tokens):
                return False
        return True

    user_id, movie_id, _rating, timestamp = fields
    uid, mid, when = positive(user_id), positive(movie_id), decimal(timestamp)
    return (
        uid is not None
        and mid is not None
        and when is not None
        and uid in user_variants
        and mid in movie_variants
        and (uid, mid, when) not in conflicting_events
    )
