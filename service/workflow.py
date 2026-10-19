"""Small model-authored plans with execution states and explicit evidence checks."""

from __future__ import annotations

from .tasks import TaskError
from .preference_tools import PREFERENCE_OPERATIONS


OPERATIONS = ("list_saved_tasks", "get_task_status", "read_task_report", "analyze_movielens",
              "run_hadoop_clean_and_score", "run_hadoop_with_adjusted_rules",
              "train_rating_classifiers", "get_classification_status", "read_classification_report",
              "predict_high_rating_logistic_regression", "predict_high_rating_decision_tree", *PREFERENCE_OPERATIONS)
PLAN_TOOL = {
    "type": "function", "name": "set_analysis_plan", "strict": True,
    "description": "在读取报告、统计或清洗前先登记简短计划。目标来自当前用户请求，只列必要工具；已有净表可直接复用。普通聊天无需计划。",
    "parameters": {"type": "object", "properties": {
        "goal": {"type": "string"}, "needed_data": {"type": "array", "items": {"type": "string"}},
        "expected_result": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "tool": {"type": "string", "enum": list(OPERATIONS)}, "purpose": {"type": "string"}},
            "required": ["tool", "purpose"], "additionalProperties": False}}},
        "required": ["goal", "needed_data", "expected_result", "steps"], "additionalProperties": False},
}
ASSESS_TOOL = {
    "type": "function", "name": "assess_analysis_goal", "strict": True,
    "description": "执行计划后、最终回复前检查结果能否回答原问题。依据本轮真实工具结果，说明不足；异步提交不能视作分析完成。服务端同时核验步骤与数据。",
    "parameters": {"type": "object", "properties": {
        "answerable": {"type": "boolean"}, "reason": {"type": "string"},
        "limitations": {"type": "array", "items": {"type": "string"}}},
        "required": ["answerable", "reason", "limitations"], "additionalProperties": False},
}


def _text(value, maximum=500):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def make_plan(args: dict) -> dict:
    if (set(args) != {"goal", "needed_data", "expected_result", "steps"}
            or not _text(args["goal"]) or not _text(args["expected_result"])
            or not isinstance(args["needed_data"], list) or not 1 <= len(args["needed_data"]) <= 8
            or not all(_text(item, 200) for item in args["needed_data"])
            or not isinstance(args["steps"], list) or not 1 <= len(args["steps"]) <= 6):
        raise TaskError("计划需包含目标、所需数据、预期结果和 1 至 6 个必要步骤。", 422)
    steps = []
    for step in args["steps"]:
        if (not isinstance(step, dict) or set(step) != {"tool", "purpose"}
                or step["tool"] not in OPERATIONS or not _text(step["purpose"], 200)):
            raise TaskError("计划中存在无效步骤或未登记的工具。", 422)
        steps.append({**step, "state": "pending", "detail": "等待执行"})
    return {"goal": args["goal"], "needed_data": args["needed_data"],
            "expected_result": args["expected_result"], "steps": steps,
            "state": "running", "validation": None}


def begin_step(plan: dict | None, tool: str) -> dict | None:
    if plan:
        for step in plan["steps"]:
            if step["tool"] == tool and step["state"] in ("pending", "failed"):
                step.update(state="running", detail="正在执行")
                plan["validation"] = None
                plan["state"] = "running"
                return step
    if tool in ("list_saved_tasks", "get_task_status"):
        return None
    raise TaskError("先调用 set_analysis_plan，将本工具列入必要步骤后再执行；不要为普通聊天启动分析。", 422)


def assess(plan: dict | None, args: dict, validations: list[dict]) -> dict:
    if plan is None:
        raise TaskError("本轮尚未登记分析计划。", 422)
    if (set(args) != {"answerable", "reason", "limitations"} or type(args["answerable"]) is not bool
            or not _text(args["reason"]) or not isinstance(args["limitations"], list)
            or len(args["limitations"]) > 10 or not all(_text(item) for item in args["limitations"])):
        raise TaskError("目标核验参数无效。", 422)
    submitted = any(step["state"] == "submitted" for step in plan["steps"])
    completed = all(step["state"] == "completed" for step in plan["steps"])
    sufficient = all(item.get("status") != "insufficient" for item in validations)
    checks = [{"name": "计划步骤已取得结果", "passed": completed},
              {"name": "工具未报告数据不足", "passed": sufficient}]
    warnings = list(dict.fromkeys([warning for item in validations for warning in item.get("warnings", [])]
                                 + args["limitations"]))
    answerable = args["answerable"] and completed and sufficient
    state = ("awaiting_task" if submitted else "insufficient" if not answerable
             else "limited" if warnings else "passed")
    result = {"status": state, "answerable": answerable, "checks": checks,
              "reason": args["reason"], "warnings": warnings,
              "note": "步骤和数据检查由服务端执行；能否回答目标由模型结合证据判断，不代表结论已获独立验证。"}
    plan["validation"] = result
    plan["state"] = state
    return result
