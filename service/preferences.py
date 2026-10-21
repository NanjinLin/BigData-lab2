"""Train-only user profiles, two clusterers, and two reduction experiments.

Every published artifact belongs to an immutable experiment directory. The active
report is replaced only after all models, coordinates and checksums are saved.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from .classification import (ClassificationError, GENRE_NAMES, _cutoffs,
                             _load_clean, _now, _save_json, _sha)

VERSION = "movielens-preferences-v2.0"
CLUSTERERS = ("kmeans", "dbscan")
REDUCERS = ("pca", "truncated_svd")
DEFAULTS = {"min_history": 5, "n_clusters": 4, "eps": 3.0,
            "min_samples": 10, "dimensions": 2, "retention": None}
FEATURES = ([f"genre_share_{g}" for g in GENRE_NAMES]
            + [f"genre_mean_{g}" for g in GENRE_NAMES]
            + [f"genre_high_ratio_{g}" for g in GENRE_NAMES]
            + ["rating_count_log1p", "rating_mean", "high_ratio"])


class PreferenceError(ValueError):
    pass


def options(value: dict | None = None) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise PreferenceError("画像分析包含未支持的参数。")
    out = {**DEFAULTS, **value}
    for key, low, high in (("min_history", 1, 10000), ("n_clusters", 2, 20),
                           ("min_samples", 2, 1000), ("dimensions", 1, len(FEATURES) - 1)):
        if type(out[key]) is not int or not low <= out[key] <= high:
            raise PreferenceError(f"{key} 必须是 {low} 至 {high} 的整数。")
    if (type(out["eps"]) not in (int, float) or not math.isfinite(out["eps"])
            or not 0 < out["eps"] <= 100):
        raise PreferenceError("eps 必须是 (0,100] 范围内的有限数。")
    if out["retention"] is not None and (type(out["retention"]) not in (float, int)
            or not math.isfinite(out["retention"]) or not 0 < out["retention"] <= 1):
        raise PreferenceError("retention 必须为空或 (0,1] 范围内的方差保留阈值。")
    out["eps"] = float(out["eps"])
    return out


def _code_hash():
    # Parsing rules and temporal boundaries are part of this implementation too.
    h = hashlib.sha256()
    for name in ("preferences.py", "classification.py", "analysis.py"):
        h.update((Path(__file__).parent / name).read_bytes())
    return h.hexdigest()


def _source(report):
    return {k: report.get(k) for k in ("task_id", "clean_data_version", "clean_data_sha256",
                                      "rule_version", "rule_sha256", "T1", "T2")}


def build_profiles(rows, users, movies, cutoff, minimum):
    """Only events <= T1 participate; no validation/test labels or histories."""
    import numpy as np
    counts, totals, high = Counter(), Counter(), Counter()
    genre_n, genre_sum, genre_high = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    liked = defaultdict(Counter)
    missing_genre_events = 0
    for uid, mid, rating, stamp in rows:
        if stamp > cutoff:
            continue
        counts[uid] += 1
        totals[uid] += rating
        high[uid] += int(rating >= 4)
        genres = movies[mid][1]
        if not genres:
            missing_genre_events += 1
        for g in genres:
            genre_n[uid][g] += 1
            genre_sum[uid][g] += rating
            genre_high[uid][g] += int(rating >= 4)
        if rating >= 4:
            liked[uid][mid] += 1
    ids = sorted(uid for uid in users if counts[uid] >= minimum)
    excluded = [{"user_id": uid, "train_rating_count": counts[uid],
                 "status": "insufficient_history", "reason": f"训练期评分不足 {minimum} 条。"}
                for uid in sorted(users) if counts[uid] < minimum]
    matrix = []
    for uid in ids:
        n = counts[uid]
        gn = genre_n[uid]
        denominator = sum(gn.values())
        matrix.append([gn[g] / denominator if denominator else 0. for g in GENRE_NAMES]
                      + [genre_sum[uid][g] / gn[g] / 5 if gn[g] else 0. for g in GENRE_NAMES]
                      + [genre_high[uid][g] / gn[g] if gn[g] else 0. for g in GENRE_NAMES]
                      + [math.log1p(n), totals[uid] / n / 5, high[uid] / n])
    return ids, np.asarray(matrix, dtype=np.float64), excluded, counts, liked, missing_genre_events


def _quality(x, labels):
    import numpy as np
    from sklearn.metrics import silhouette_score, calinski_harabasz_score, davies_bouldin_score
    mask = labels != -1
    a, b = x[mask], labels[mask]
    result = {"clusters": int(len(np.unique(b))), "noise_count": int((~mask).sum()),
              "noise_ratio": float((~mask).mean()), "evaluated_users": len(b),
              "silhouette": None, "calinski_harabasz": None, "davies_bouldin": None,
              "silhouette_sample_limit": 1000, "metric_note": "噪声点不参与三项内部指标；轮廓系数固定随机种子42，最多抽样1000人。"}
    if not 2 <= result["clusters"] < len(b):
        result["unavailable_reason"] = "非噪声群体不足两个，或每个样本独占一群，内部指标不适用。"
        return result
    sample = np.random.RandomState(42).choice(len(b), min(1000, len(b)), replace=False)
    # A sampled single cluster cannot support silhouette; never invent a score.
    if 2 <= len(np.unique(b[sample])) < len(sample):
        result["silhouette"] = float(silhouette_score(a[sample], b[sample]))
    else:
        result["silhouette_unavailable_reason"] = "抽样中可用群体不足，轮廓系数不适用。"
    result["calinski_harabasz"] = float(calinski_harabasz_score(a, b))
    result["davies_bouldin"] = float(davies_bouldin_score(a, b))
    return result


def _cluster(x, algorithm, config):
    from sklearn.cluster import KMeans, DBSCAN
    if algorithm == "kmeans":
        parameters = {"n_clusters": config["n_clusters"], "n_init": 10, "random_state": 42}
        estimator = KMeans(**parameters)
    else:
        parameters = {"eps": config["eps"], "min_samples": config["min_samples"], "metric": "euclidean"}
        estimator = DBSCAN(**parameters)
    started = time.perf_counter()
    labels = estimator.fit_predict(x)
    seconds = time.perf_counter() - started
    return estimator, labels, {"algorithm": algorithm, "parameters": parameters,
                              "fit_seconds": seconds, "metrics": _quality(x, labels)}


def _summaries(ids, raw, labels, counts, liked, movies):
    import numpy as np
    result = []
    for label in sorted(set(labels.tolist())):
        positions = np.flatnonzero(labels == label)
        mean = raw[positions].mean(axis=0)
        top = np.argsort(-mean[:len(GENRE_NAMES)])[:3]
        representatives = Counter()
        for i in positions:
            representatives.update(liked[ids[i]])
        result.append({"cluster": int(label), "noise": bool(label == -1), "users": len(positions),
                       "mean_train_ratings": float(np.mean([counts[ids[i]] for i in positions])),
                       "mean_rating": float(mean[-2] * 5), "high_ratio": float(mean[-1]),
                       "top_genres": [{"genre": GENRE_NAMES[g], "mean_share": float(mean[g])}
                                      for g in top if mean[g] > 0],
                       "representative_movies": [{"movie_id": mid, "title": movies[mid][0], "train_high_ratings": n}
                                                 for mid, n in sorted(representatives.items(), key=lambda p: (-p[1], p[0]))[:5]],
                       "interpretation": "训练期偏好统计摘要；群体编号不是排序，噪声不是异常用户。"})
    return result


def _reduce(x, algorithm, config):
    import numpy as np
    from sklearn.decomposition import PCA, TruncatedSVD
    maximum = min(x.shape[0] - 1, x.shape[1] - 1)
    if config["dimensions"] > maximum and config["retention"] is None:
        raise PreferenceError(f"当前画像最多支持 {maximum} 个降维维数。")
    k = maximum if config["retention"] is not None else config["dimensions"]
    estimator = (PCA(n_components=k, svd_solver="full") if algorithm == "pca" else
                 TruncatedSVD(n_components=k, algorithm="randomized", n_iter=7, random_state=42))
    started = time.perf_counter()
    coords = estimator.fit_transform(x)
    ratios = np.nan_to_num(estimator.explained_variance_ratio_, nan=0.0)
    if config["retention"] is not None:
        cumulative = np.cumsum(ratios)
        found = np.flatnonzero(cumulative + 1e-12 >= config["retention"])
        if not len(found):
            raise PreferenceError(f"{algorithm} 在最多 {maximum} 维下无法达到方差保留阈值 {config['retention']}。")
        k = int(found[0]) + 1
        # Fit the actual persisted model at its selected dimension.
        estimator.set_params(n_components=k)
        coords = estimator.fit_transform(x)
        ratios = np.nan_to_num(estimator.explained_variance_ratio_, nan=0.0)
        if float(ratios.sum()) + 1e-8 < config["retention"]:
            raise PreferenceError(f"{algorithm} 实际拟合未达到指定保留阈值，请调整维数。")
    seconds = time.perf_counter() - started
    reconstruction = estimator.inverse_transform(coords)
    return estimator, coords, {"algorithm": algorithm, "parameters": {"dimensions": k,
                              "requested_dimensions": config["dimensions"], "retention": config["retention"],
                              "solver": "full" if algorithm == "pca" else "randomized", "random_state": 42},
                              "original_dimensions": x.shape[1], "actual_dimensions": k,
                              "explained_variance_ratio": ratios.tolist(), "retained_variance": float(ratios.sum()),
                              "reconstruction_mse": float(np.mean((x - reconstruction) ** 2)),
                              "fit_transform_seconds": seconds, "input_matrix_bytes": x.nbytes,
                              "coordinate_bytes": coords.nbytes,
                              "information_note": "方差保留率是数值压缩指标，不代表保留了同等比例的语义或预测能力。"}


def train(directory: Path, report: dict, config: dict | None = None, *, notify=None):
    import joblib
    import numpy as np
    from sklearn import __version__ as sklearn_version
    from sklearn.metrics import adjusted_rand_score
    from sklearn.preprocessing import StandardScaler
    from threadpoolctl import threadpool_limits
    config = options(config)
    stage = notify or (lambda _: None)
    if report.get("status") != "completed" or not all(report.get(k) for k in ("task_id", "clean_data_version", "clean_data_sha256")):
        raise PreferenceError("缺少已完成清洗报告或数据版本。")
    try:
        t1, t2 = _cutoffs(report)
        stage("verify_clean_data")
        rows, users, movies = _load_clean(directory, report)
    except ClassificationError as exc:
        raise PreferenceError(str(exc)) from exc
    periods = Counter("train" if r[3] <= t1 else "validation" if r[3] <= t2 else "test" for r in rows)
    if not report.get("period_counts") or any(periods[k] != report["period_counts"].get(k) for k in ("train", "validation", "test")):
        raise PreferenceError("时间切分样本量与清洗报告不一致。")
    stage("build_train_profiles")
    ids, raw, excluded, counts, liked, missing = build_profiles(rows, users, movies, t1, config["min_history"])
    if len(ids) <= max(config["n_clusters"], 2):
        raise PreferenceError("训练期历史足够的用户数量不足，无法完成两种算法对照。")
    if not np.any(np.var(raw, axis=0) > 0):
        raise PreferenceError("画像没有可用变化，无法评价降维和聚类。")
    scaler = StandardScaler(with_mean=False)
    x = scaler.fit_transform(raw)
    identity = {"version": VERSION, "source": _source(report), "config": config,
                "implementation_sha256": _code_hash(), "sklearn": sklearn_version}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    # Unique run suffix preserves actual timings and models even on same-config reruns.
    experiment = f"pref-{fingerprint}-{uuid.uuid4().hex[:8]}"
    root = directory / "preferences"
    destination = root / experiment
    destination.mkdir(parents=True)
    artifacts, clusterings, reductions = {}, {}, {}

    def save(name, value, binary=False):
        path = destination / name
        if binary:
            joblib.dump(value, path, compress=3)
        else:
            _save_json(path, value)
        artifacts[name] = {"sha256": _sha(path), "bytes": path.stat().st_size}

    profiles = [{"user_id": uid, "status": "completed", "train_rating_count": counts[uid],
                 "features": raw[i].tolist()} for i, uid in enumerate(ids)]
    save("profiles.json", {"feature_names": FEATURES, "users": profiles, "excluded_users": excluded,
                           "source": _source(report), "profile_cutoff": report["T1"]})
    save("scaler.joblib", scaler, True)
    # Bound BLAS threads in the worker rather than allowing nested CPU oversubscription.
    with threadpool_limits(limits=1):
        for algorithm in CLUSTERERS:
            stage(f"cluster_{algorithm}")
            model, labels, item = _cluster(x, algorithm, config)
            item["summaries"] = _summaries(ids, raw, labels, counts, liked, movies)
            item["model_version"] = f"{experiment}-{algorithm}"
            save(f"{algorithm}.joblib", model, True)
            save(f"{algorithm}.json", {"algorithm": algorithm, "users": [
                {"user_id": uid, "cluster": int(labels[i]), "noise": bool(labels[i] == -1),
                 "train_rating_count": counts[uid]} for i, uid in enumerate(ids)]})
            clusterings[algorithm] = item
        for algorithm in REDUCERS:
            stage(f"reduce_{algorithm}")
            model, z, item = _reduce(x, algorithm, config)
            item["model_version"] = f"{experiment}-{algorithm}"
            item["comparisons"] = {}
            projected_labels = {}
            for clusterer in CLUSTERERS:
                reduced_model, labels, compared = _cluster(z, clusterer, config)
                original = json.loads((destination / f"{clusterer}.json").read_text(encoding="utf-8"))
                before = np.asarray([u["cluster"] for u in original["users"]])
                compared["adjusted_rand_vs_original"] = float(adjusted_rand_score(before, labels))
                compared["comparison_note"] = "前后使用相同用户与相同参数；DBSCAN的欧氏距离尺度改变，固定eps不保证相同密度含义。ARI将噪声标签-1作为一类。"
                compared["summaries"] = _summaries(ids, raw, labels, counts, liked, movies)
                save(f"{algorithm}_{clusterer}.joblib", reduced_model, True)
                projected_labels[clusterer] = labels
                item["comparisons"][clusterer] = compared
            save(f"{algorithm}.joblib", model, True)
            save(f"{algorithm}.json", {"algorithm": algorithm, "source": _source(report),
                 "original_dimensions": len(FEATURES), "actual_dimensions": z.shape[1],
                 "users": [{"user_id": uid, "coordinates": z[i].tolist(),
                            "clusters": {a: int(projected_labels[a][i]) for a in CLUSTERERS}}
                           for i, uid in enumerate(ids)],
                 "plot_note": "二维图使用前两个分量；一维模型的第二坐标为0。聚类使用全部实际降维维数。"})
            item["model_bytes"] = artifacts[f"{algorithm}.joblib"]["bytes"]
            reductions[algorithm] = item
    stage("save_report")
    outcome = {"status": "completed", "task_id": report["task_id"], "experiment_id": experiment,
               "version": VERSION, "implementation_sha256": _code_hash(), "created_at": _now(),
               "source": _source(report), "parameters": config, "feature_names": FEATURES,
               "profile_dimensions": len(FEATURES), "eligible_users": len(ids), "excluded_users": len(excluded),
               "missing_genre_train_events": missing, "split_counts": dict(periods),
               "scaler": "StandardScaler(with_mean=False), fitted on eligible training profiles only",
               "profile_policy": "仅Timestamp<=T1的评分构建画像；验证/测试评分不参与画像、缩放、聚类或降维。用户ID和人口属性不作为偏好特征。",
               "selection_policy": "无监督参数为预先指定配置，未以测试集选择；内部聚类指标描述训练期画像结构，不是测试集预测准确率。",
               "library": f"scikit-learn {sklearn_version}", "clusterings": clusterings,
               "reductions": reductions, "artifacts": artifacts,
               "limitations": ["训练期不足门槛的用户不分配群体或坐标，已在画像文件中逐人登记。",
                               "类型缺失的训练评分参与总体均分与次数，不参与类型偏好；类型均分中的0表示无已知该类型历史。",
                               "PCA对输入中心化，SVD直接分解未中心化的缩放画像，因此方向和方差保留可能不同。",
                               "运行秒数受机器和缓存影响；矩阵字节数与模型文件大小是实测资源指标，不等于进程峰值内存。",
                               "群体描述是训练历史统计，不是因果解释或对未来行为的事实判断。"]}
    _save_json(destination / "report.json", outcome)
    _save_json(root / "report.json", outcome)
    stage("completed")
    return outcome


def training_report(directory: Path, source: dict):
    try:
        report = json.loads((directory / "preferences" / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PreferenceError("聚类与降维尚未完成，请先运行画像分析。") from exc
    if (not isinstance(report, dict) or report.get("status") != "completed" or report.get("source") != _source(source)
            or report.get("implementation_sha256") != _code_hash()):
        raise PreferenceError("画像模型与当前净表、T1/T2或代码版本不一致，请重新训练。")
    if (not isinstance(report.get("experiment_id"), str)
            or not re.fullmatch(r"pref-[0-9a-f]{16}-[0-9a-f]{8}", report["experiment_id"])
            or not isinstance(report.get("artifacts"), dict)
            or not isinstance(report.get("clusterings"), dict) or set(report["clusterings"]) != set(CLUSTERERS)
            or not isinstance(report.get("reductions"), dict) or set(report["reductions"]) != set(REDUCERS)):
        raise PreferenceError("画像报告结构损坏，请重新训练。")
    clean = directory / "cleaned.tsv"
    if not clean.is_file() or _sha(clean) != source.get("clean_data_sha256"):
        raise PreferenceError("完整净表缺失或SHA-256不一致，分析结果不可用。")
    return report


def artifact(directory: Path, report: dict, name: str):
    if name not in report["artifacts"] or not name.endswith(".json"):
        raise PreferenceError("未登记的可下载结果文件。")
    # Only generated names from the fixed manifest; never accept an arbitrary path.
    if Path(name).name != name or "/" in name or "\\" in name:
        raise PreferenceError("结果文件名无效。")
    path = directory / "preferences" / report["experiment_id"] / name
    if not path.is_file() or _sha(path) != report["artifacts"][name]["sha256"]:
        raise PreferenceError("分析产物不存在或SHA-256校验失败，请重新训练。")
    return json.loads(path.read_text(encoding="utf-8"))


class PreferenceService:
    def __init__(self, tasks):
        self.tasks = tasks
        self.lock = tasks.lock
        self.running = set()
        tasks.task_users["preferences"] = self.running

    def result(self, task_id):
        return training_report(self.tasks._directory(task_id), self.tasks.report(task_id))

    def data(self, task_id, name):
        return artifact(self.tasks._directory(task_id), self.result(task_id), name)

    def status(self, task_id):
        self.tasks.report(task_id)
        path = self.tasks._directory(task_id) / "preferences" / "status.json"
        if path.is_file():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise PreferenceError("画像分析状态文件损坏。") from exc
            if state["state"] in ("queued", "running"):
                if task_id not in self.running:
                    return {**state, "state": "interrupted", "error": "服务已中断，可重新运行画像分析。"}
                return state
            if state["state"] == "failed":
                return state
        try:
            result = self.result(task_id)
            return {"state": "completed", "stage": "completed", "experiment_id": result["experiment_id"]}
        except PreferenceError as exc:
            return {"state": "not_started", "stage": "idle", "message": str(exc)}

    def start(self, task_id, config=None):
        config = options(config)
        with self.lock:
            report = self.tasks.report(task_id)
            directory = self.tasks._directory(task_id)
            if not (directory / "cleaned.tsv").is_file():
                raise PreferenceError("该任务缺少完整cleaned.tsv，不能运行画像分析。")
            if self.running:
                raise PreferenceError("已有画像分析正在运行，请完成后再提交。")
            self.running.add(task_id)
        def save(state, stage, error=None):
            _save_json(directory / "preferences" / "status.json",
                       {"task_id": task_id, "state": state, "stage": stage, "error": error,
                        "parameters": config, "updated_at": _now()})
        try:
            save("queued", "waiting_for_worker")
        except Exception:
            with self.lock:
                self.running.discard(task_id)
            raise
        def work():
            current_stage = "waiting_for_worker"
            def progress(stage):
                nonlocal current_stage
                current_stage = stage
                save("running", stage)
            try:
                train(directory, report, config, notify=progress)
                save("completed", "completed")
            except Exception as exc:
                save("failed", current_stage, str(exc))
            finally:
                with self.lock:
                    self.running.discard(task_id)
        threading.Thread(target=work, daemon=True, name=f"preferences-{task_id}").start()
        return {"task_id": task_id, "state": "queued", "stage": "waiting_for_worker", "parameters": config}

    def query(self, task_id, category, algorithm, user_ids=None, parameters=None, comparison_algorithm="both"):
        allowed = CLUSTERERS if category == "cluster" else REDUCERS if category == "reduce" else ()
        if not isinstance(algorithm, str) or algorithm not in allowed:
            raise PreferenceError("未登记的聚类或降维算法。")
        if not isinstance(comparison_algorithm, str) or comparison_algorithm not in (*CLUSTERERS, "both"):
            raise PreferenceError("对照聚类算法必须是kmeans、dbscan或both。")
        if user_ids is None:
            user_ids = []
        if (not isinstance(user_ids, list) or len(user_ids) > 200 or len(set(map(str, user_ids))) != len(user_ids)
                or any(type(uid) is not int or uid <= 0 for uid in user_ids)):
            raise PreferenceError("user_ids需为最多200个不重复的正整数。")
        report = self.result(task_id)
        if parameters is not None:
            if not isinstance(parameters, dict) or set(parameters) - DEFAULTS.keys():
                raise PreferenceError("查询含未支持的算法参数。")
            requested = options({**report["parameters"], **parameters})
            if requested != report["parameters"]:
                raise PreferenceError("当前已登记结果的参数与请求不同，请用指定参数重新运行画像分析。")
        root = self.tasks._directory(task_id)
        result = artifact(root, report, f"{algorithm}.json")
        profile = artifact(root, report, "profiles.json")
        available = {u["user_id"]: u for u in result["users"]}
        excluded = {u["user_id"]: u for u in profile["excluded_users"]}
        wanted = user_ids or list(available)[:30]
        answers = [available[uid] if uid in available else excluded[uid] if uid in excluded else
                   {"user_id": uid, "status": "unknown_user", "reason": "UserID不存在于已登记净表。"}
                   for uid in wanted]
        details = report["clusterings" if category == "cluster" else "reductions"][algorithm]
        comparisons = {}
        if category == "reduce":
            selected = CLUSTERERS if comparison_algorithm == "both" else (comparison_algorithm,)
            details = {**details, "comparisons": {a: details["comparisons"][a] for a in selected}}
            comparisons = {"original_clusterings": {a: report["clusterings"][a] for a in selected},
                           "comparison_algorithm": comparison_algorithm}
        return {"status": "completed", "category": category, "algorithm": algorithm,
                "experiment_id": report["experiment_id"], "source": report["source"],
                "population_users": report["eligible_users"], "returned_users": len(answers),
                "sample_only": not bool(user_ids), "users": answers, "model": details, **comparisons,
                "subset_policy": "模型在全部历史足够的训练用户上拟合；所选用户集合仅用于查询，不重新拟合子集。",
                "limitations": report["limitations"],
                "evidence": f"runs/{task_id}/preferences/{report['experiment_id']}/report.json"}
