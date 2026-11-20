#!/usr/bin/env bash
# Wrapper so the agent has one fixed command to run (and to auto-approve).
#   DB2Q_JAVA      path to db2q.java          (default: ~/scripts/db2q/db2q.java)
#   DB2Q_JVM_OPTS  extra JVM options, if your machine needs any
set -euo pipefail
script="${DB2Q_JAVA:-$HOME/scripts/db2q/db2q.java}"
if [[ ! -f "$script" ]]; then
  echo "db2q: $script not found (set DB2Q_JAVA)" >&2
  exit 1
fi
opts=()
[[ -n "${DB2Q_JVM_OPTS:-}" ]] && opts=(--java-options="$DB2Q_JVM_OPTS")
exec jbang --quiet "${opts[@]}" "$script" "$@"
