"""Deterministic, read-only statistics over a verified MovieLens clean table."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


VERSION = "movielens-statistics-v1"
GENRES = frozenset(("Action", "Adventure", "Animation", "Children's", "Comedy", "Crime",
                    "Documentary", "Drama", "Fantasy", "Film-Noir", "Horror", "Musical",
                    "Mystery", "Romance", "Sci-Fi", "Thriller", "War", "Western"))
DEFAULTS = {"group_by": "genre", "sort_by": "mean_rating", "min_ratings": 1,
            "limit": 20, "movie_query": "", "genre": "", "period": "all"}


class AnalysisError(ValueError):
    pass


def validate_options(options: dict) -> dict:
    if not isinstance(options, dict) or set(options) - DEFAULTS.keys():
        raise AnalysisError("统计参数含未支持的字段。")
    value = {**DEFAULTS, **options}
    for key, allowed in (("group_by", ("movie", "genre")),
                         ("sort_by", ("mean_rating", "rating_count")),
                         ("period", ("all", "train", "validation", "test"))):
        if not isinstance(value[key], str) or value[key] not in allowed:
            raise AnalysisError(f"统计参数 {key} 无效。")
    for key, maximum in (("min_ratings", 10_000_000), ("limit", 50)):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise AnalysisError(f"{key} 必须是 1 至 {maximum} 的整数。")
    if not isinstance(value["movie_query"], str) or len(value["movie_query"]) > 120:
        raise AnalysisError("电影标题关键词不得超过 120 字。")
    value["movie_query"] = value["movie_query"].strip()
    if not isinstance(value["genre"], str) or value["genre"] and value["genre"] not in GENRES:
        raise AnalysisError("genre 需为登记的英文电影类型，或留空。")
    return value


def _record(line: bytes) -> tuple[str, list]:
    try:
        tag, payload = line.decode("utf-8").rstrip("\r\n").split("\t", 1)
        item = json.loads(payload)
        table, fields = item["table"], item["fields"]
        if (tag != "C" or table not in ("ratings", "users", "movies")
                or not isinstance(fields, list) or len(fields) != {"ratings": 4, "users": 5, "movies": 3}[table]
                or not all(isinstance(field, str) for field in fields)):
            raise ValueError()
        return table, fields
    except (ValueError, UnicodeError, KeyError, TypeError) as error:
        raise AnalysisError("净表格式损坏，无法提供可核验的统计。") from error


def analyze_cleaned(directory: Path, report: dict, options: dict, *, all_groups: bool = False) -> dict:
    query = validate_options(options)
    if all_groups:
        # Chart-only callers need the complete movie population, not a top-50 ranking.
        query["limit"] = None
    source = directory / "cleaned.tsv"
    if not source.is_file():
        raise AnalysisError("本机缺少该任务的完整净表，请转交 cleaned.tsv 或明确要求重新清洗；报告和样例不能替代完整数据。")
    fingerprint = source.stat()

    def verify_source_unchanged():
        current = source.stat()
        if current.st_mtime_ns != fingerprint.st_mtime_ns or current.st_size != fingerprint.st_size:
            raise AnalysisError("统计期间净表发生变化，请重试。")

    digest, movies, counts = hashlib.sha256(), {}, Counter()
    with source.open("rb") as stream:
        for line in stream:
            digest.update(line)
            table, fields = _record(line)
            counts[table] += 1
            if table == "movies":
                if fields[0] in movies:
                    raise AnalysisError("净表存在重复电影 ID，无法可靠关联。")
                movies[fields[0]] = (fields[1], tuple(dict.fromkeys(g for g in fields[2].split("|") if g in GENRES)))
    clean_hash = digest.hexdigest()
    if clean_hash != report.get("clean_data_sha256"):
        raise AnalysisError("净表 SHA-256 与正式报告不一致，统计已停止。")
    verify_source_unchanged()
    for table in ("ratings", "users", "movies"):
        expected = report.get("cleaning", {}).get("tables", {}).get(table, {}).get("output")
        if expected != counts[table]:
            raise AnalysisError(f"{table} 净表行数与正式报告不一致，统计已停止。")
    implementation_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report_hash = hashlib.sha256(json.dumps(report, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    identity = json.dumps({"version": VERSION, "implementation_sha256": implementation_hash,
                           "sha256": clean_hash, "query": query,
                           "report_content_sha256": report_hash}, sort_keys=True)
    analysis_id = "analysis-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    destination = directory / "analyses" / f"{analysis_id}.json"
    if destination.is_file():
        try:
            cached = json.loads(destination.read_text(encoding="utf-8"))
            if cached["analysis_id"] == analysis_id and cached["source"]["clean_data_sha256"] == clean_hash:
                return {**cached, "reused": True}
        except (ValueError, OSError, KeyError, TypeError):
            pass
    cutoffs = None
    if query["period"] != "all":
        try:
            cutoffs = [datetime.fromisoformat(report[key].replace("Z", "+00:00")).timestamp() for key in ("T1", "T2")]
            if cutoffs[0] >= cutoffs[1]:
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise AnalysisError("正式报告缺少有效的时间切分边界，无法按时间段统计。") from error
    groups, coverage, matched_distribution = {}, Counter(), Counter()
    with source.open("rb") as stream:
        for line in stream:
            table, fields = _record(line)
            if table != "ratings":
                continue
            try:
                user_id, movie_id = fields[:2]
                rating, timestamp = int(fields[2]), int(fields[3])
                if not user_id.isdecimal() or int(user_id) < 1 or rating not in range(1, 6):
                    raise ValueError()
            except (ValueError, TypeError) as error:
                raise AnalysisError("净表含无效评分记录，统计已停止。") from error
            coverage["total_ratings"] += 1
            if cutoffs:
                period = "train" if timestamp <= cutoffs[0] else "validation" if timestamp <= cutoffs[1] else "test"
                if period != query["period"]:
                    continue
            coverage["period_ratings"] += 1
            movie = movies.get(movie_id)
            if movie is None:
                coverage["missing_movie_ratings"] += 1
                continue
            title, genres = movie
            if query["movie_query"].casefold() not in title.casefold() or query["genre"] and query["genre"] not in genres:
                coverage["filtered_ratings"] += 1
                continue
            if query["group_by"] == "genre" and not genres:
                coverage["missing_genre_ratings"] += 1
                continue
            coverage["matched_ratings"] += 1
            matched_distribution[rating] += 1
            keys = (movie_id,) if query["group_by"] == "movie" else genres
            for key in keys:
                group = groups.setdefault(key, {"rating_count": 0, "rating_sum": 0, "users": set(), "distribution": Counter()})
                group["rating_count"] += 1
                group["rating_sum"] += rating
                group["users"].add(user_id)
                group["distribution"][rating] += 1
    verify_source_unchanged()
    rows = []
    for key, group in groups.items():
        if group["rating_count"] < query["min_ratings"]:
            continue
        rows.append({"key": key, "label": movies[key][0] if query["group_by"] == "movie" else key,
                     "rating_count": group["rating_count"], "user_count": len(group["users"]),
                     "mean_rating": group["rating_sum"] / group["rating_count"],
                     "distribution": {str(i): group["distribution"][i] for i in range(1, 6)}})
    rows.sort(key=lambda row: (-row[query["sort_by"]], -row["rating_count"], row["key"]))
    eligible_count = len(rows)
    rows = rows[:query["limit"]]
    for row in rows:
        row["mean_rating"] = round(row["mean_rating"], 6)
    warnings = []
    if not rows:
        warnings.append("当前筛选条件下没有达到最低评分次数的分组，不能据此给出排名。")
    small = [row["label"] for row in rows if row["rating_count"] < 30 or row["user_count"] < 30]
    if small:
        warnings.append("部分展示分组不足 30 条评分或 30 位用户；这是提示样本较少的经验阈值，不是显著性检验：" + "、".join(small[:5]))
    if coverage["missing_movie_ratings"] or coverage["missing_genre_ratings"]:
        warnings.append("存在无法关联电影或缺少类型的评分，相关记录未进入对应统计。")
    policy = report.get("rule_parameters") or report.get("cleaning", {}).get("processing_policy", {})
    if policy.get("min_retained_rating", 1) > 1:
        warnings.append("本次净表已过滤部分合法低分，均值和分布受到清洗策略影响，不能代表完整原始评分。")
    checks = [{"name": "净表哈希和三表行数与报告一致", "passed": True},
              {"name": "筛选后存在可展示分组", "passed": bool(rows)},
              {"name": "各分组评分分布合计等于评分次数", "passed": all(sum(r["distribution"].values()) == r["rating_count"] for r in rows)}]
    source_info = {"task_id": report["task_id"], "clean_data_version": report.get("clean_data_version"),
                   "clean_data_sha256": clean_hash, "rule_version": report.get("rule_version"),
                   "report_content_sha256": report_hash,
                   "rule_parameters": report.get("rule_parameters"),
                   "T1": report.get("T1"), "T2": report.get("T2"), "reused_clean_table": True,
                   "rating_disposition": {key: report.get("cleaning", {}).get("tables", {}).get("ratings", {}).get(key)
                                          for key in ("input", "output", "quarantine", "duplicate")}}
    result = {"analysis_id": analysis_id, "version": VERSION, "implementation_sha256": implementation_hash,
              "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "source": source_info, "query": query, "rows": rows,
              "rating_distribution": {str(i): matched_distribution[i] for i in range(1, 6)},
              "coverage": {key: coverage[key] for key in ("total_ratings", "period_ratings", "filtered_ratings", "missing_movie_ratings", "missing_genre_ratings", "matched_ratings")},
              "groups": {"total": len(groups), "eligible": eligible_count, "returned": len(rows),
                         "below_min_ratings": len(groups) - eligible_count},
              "method": {"mean_rating": "评分值之和 / 评分次数（按评分事件加权）",
                         "user_count": "分组内不同 user_id 的数量；评分次数可能大于人数",
                         "genre_counting": "多类型电影的每条评分计入其每个类型；类型间计数不能相加作为总量",
                         "ranking": "按选定指标降序，同分按评分次数降序及 ID 排序",
                         "scope": "观察性描述统计；不能证明因果、统计显著性或对全体观众的代表性"},
              "validation": {"status": "insufficient" if not rows else "limited" if warnings else "passed",
                             "checks": checks, "warnings": warnings},
              "evidence": f"runs/{report['task_id']}/analyses/{analysis_id}.json"}
    destination.parent.mkdir(exist_ok=True)
    temporary = destination.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {**result, "reused": False}
