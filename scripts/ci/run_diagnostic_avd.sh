#!/usr/bin/env bash
set -euo pipefail
if [[ "$STOCK_QQ_DIAGNOSTIC" != true || "${QQ_DIAGNOSTIC_LAYER:-stock}" != stock ]]; then bash scripts/ci/verify_avd_root.sh; fi
mkdir -p work/runtime-smoke/ui-classes
sdkmanager 'platforms;android-37.2'
javac --release 17 -cp "$ANDROID_HOME/platforms/android-37.2/android.jar" -d work/runtime-smoke/ui-classes scripts/ci/UiHierarchy.java
"$ANDROID_HOME/build-tools/37.0.0/d8" --min-api 28 --lib "$ANDROID_HOME/platforms/android-37.2/android.jar" --output work/runtime-smoke/ui-hierarchy.jar work/runtime-smoke/ui-classes/UiHierarchy.class
adb shell getconf PAGESIZE > work/runtime-smoke/evidence/page-size.txt
adb shell getprop > work/runtime-smoke/evidence/image-properties.txt
adb shell cat /proc/meminfo > work/runtime-smoke/evidence/android-memory-initial.txt
(
  while true; do
    date -u '+%FT%TZ'
    ps -eo pid,rss,vsz,comm | awk '$4 ~ /qemu-system|emulator/ {print}'
    free -k
    sleep 10
  done
) > work/runtime-smoke/evidence/host-memory.txt &
memory_monitor=$!
trap 'kill "$memory_monitor" 2>/dev/null || true' EXIT
if [ "$STOCK_QQ_DIAGNOSTIC" = true ]; then
  test "$APP_ID" = qq
  python3 -m scripts.ci.qq_sigbus_diagnostic
else
  python3 scripts/ci/runtime_smoke.py --app-id "$APP_ID"
  python3 scripts/ci/runtime_tooling_probe.py
fi
