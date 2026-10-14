#!/usr/bin/env bash
set -euo pipefail
[[ $# == 0 ]] || { echo 'The managed smoke does not accept arguments.' >&2; exit 2; }
exec python3 "$(dirname "$0")/adb_relay_client.py" verify-runtime
