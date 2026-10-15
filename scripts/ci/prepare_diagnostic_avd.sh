#!/usr/bin/env bash
set -euo pipefail
if [[ "${STOCK_QQ_DIAGNOSTIC:-false}" != true ]]; then
  bash scripts/ci/prepare_avd_ramdisk.sh
fi
