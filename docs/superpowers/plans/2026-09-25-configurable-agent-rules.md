# Configurable Agent Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Let the Agent run the registered default Hadoop rules or a validated, versioned adjustment to cleaning and table score weights.

**Architecture:** Two strict model tools select default or configured execution. A small rule builder validates and snapshots parameters; the existing Hadoop driver distributes that snapshot to both score jobs and the clean job. Reports and UI disclose the exact parameters and version.

**Tech Stack:** Python standard library, OpenAI Responses API function tools, Hadoop Streaming, vanilla JavaScript.

**Spec:** `docs/superpowers/specs/2026-09-25-configurable-agent-rules-design.md`

## Global Constraints

- Registered raw data remain read-only and hash-checked.
- Default `quality-clean-v1.0.0` results remain compatible with existing reports.
- A task's pre-clean and post-clean scores use the same frozen rule snapshot.
- No model-selected parameter can run before server validation; failed tasks display no substitute scores.

## Review Focus

- A user asks for a value outside 1–5: reject without task creation.
- A model supplies a valid schema but ignores an unsupported user condition: reject rather than silently run default.
- A custom rule snapshot changes after submission: the driver refuses a mismatched hash.
- Strict title-year filtering removes all variants for a movie: associated ratings are counted as policy-related isolation.
- A weight is zero or an entire table is empty: compute only from available tables with positive weights; never divide by zero.

---

### Task 1: Validated rule profiles and model selection

**Files:** Create `service/rules.py`; modify `service/agent.py`, `service/tasks.py`; test `tests/test_llm_agent.py`, `tests/test_rules_config.py`, `tests/test_service.py`.

**Interfaces:** `build_rules(options: dict | None) -> dict`, `validate_rules(rules: dict) -> dict`; `DataGovernanceAgent.plan(prompt) -> decision` with `options` for configured runs.

- [x] Write tests for default byte-equivalent config, custom version stability, invalid bounds, unknown keys, two tool choices, and unsupported scope.
- [x] Run the focused tests and confirm each new behavior fails for the expected reason.
- [x] Implement strict options validation, deterministic config version, model tools, and task snapshot writing.
- [x] Run focused and full test suites; confirm sidecars and lock cleanup.

### Task 2: Hadoop consumes task snapshot

**Files:** Modify `scripts/run_iteration1.py`, `hadoop_jobs/quality.py`, `hadoop_jobs/clean_reducer.py`, `scripts/run_from_wsl.sh`; test `tests/test_hadoop_rules.py`, `tests/test_rules_config.py`.

**Interfaces:** CLI `--rules-file` and `--rules-sha256`; Hadoop cache alias `quality_rules_runtime.json` loaded by each reducer.

- [x] Write tests for minimum retained rating, strict title-year handling, default behavior, and weighted scoring.
- [x] Run focused tests to observe the missing behavior.
- [x] Implement rule snapshot verification, reducer distribution, policy audit reasons, and weighted four-dimension aggregation.
- [x] Run focused and full suites, then inspect report version/hash reconciliation.

### Task 3: User-visible evidence and manual verification

**Files:** Modify `service/static/app.js`, `service/static/index.html`, `docs/迭代一_手动测试.md`, `docs/迭代一_清洗与评分规则_v1.md`; test relevant HTTP service cases.

- [x] Add an HTTP test that a custom decision creates a task with exact configuration metadata.
- [x] Confirm it fails, then show rule parameters and scoring weights from each report on the page.
- [x] Document a default prompt, three custom prompts, rejected requests, and expected report fields.
- [x] Run Python tests, JavaScript syntax check, browser smoke test, and one WSL Hadoop custom run where available.

