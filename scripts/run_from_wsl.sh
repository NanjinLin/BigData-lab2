#!/usr/bin/env bash
set -euo pipefail

TASK_ID="$1"
AUTO_START="${2:-1}"
[[ -n "${3:-}" ]] && export ML1M_HADOOP_HOME="$3"
[[ -n "${4:-}" ]] && export ML1M_HADOOP_CONF_DIR="$4"
[[ -n "${5:-}" ]] && export ML1M_HDFS_ROOT="$5"
[[ -n "${6:-}" ]] && export ML1M_HADOOP_USER="$6"
RULE_SHA256="${7:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$AUTO_START" == 1 ]]; then
  bash "$SCRIPT_DIR/ensure_hadoop.sh"
fi
if [[ -n "$RULE_SHA256" ]]; then
  python3 "$SCRIPT_DIR/run_iteration1.py" --task-id "$TASK_ID" \
    --rules-file "$SCRIPT_DIR/../runs/$TASK_ID.rules.json" --rules-sha256 "$RULE_SHA256"
else
  python3 "$SCRIPT_DIR/run_iteration1.py" --task-id "$TASK_ID"
fi
