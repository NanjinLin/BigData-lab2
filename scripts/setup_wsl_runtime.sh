#!/usr/bin/env bash
# Install a private Java/Hadoop runtime for a fresh Ubuntu WSL user.
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="$HOME/.local"
DOWNLOADS="$BASE/share/ml1m-downloads"
STATE="$BASE/share/ml1m-hadoop"
HADOOP_HOME="$BASE/opt/hadoop-3.5.0"
JAVA_HOME="$BASE/opt/temurin-17"
mkdir -p "$DOWNLOADS" "$BASE/opt" "$STATE/conf" "$STATE/logs" "$STATE/pids" "$STATE/tmp"

if [[ ! -x "$JAVA_HOME/bin/java" ]]; then
  echo "Fetching Eclipse Temurin 17 metadata..."
  mapfile -t asset < <(curl -fsSL --retry 3 \
    'https://api.adoptium.net/v3/assets/latest/17/hotspot?architecture=x64&image_type=jdk&os=linux' |
    python3 -c 'import json,sys; a=json.load(sys.stdin)[0]["binary"]["package"]; print(a["link"]); print(a["checksum"])')
  curl -fL --retry 3 -o "$DOWNLOADS/temurin-17.tar.gz" "${asset[0]}"
  echo "${asset[1]}  $DOWNLOADS/temurin-17.tar.gz" | sha256sum -c -
  mkdir -p "$JAVA_HOME"
  tar -xzf "$DOWNLOADS/temurin-17.tar.gz" -C "$JAVA_HOME" --strip-components=1
fi

if [[ ! -x "$HADOOP_HOME/bin/hadoop" ]]; then
  echo "Fetching Apache Hadoop 3.5.0..."
  curl -fL --retry 3 -o "$DOWNLOADS/hadoop-3.5.0.tar.gz" \
    'https://downloads.apache.org/hadoop/common/hadoop-3.5.0/hadoop-3.5.0.tar.gz'
  echo '04ab94496cc00c8b7a28d03f6308eff8d2a4e7f37a9da5e8e086e4d6fc990e7a94d661908f6a6136039536efb362614b8aecdef185b5fb8ed588f0b152c7aa16  '"$DOWNLOADS/hadoop-3.5.0.tar.gz" | sha512sum -c -
  tar -xzf "$DOWNLOADS/hadoop-3.5.0.tar.gz" -C "$BASE/opt"
fi

cp "$PROJECT"/hadoop-conf/* "$STATE/conf/"
sed -i \
  -e "s#/home/linxinan#$HOME#g" \
  -e "s#/opt/hadoop-3.5.0#$HADOOP_HOME#g" \
  -e "s#/usr/lib/jvm/java-17-openjdk-amd64#$JAVA_HOME#g" \
  -e 's/\r$//' \
  "$STATE/conf/"*.xml "$STATE/conf/hadoop-env.sh"

export JAVA_HOME HADOOP_HOME HADOOP_CONF_DIR="$STATE/conf"
if [[ ! -f "$STATE/name/current/VERSION" ]]; then
  if [[ -d "$STATE/name" && -n "$(ls -A "$STATE/name")" ]]; then
    echo "NameNode data exists without a VERSION marker; refusing to format it." >&2
    exit 1
  fi
  echo "Initializing the new, empty NameNode directory at $STATE/name..."
  "$HADOOP_HOME/bin/hdfs" namenode -format -nonInteractive
fi

echo "Runtime ready: JAVA_HOME=$JAVA_HOME"
echo "Runtime ready: HADOOP_HOME=$HADOOP_HOME"
echo "Runtime ready: HADOOP_CONF_DIR=$HADOOP_CONF_DIR"
