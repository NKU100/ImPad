#!/usr/bin/env bash
set -euo pipefail
if [[ "${STOCK_QQ_DIAGNOSTIC:-false}" != true || "${QQ_DIAGNOSTIC_LAYER:-stock}" != stock ]]; then
  case "${QQ_DIAGNOSTIC_IMAGE:-api37-ps16k}" in
    api37-ps16k) bash scripts/ci/prepare_avd_ramdisk.sh ;;
    api30-google)
      stock_ramdisk="${ANDROID_HOME:?}/system-images/android-30/google_apis/x86_64/ramdisk.img"
      python3 scripts/environment/build_ramdisk.py --recipe environment/avd-api30-google.json \
        --stock-ramdisk "$stock_ramdisk" --magisk-apk work/runtime-smoke/magisk.apk \
        --output work/ramdisk-build/api30-google
      cp work/ramdisk-build/api30-google/manifest.json work/runtime-smoke/evidence/ramdisk-reproduction.json
      cp work/ramdisk-build/api30-google/ramdisk37-patched.img "$stock_ramdisk"
      ;;
    *) echo 'Selected framework image requires a separately verified ramdisk' >&2; exit 1 ;;
  esac
fi
