#!/usr/bin/env bash
set -euo pipefail
: "${IMPAD_ADB_RELAY:?Disposable ReDroid relay is required}"
case "${1:-}" in
  app-launch|wechat-launch|shell|exec-out|push|pull|install|uninstall|logcat|reboot|root|unroot|wait-for-device|get-state|version|help) ;;
  *) echo 'Use a device command; the ReDroid and ADB server are fixed by the controller.' >&2; exit 2 ;;
esac
exec python3 "$(dirname "$0")/adb_relay_client.py" "$@"
