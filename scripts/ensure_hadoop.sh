#!/usr/bin/env bash
set -euo pipefail

DEFAULT_HADOOP_HOME=/opt/hadoop-3.5.0
[[ -d "$DEFAULT_HADOOP_HOME" ]] || DEFAULT_HADOOP_HOME="$HOME/.local/opt/hadoop-3.5.0"
HADOOP_HOME="${ML1M_HADOOP_HOME:-$DEFAULT_HADOOP_HOME}"
DEFAULT_CONF_DIR=/home/linxinan/ml1m-hadoop-conf
[[ -d "$DEFAULT_CONF_DIR" ]] || DEFAULT_CONF_DIR="$HOME/.local/share/ml1m-hadoop/conf"
export HADOOP_CONF_DIR="${ML1M_HADOOP_CONF_DIR:-$DEFAULT_CONF_DIR}"
HADOOP_USER="${ML1M_HADOOP_USER:-$(id -un)}"

if [[ ! -x "$HADOOP_HOME/bin/hdfs" || ! -x "$HADOOP_HOME/bin/yarn" ]]; then
  echo "Hadoop is missing at $HADOOP_HOME; install Hadoop 3.5.0 or set ML1M_HADOOP_HOME." >&2
  exit 1
fi
if [[ ! -d "$HADOOP_CONF_DIR" ]]; then
  echo "Hadoop configuration is missing at $HADOOP_CONF_DIR; set ML1M_HADOOP_CONF_DIR." >&2
  exit 1
fi
source "$HADOOP_CONF_DIR/hadoop-env.sh"
mkdir -p "$HOME/.local/share/ml1m-hadoop/logs"

if "$HADOOP_HOME/bin/hdfs" dfs -ls / >/dev/null 2>&1 && \
   "$HADOOP_HOME/bin/yarn" node -list 2>&1 | grep -q RUNNING; then
  echo "HDFS and YARN are already ready."
  exit 0
fi

for spec in \
  "ml1m-namenode:hdfs:namenode" \
  "ml1m-datanode:hdfs:datanode" \
  "ml1m-resourcemanager:yarn:resourcemanager" \
  "ml1m-nodemanager:yarn:nodemanager"; do
  IFS=: read -r unit executable role <<< "$spec"
  if ! systemctl is-active --quiet "$unit"; then
    if sudo -n true 2>/dev/null; then
      sudo -n systemd-run --unit="$unit" --property="User=$HADOOP_USER" \
        --setenv="HADOOP_CONF_DIR=$HADOOP_CONF_DIR" "$HADOOP_HOME/bin/$executable" "$role"
    else
      nohup "$HADOOP_HOME/bin/$executable" "$role" \
        >> "$HOME/.local/share/ml1m-hadoop/logs/$unit.log" 2>&1 < /dev/null &
    fi
  fi
done

for attempt in $(seq 1 30); do
  if "$HADOOP_HOME/bin/hdfs" dfs -ls / >/dev/null 2>&1 && \
     "$HADOOP_HOME/bin/yarn" node -list 2>&1 | grep -q RUNNING; then
    echo "HDFS and YARN are ready."
    exit 0
  fi
  sleep 2
done
echo "HDFS or YARN did not become ready within 60 seconds." >&2
exit 1
