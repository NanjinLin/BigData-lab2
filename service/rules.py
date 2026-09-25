"""Validate and version the limited rule profiles executable by Hadoop."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


BASE_PATH = Path(__file__).resolve().parent.parent / "config" / "quality_rules_v1.json"
BASE_RULES = json.loads(BASE_PATH.read_text(encoding="utf-8"))
BASE_VERSION = BASE_RULES["rule_version"]
DEFAULT_OPTIONS = {"min_retained_rating": 1, "missing_title_year_policy": "flag",
                   "table_weights": {"ratings": 1, "users": 1, "movies": 1}}
TABLES = frozenset(("ratings", "users", "movies"))


def _implementation_fingerprint() -> str:
    digest = hashlib.sha256()
    root = BASE_PATH.parent.parent / "hadoop_jobs"
    for name in ("quality.py", "clean_reducer.py", "score_reducer.py"):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


class RuleConfigError(ValueError):
    pass


def _validated_options(options: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(options, dict) or set(options) != set(DEFAULT_OPTIONS):
        raise RuleConfigError("规则参数必须包含最低保留评分、标题年份策略和三表权重，且不能包含其他参数。")
    minimum = options["min_retained_rating"]
    if type(minimum) is not int or not 1 <= minimum <= 5:
        raise RuleConfigError("最低保留评分必须是 1 至 5 的整数。")
    policy = options["missing_title_year_policy"]
    if policy not in ("flag", "quarantine"):
        raise RuleConfigError("标题缺少年份的策略只能是 flag 或 quarantine。")
    weights = options["table_weights"]
    if not isinstance(weights, dict) or set(weights) != TABLES:
        raise RuleConfigError("权重必须分别指定评分、用户和电影三张表。")
    if any(type(value) is not int or value < 0 for value in weights.values()):
        raise RuleConfigError("每张表的权重必须是非负整数。")
    if not any(weights.values()):
        raise RuleConfigError("三张表的权重不能全为零。")
    return {"min_retained_rating": minimum, "missing_title_year_policy": policy,
            "table_weights": {name: weights[name] for name in ("ratings", "users", "movies")}}


def build_rules(options: dict[str, Any] | None) -> dict[str, Any]:
    """Return the registered config or a deterministic, validated derivative."""
    if options is None:
        return copy.deepcopy(BASE_RULES)
    selected = _validated_options(options)
    if selected == DEFAULT_OPTIONS:
        return copy.deepcopy(BASE_RULES)
    identity = {"options": selected, "implementation": _implementation_fingerprint()}
    version_hash = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"))
                                  .encode("utf-8")).hexdigest()[:12]
    rules = copy.deepcopy(BASE_RULES)
    rules["rule_version"] = f"{BASE_VERSION}+{version_hash}"
    rules["processing_policy"] = {"min_retained_rating": selected["min_retained_rating"],
                                  "missing_title_year_policy": selected["missing_title_year_policy"]}
    rules["table_weights"] = selected["table_weights"]
    return rules


def validate_rules(rules: dict[str, Any]) -> dict[str, Any]:
    """Reject snapshots that change fields outside the registered tuning surface."""
    if not isinstance(rules, dict):
        raise RuleConfigError("规则快照必须是 JSON 对象。")
    if rules == BASE_RULES:
        return copy.deepcopy(BASE_RULES)
    policy = rules.get("processing_policy")
    if not isinstance(policy, dict):
        raise RuleConfigError("规则快照缺少处理策略。")
    options = {"min_retained_rating": policy.get("min_retained_rating"),
               "missing_title_year_policy": policy.get("missing_title_year_policy"),
               "table_weights": rules.get("table_weights")}
    expected = build_rules(options)
    if rules != expected:
        raise RuleConfigError("规则快照与已登记基础规则或派生版本不一致。")
    return expected


def snapshot_bytes(rules: dict[str, Any]) -> bytes:
    validate_rules(rules)
    if rules == BASE_RULES:
        return BASE_PATH.read_bytes()
    return (json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
