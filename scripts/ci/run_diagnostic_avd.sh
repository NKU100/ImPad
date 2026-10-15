#!/usr/bin/env bash
set -euo pipefail
if [[ "$STOCK_QQ_DIAGNOSTIC" != true || "${QQ_DIAGNOSTIC_LAYER:-stock}" != stock ]]; then bash scripts/ci/verify_avd_root.sh; fi
mkdir -p work/runtime-smoke/ui-classes
javac --release 17 -cp "$ANDROID_HOME/platforms/android-37.2/android.jar" -d work/runtime-smoke/ui-classes scripts/ci/UiHierarchy.java
"$ANDROID_HOME/build-tools/37.0.0/d8" --min-api 37 --lib "$ANDROID_HOME/platforms/android-37.2/android.jar" --output work/runtime-smoke/ui-hierarchy.jar work/runtime-smoke/ui-classes/UiHierarchy.class
if [ "$STOCK_QQ_DIAGNOSTIC" = true ]; then
  test "$APP_ID" = qq
  python3 -m scripts.ci.qq_sigbus_diagnostic
else
  python3 scripts/ci/runtime_smoke.py --app-id "$APP_ID"
  python3 scripts/ci/runtime_tooling_probe.py
fi
