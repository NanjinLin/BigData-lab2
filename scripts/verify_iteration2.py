#!/usr/bin/env python3
"""Verify a portable iteration-two experiment, including actual saved transforms."""
import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from service.classification import predict, training_report as classifier_report, _sha
from service.preferences import artifact, training_report
from service.tasks import TaskService


def verify(project, task_id):
    import joblib
    import numpy as np
    tasks = TaskService(project)
    directory = tasks._directory(task_id)
    source = tasks.report(task_id)
    report = training_report(directory, source)
    root = directory / "preferences" / report["experiment_id"]
    for name, item in report["artifacts"].items():
        if Path(name).name != name or not (root / name).is_file() or _sha(root / name) != item["sha256"]:
            raise ValueError(f"模型或结果校验失败：{name}")
    profiles = artifact(directory, report, "profiles.json")
    x = joblib.load(root / "scaler.joblib").transform(np.array([u["features"] for u in profiles["users"]]))
    ids = [u["user_id"] for u in profiles["users"]]
    for algorithm in ("kmeans", "dbscan"):
        model = joblib.load(root / f"{algorithm}.joblib")
        saved = artifact(directory, report, algorithm + ".json")
        assert ids == [u["user_id"] for u in saved["users"]]
        np.testing.assert_array_equal(model.labels_, [u["cluster"] for u in saved["users"]])
        if algorithm == "kmeans":
            np.testing.assert_array_equal(model.predict(x), model.labels_)
    for algorithm in ("pca", "truncated_svd"):
        saved = artifact(directory, report, algorithm + ".json")
        assert ids == [u["user_id"] for u in saved["users"]]
        coords = np.array([u["coordinates"] for u in saved["users"]])
        transformed = joblib.load(root / f"{algorithm}.joblib").transform(x)
        np.testing.assert_allclose(transformed, coords, rtol=1e-8, atol=1e-8)
        for clusterer in ("kmeans", "dbscan"):
            model = joblib.load(root / f"{algorithm}_{clusterer}.joblib")
            np.testing.assert_array_equal(model.labels_, [u["clusters"][clusterer] for u in saved["users"]])
    classifiers = classifier_report(directory, source)
    for algorithm in ("logistic_regression", "decision_tree"):
        # Predict uses the model's integrity checks and verifies its internal version.
        record = classifiers["models"][algorithm]
        model_path = directory / "classification" / record["model_file"]
        if _sha(model_path) != record["model_sha256"]:
            raise ValueError(f"分类模型校验失败：{algorithm}")
        model = joblib.load(model_path)
        predict(directory, source, min(model["users"]), min(model["movies"]), algorithm)
    return {"status": "verified", "task_id": task_id, "source": report["source"],
            "preference_experiment": report["experiment_id"], "classifier_experiment": classifiers["experiment_id"],
            "checked_preference_artifacts": len(report["artifacts"]), "checked_classifiers": 2,
            "eligible_users": len(ids), "excluded_users": len(profiles["excluded_users"])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    try:
        from threadpoolctl import threadpool_limits
        with threadpool_limits(limits=1):
            result = verify(PROJECT, args.task_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"校验失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
