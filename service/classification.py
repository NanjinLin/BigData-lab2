"""Time-safe classification over a registered Hadoop-cleaned MovieLens task.

Only training-period ratings update features. Training events use *strictly*
earlier timestamps; validation, test and online predictions use the same frozen
T1 training snapshot. No outcome from validation/test is used as a feature.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .analysis import GENRES, _record, AnalysisError

VERSION = "movielens-high-rating-v2.0"
# Iteration-1 cleaning intentionally masks uncertain metadata rather than dropping its ID.
NULL_MARKER = "\\N"
VALID_AGES = frozenset((1, 18, 25, 35, 45, 50, 56))
ALGORITHMS = ("logistic_regression", "decision_tree")
GENRE_NAMES = tuple(sorted(GENRES))
FEATURE_NAMES = [
    "user_rating_count_log1p", "user_rating_mean", "user_high_rating_ratio",
    "movie_rating_count_log1p", "movie_rating_mean", "movie_high_rating_ratio",
    "user_movie_genre_count_log1p", "user_movie_genre_mean", "user_movie_genre_high_ratio",
    "user_known", "movie_known", "user_movie_genre_known",
    "user_gender_male", "user_age_code", "user_occupation_code",
    "user_gender_known", "user_age_known", "user_occupation_known", "movie_release_year",
    "movie_genre_count",
] + ["genre_" + name for name in GENRE_NAMES]
MODEL_CONFIG = {"logistic_regression": {"C": [0.1, 1.0, 10.0], "max_iter": 250,
                                         "tol": 1e-3, "random_state": 42},
                "decision_tree": {"max_depth": [6, 10, 14], "min_samples_leaf": 30,
                                  "random_state": 42}}


class ClassificationError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for part in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def _cutoffs(report: dict) -> tuple[int, int]:
    try:
        a, b = (int(datetime.fromisoformat(report[k].replace("Z", "+00:00")).timestamp())
                for k in ("T1", "T2"))
        if a >= b:
            raise ValueError()
        return a, b
    except (KeyError, ValueError, AttributeError, TypeError) as exc:
        raise ClassificationError("正式报告缺少有效的 T1/T2 时间边界。") from exc


def _score(values):
    n, total, high = values
    return (math.log1p(n), total / n if n else 0., high / n if n else 0.)


def _empty_counts():
    return [0, 0, 0]


class History:
    """Mutable training-only state; serialized for immutable online inference."""
    def __init__(self):
        self.users = defaultdict(_empty_counts)
        self.movies = defaultdict(_empty_counts)
        self.user_genres = defaultdict(_empty_counts)

    def update(self, uid: int, mid: int, rating: int, genres: tuple[str, ...]):
        for data in (self.users[uid], self.movies[mid]):
            data[0] += 1
            data[1] += rating
            data[2] += int(rating >= 4)
        for genre in genres:
            data = self.user_genres[(uid, genre)]
            data[0] += 1
            data[1] += rating
            data[2] += int(rating >= 4)

    def features(self, uid: int, mid: int, users: dict, movies: dict):
        info, genres, year = movies[mid]
        a = self.users.get(uid, (0, 0, 0))
        b = self.movies.get(mid, (0, 0, 0))
        stats = [self.user_genres.get((uid, g), (0, 0, 0)) for g in genres]
        n = sum(x[0] for x in stats)
        genre_total = sum(x[1] for x in stats)
        genre_high = sum(x[2] for x in stats)
        u_gender, u_age, u_occupation = users[uid]
        return [*_score(a), *_score(b), *_score((n, genre_total, genre_high)),
                float(a[0] > 0), float(b[0] > 0), float(n > 0),
                float(u_gender == "M"), float(u_age or 0) / 100.,
                float(u_occupation if u_occupation is not None else 0) / 20.,
                float(u_gender is not None), float(u_age is not None),
                float(u_occupation is not None),
                (year - 1900.) / 120. if year else 0., float(len(genres)),
                *(float(g in genres) for g in GENRE_NAMES)]


def _optional_user_code(value: str, field_name: str, allowed: frozenset[int]) -> int | None:
    """Interpret the clean rule's masked attribute as unknown, not an invalid row."""
    if value == NULL_MARKER:
        return None
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"users.{field_name} 无法解析：{value!r}") from exc
    if result not in allowed:
        raise ValueError(f"users.{field_name} 超出允许范围：{value!r}")
    return result


def _load_clean(directory: Path, report: dict):
    """Verify complete cleaned table against its exact registered report."""
    source = directory / "cleaned.tsv"
    if not source.is_file():
        raise ClassificationError("完整净表 cleaned.tsv 不存在，请在该任务目录恢复净表或重新运行 Hadoop 清洗。")
    before = source.stat()
    digest = hashlib.sha256()
    rows, users, movies, counts = [], {}, {}, Counter()
    with source.open("rb") as stream:
        for line_number, line in enumerate(stream, start=1):
            digest.update(line)
            table = "unknown"
            try:
                table, fields = _record(line)
                counts[table] += 1
                if table == "users":
                    uid = int(fields[0])
                    if uid <= 0 or uid in users:
                        raise ValueError(f"无效或重复的 UserID：{uid}")
                    gender = fields[1]
                    if gender not in ("M", "F", NULL_MARKER):
                        raise ValueError(f"users.gender 非法值：{gender!r}")
                    age = _optional_user_code(fields[2], "age", VALID_AGES)
                    occupation = _optional_user_code(fields[3], "occupation", frozenset(range(21)))
                    users[uid] = (None if gender == NULL_MARKER else gender, age, occupation)
                elif table == "movies":
                    mid = int(fields[0])
                    if mid <= 0 or mid in movies:
                        raise ValueError(f"无效或重复的 MovieID：{mid}")
                    year_match = re.search(r"\((\d{4})\)\s*$", fields[1])
                    genres = tuple(dict.fromkeys(g for g in fields[2].split("|") if g in GENRES))
                    movies[mid] = (fields[1], genres, int(year_match.group(1)) if year_match else None)
                else:
                    uid, mid, rating, timestamp = map(int, fields)
                    if uid <= 0 or mid <= 0 or rating not in (1, 2, 3, 4, 5) or timestamp < 0:
                        raise ValueError(f"评分记录字段超出允许范围：UserID={uid}, MovieID={mid}")
                    rows.append((uid, mid, rating, timestamp))
            except (AnalysisError, ValueError, TypeError, OverflowError) as exc:
                raise ClassificationError(f"净表第 {line_number} 行（{table}）无法用于分类：{exc}") from exc
    if digest.hexdigest() != report.get("clean_data_sha256"):
        raise ClassificationError("净表 SHA-256 与清洗报告不一致，拒绝训练。")
    for table in ("ratings", "users", "movies"):
        expected = report.get("cleaning", {}).get("tables", {}).get(table, {}).get("output")
        if counts[table] != expected:
            raise ClassificationError(f"{table} 净表数量与报告不一致，拒绝训练。")
    if any(uid not in users or mid not in movies for uid, mid, _, _ in rows):
        raise ClassificationError("存在无法关联用户/电影的评分，不可训练。")
    after = source.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ClassificationError("净表在读取期间发生变化，请重试。")
    return rows, users, movies


def _split_features(rows, users, movies, bounds):
    """Construct event features before incorporating ANY event at same timestamp."""
    import numpy as np
    train = sorted((r for r in rows if r[3] <= bounds[0]), key=lambda r: r[3])
    valid = [r for r in rows if bounds[0] < r[3] <= bounds[1]]
    test = [r for r in rows if r[3] > bounds[1]]
    if not train or not valid or not test:
        raise ClassificationError("训练、验证、测试时间段中存在空集合，无法评价分类模型。")
    hist = History()
    features = np.empty((len(train), len(FEATURE_NAMES)), dtype=np.float32)
    targets = np.empty(len(train), dtype=np.int8)
    i = 0
    while i < len(train):
        j = i + 1
        while j < len(train) and train[j][3] == train[i][3]:
            j += 1
        for k in range(i, j):
            uid, mid, rating, _ = train[k]
            features[k] = hist.features(uid, mid, users, movies)
            targets[k] = int(rating >= 4)
        for k in range(i, j):
            uid, mid, rating, _ = train[k]
            hist.update(uid, mid, rating, movies[mid][1])
        i = j
    def frozen(items):
        matrix = np.empty((len(items), len(FEATURE_NAMES)), dtype=np.float32)
        y = np.empty(len(items), dtype=np.int8)
        for i, (uid, mid, rating, _) in enumerate(items):
            matrix[i] = hist.features(uid, mid, users, movies)
            y[i] = int(rating >= 4)
        return matrix, y
    return (features, targets), frozen(valid), frozen(test), hist, dict(zip(("train", "validation", "test"), (len(train), len(valid), len(test))))


def _metrics(y, scores):
    import numpy as np
    from sklearn.metrics import accuracy_score, precision_recall_fscore_support, roc_auc_score, brier_score_loss, confusion_matrix
    labels = (scores >= 0.5).astype(np.int8)
    p, r, f, _ = precision_recall_fscore_support(y, labels, average="binary", zero_division=0)
    return {"samples": int(len(y)), "positive_rate": round(float(np.mean(y)), 6),
            "accuracy": round(float(accuracy_score(y, labels)), 6),
            "precision": round(float(p), 6), "recall": round(float(r), 6), "f1": round(float(f), 6),
            "roc_auc": round(float(roc_auc_score(y, scores)), 6) if len(set(y.tolist())) == 2 else None,
            "brier": round(float(brier_score_loss(y, scores)), 6),
            "confusion_matrix": confusion_matrix(y, labels, labels=[0, 1]).astype(int).tolist()}


def _prob(model, x):
    import numpy as np
    classes = list(model.classes_)
    if 1 not in classes:
        raise ClassificationError("训练集不包含高分类别，模型无效。")
    return np.asarray(model.predict_proba(x)[:, classes.index(1)], dtype=np.float64)


def _model(algorithm: str, choice):
    if algorithm == "logistic_regression":
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        return make_pipeline(StandardScaler(), LogisticRegression(
            C=choice, max_iter=MODEL_CONFIG["logistic_regression"]["max_iter"],
            tol=MODEL_CONFIG["logistic_regression"]["tol"], random_state=42))
    from sklearn.tree import DecisionTreeClassifier
    return DecisionTreeClassifier(max_depth=choice, min_samples_leaf=MODEL_CONFIG["decision_tree"]["min_samples_leaf"], random_state=42)


def _code_hash():
    return _sha(Path(__file__))


def _report_path(directory):
    return directory / "classification" / "report.json"


def training_report(directory: Path, report: dict) -> dict:
    path = _report_path(directory)
    if not path.is_file():
        raise ClassificationError("尚无已完成分类训练报告，请先训练两种分类算法。")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClassificationError("分类报告无法读取。") from exc
    source = result.get("source", {})
    if (source.get("clean_data_sha256") != report.get("clean_data_sha256")
            or source.get("clean_data_version") != report.get("clean_data_version")
            or source.get("T1") != report.get("T1") or source.get("T2") != report.get("T2")
            or result.get("implementation_sha256") != _code_hash()
            or set(result.get("models", {})) != set(ALGORITHMS)):
        raise ClassificationError("分类模型和当前净表、时间边界或代码版本不一致，请重新训练。")
    return result


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def train(directory: Path, report: dict, *, notify=None) -> dict:
    """Train both algorithms. Writes no fabricated results or partial report."""
    import joblib
    from sklearn import __version__ as sklearn_version
    import numpy as np
    def stage(value):
        if notify:
            notify(value)
    bounds = _cutoffs(report)
    stage("verify_clean_data")
    rows, users, movies = _load_clean(directory, report)
    stage("build_temporal_features")
    (x_train, y_train), (x_valid, y_valid), (x_test, y_test), hist, counts = _split_features(rows, users, movies, bounds)
    if len(np.unique(y_train)) != 2:
        raise ClassificationError("训练集需要同时包含高分和非高分样本。")
    if report.get("period_counts") and any(report["period_counts"].get(k) != v for k, v in counts.items()):
        raise ClassificationError("时间切分样本量与清洗报告不一致。")
    identity = {"version": VERSION, "sha256": report["clean_data_sha256"], "code_sha256": _code_hash(),
                "T1": report["T1"], "T2": report["T2"], "config": MODEL_CONFIG}
    experiment_id = "cls-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    root = directory / "classification"
    destination = root / "models" / experiment_id
    destination.mkdir(parents=True, exist_ok=True)
    summaries = {}
    stage("train_models")
    for algorithm in ALGORITHMS:
        choices = MODEL_CONFIG[algorithm]["C" if algorithm == "logistic_regression" else "max_depth"]
        candidates = []
        for choice in choices:
            estimator = _model(algorithm, choice)
            estimator.fit(x_train, y_train)
            measured = _metrics(y_valid, _prob(estimator, x_valid))
            candidates.append((measured["f1"], measured["roc_auc"] if measured["roc_auc"] is not None else -1, choice, estimator, measured))
        # Deterministic selection; test is evaluated only AFTER selection.
        winner = max(candidates, key=lambda entry: (entry[0], entry[1]))
        _, _, choice, estimator, validation = winner
        evaluation = _metrics(y_test, _prob(estimator, x_test))
        artifact = destination / f"{algorithm}.joblib"
        tmp = destination / f"{algorithm}.pending"
        joblib.dump({"estimator": estimator, "history": hist,
                     "users": users, "movies": movies, "feature_names": FEATURE_NAMES,
                     "experiment_id": experiment_id, "algorithm": algorithm}, tmp, compress=3)
        tmp.replace(artifact)
        params = ({"C": choice, "max_iter": 250, "tol": 1e-3, "scaler": "StandardScaler"}
                  if algorithm == "logistic_regression" else
                  {"max_depth": choice, "min_samples_leaf": 30, "random_state": 42})
        summaries[algorithm] = {"model_version": f"{experiment_id}-{algorithm}",
                                "parameters": params, "validation": validation, "test": evaluation,
                                "candidates": [{"parameters": ({"C": c[2]} if algorithm == "logistic_regression" else {"max_depth": c[2], "min_samples_leaf": 30}),
                                                "validation": c[4]} for c in candidates],
                                "model_file": f"models/{experiment_id}/{algorithm}.joblib",
                                "model_sha256": _sha(artifact)}
    stage("save_report")
    outcome = {"status": "completed", "task_id": report["task_id"], "experiment_id": experiment_id,
               "version": VERSION, "implementation_sha256": _code_hash(), "created_at": _now(),
               "source": {"task_id": report["task_id"], "clean_data_version": report["clean_data_version"],
                          "clean_data_sha256": report["clean_data_sha256"], "rule_version": report.get("rule_version"),
                          "T1": report["T1"], "T2": report["T2"]},
               "target": "rating >= 4", "positive_class": 1, "decision_threshold": 0.5,
               "feature_names": FEATURE_NAMES, "feature_count": len(FEATURE_NAMES),
               "split_counts": counts, "training_positive_rate": round(float(np.mean(y_train)), 6),
               "validation_usage": "hyperparameter selection only", "test_usage": "final evaluation only",
               "feature_policy": "训练事件仅使用严格早于事件时间戳的训练历史；验证、测试及在线预测只用 T1 训练历史快照。同时间戳事件相互不可见。",
               "selection_metric": "validation F1, then validation ROC AUC", "python_library": f"scikit-learn {sklearn_version}",
               "models": summaries, "limitations": ["预测仅针对已登记的用户与电影，且表示高评分的模型估计概率，不是实际评分。",
                                                   "验证和测试特征不包含验证期或测试期评分历史，低历史量用户的预测不确定性更高。"]}
    # Keep complete experiment reports addressable after retraining, including
    # the previously active report (e.g. an earlier algorithm selection).
    history_dir = root / "reports"
    active = _report_path(directory)
    if active.is_file():
        try:
            previous = json.loads(active.read_text(encoding="utf-8"))
            old_id = previous.get("experiment_id", "")
            if re.fullmatch(r"cls-[0-9a-f]{20}", old_id) and old_id != experiment_id:
                archived = history_dir / f"{old_id}.json"
                if not archived.exists():
                    _save_json(archived, previous)
        except (OSError, ValueError, TypeError):
            pass  # An unreadable old report is not evidence for the new run.
    _save_json(history_dir / f"{experiment_id}.json", outcome)
    _save_json(active, outcome)
    stage("completed")
    return outcome


def predict(directory: Path, report: dict, user_id: int, movie_id: int, algorithm: str) -> dict:
    if algorithm not in ALGORITHMS:
        raise ClassificationError("分类算法必须是 logistic_regression 或 decision_tree。")
    if type(user_id) is not int or type(movie_id) is not int or user_id <= 0 or movie_id <= 0:
        raise ClassificationError("UserID 和 MovieID 必须是正整数。")
    saved = training_report(directory, report)
    record = saved["models"][algorithm]
    relative = record["model_file"]
    if not re.fullmatch(r"models/cls-[0-9a-f]{20}/(logistic_regression|decision_tree)\.joblib", relative):
        raise ClassificationError("模型路径记录无效。")
    model_path = directory / "classification" / relative
    if not model_path.is_file() or _sha(model_path) != record["model_sha256"]:
        raise ClassificationError("模型文件不存在或校验失败，请重新训练。")
    import joblib
    import numpy as np
    model = joblib.load(model_path)  # local server-produced artifacts only; never accept a user path
    if (model.get("experiment_id") != saved["experiment_id"] or model.get("algorithm") != algorithm
            or model.get("feature_names") != FEATURE_NAMES):
        raise ClassificationError("模型文件与报告不一致。")
    users, movies = model["users"], model["movies"]
    if user_id not in users:
        raise ClassificationError(f"未知 UserID：{user_id}。")
    if movie_id not in movies:
        raise ClassificationError(f"未知 MovieID：{movie_id}。")
    history = model["history"]
    n = history.users.get(user_id, (0, 0, 0))[0]
    feature = np.asarray([history.features(user_id, movie_id, users, movies)], dtype=np.float32)
    probability = float(_prob(model["estimator"], feature)[0])
    return {"status": "completed", "task_id": report["task_id"], "experiment_id": saved["experiment_id"],
            "algorithm": algorithm, "user_id": user_id, "movie_id": movie_id,
            "movie_title": movies[movie_id][0], "predicted_class": int(probability >= 0.5),
            "positive_probability": round(probability, 6), "decision_threshold": 0.5,
            "user_train_rating_count": n, "history_warning": "该用户在训练期无历史评分。" if not n else (
                "该用户训练期历史不足 5 条，预测不确定性较高。" if n < 5 else None),
            "model_version": record["model_version"], "parameters": record["parameters"],
            "validation": record["validation"], "test": record["test"],
            "source": saved["source"], "evidence": f"runs/{report['task_id']}/classification/report.json"}


class ClassificationService:
    """Asynchronous trainer attached to existing TaskService; no Hadoop re-run."""
    def __init__(self, tasks):
        self.tasks = tasks
        self.lock = threading.Lock()
        self.running: set[str] = set()

    def _directory(self, task_id: str) -> Path:
        return self.tasks._directory(task_id)

    def status(self, task_id: str) -> dict:
        self.tasks.report(task_id)
        path = self._directory(task_id) / "classification" / "status.json"
        if not path.is_file():
            try:
                outcome = self.result(task_id)
                return {"state": "completed", "stage": "completed", "experiment_id": outcome["experiment_id"]}
            except ClassificationError:
                return {"state": "not_started", "stage": "idle"}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ClassificationError("分类任务状态文件无法读取。") from exc
        if state.get("state") in ("running", "queued") and task_id not in self.running:
            return {**state, "state": "interrupted", "error": "服务中断，分类训练未完成，可重新提交。"}
        if state.get("state") == "completed":
            try:
                result = self.result(task_id)
            except ClassificationError:
                return {"state": "not_started", "stage": "idle", "message": "模型版本已更新，点击开始训练。"}
            return {"state": "completed", "stage": "completed", "experiment_id": result["experiment_id"]}
        return state

    def result(self, task_id: str):
        return training_report(self._directory(task_id), self.tasks.report(task_id))

    def predict(self, task_id: str, user_id: int, movie_id: int, algorithm: str):
        return predict(self._directory(task_id), self.tasks.report(task_id), user_id, movie_id, algorithm)

    def start(self, task_id: str):
        report = self.tasks.report(task_id)
        directory = self._directory(task_id)
        if not (directory / "cleaned.tsv").is_file():
            raise ClassificationError("该任务尚无完整 cleaned.tsv，不能训练分类模型。")
        with self.lock:
            if task_id in self.running:
                raise ClassificationError("当前任务的分类训练正在进行。")
            self.running.add(task_id)
        root = directory / "classification"
        def save(state, stage, error=None):
            _save_json(root / "status.json", {"task_id": task_id, "state": state, "stage": stage,
                                                      "updated_at": _now(), "error": error})
        try:
            save("queued", "waiting_for_worker")
        except Exception:
            with self.lock:
                self.running.discard(task_id)
            raise
        def work():
            try:
                train(directory, report, notify=lambda stage: save("running", stage))
                save("completed", "completed")
            except Exception as exc:
                save("failed", "failed", str(exc))
            finally:
                with self.lock:
                    self.running.discard(task_id)
        threading.Thread(target=work, daemon=True, name=f"classification-{task_id}").start()
        return {"task_id": task_id, "state": "queued", "stage": "waiting_for_worker"}
