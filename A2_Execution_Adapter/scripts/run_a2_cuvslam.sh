#!/usr/bin/env bash
# Foreground E4B planning/execution with one explicitly selected cuVSLAM scene.
set -euo pipefail
a2_adapter_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$a2_adapter_root/scripts/run_a2_isaac.sh" \
  --map-scene "$a2_adapter_root/maps/cuvslam360_9230e3f6" \
  --executor-options "$a2_adapter_root/config/cuvslam360_read_only.json" \
  --out "$a2_adapter_root/results/cuvslam_run.json" "$@"
