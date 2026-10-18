#!/usr/bin/env bash
set -euo pipefail
: "${IMPAD_JADX_LOCK:?JADX lock must be configured by the trusted controller}"
# A shared lock covers commands launched concurrently by separate model tools.
: "${IMPAD_JADX_HEAP:?JADX heap budget must be configured by the trusted controller}"
exec flock "$IMPAD_JADX_LOCK" env JAVA_OPTS="-Xms256m -Xmx${IMPAD_JADX_HEAP}" JADX_OPTS='' \
  "$(dirname "$0")/jadx.real" -j 2 "$@"
