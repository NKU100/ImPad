#!/usr/bin/env bash
set -euo pipefail
: "${IMPAD_JADX_LOCK:?JADX lock must be configured by the trusted controller}"
# A shared lock covers commands launched concurrently by separate model tools.
heap=4g
if [[ "${APP_ID:-wechat}" == wecom ]]; then heap=3g; fi
exec flock "$IMPAD_JADX_LOCK" env JAVA_OPTS="-Xms256m -Xmx$heap" JADX_OPTS='' \
  "$(dirname "$0")/jadx.real" -j 2 "$@"
