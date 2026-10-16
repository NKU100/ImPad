#!/usr/bin/env bash
set -euo pipefail
python3 -m scripts.ci.local_qq_runtime --version 30 "$@"
