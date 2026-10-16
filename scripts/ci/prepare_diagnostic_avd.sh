#!/usr/bin/env bash
set -euo pipefail
if [[ "${STOCK_QQ_DIAGNOSTIC:-false}" != true || "${QQ_DIAGNOSTIC_LAYER:-stock}" != stock ]]; then
  if [[ "${QQ_DIAGNOSTIC_IMAGE:-api37-ps16k}" != api37-ps16k ]]; then
    echo 'API 30 framework run requires a separately verified ramdisk' >&2
    exit 1
  fi
  bash scripts/ci/prepare_avd_ramdisk.sh
fi
