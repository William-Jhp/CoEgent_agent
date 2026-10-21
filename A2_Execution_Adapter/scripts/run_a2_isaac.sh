#!/usr/bin/env bash
# Foreground E4B -> A2 executor. Deployment default: cuVSLAM360, read-only.
set -euo pipefail
a2_adapter_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
a2_workspace_root="$(cd -- "$a2_adapter_root/.." && pwd)"
cd "$a2_workspace_root"
export PYTHONPATH="$a2_adapter_root:$a2_workspace_root/CoEgent_Agent:$a2_workspace_root/Executor_Adapters${PYTHONPATH:+:$PYTHONPATH}"
exec "$a2_workspace_root/.venv/bin/python" -m coegent run \
  --tool-profile a2-isaac --executor a2-isaac \
  --map-scene "$a2_adapter_root/maps/cuvslam360_9230e3f6" \
  --executor-options "$a2_adapter_root/config/cuvslam360_read_only.json" \
  --planner auto --generation-planner inprocess --max-tokens 1024 --max-repairs 1 \
  --no-auto-save-plans --max-replans 0 --mission-timeout 300 \
  --out "$a2_adapter_root/results/run.json" "$@"
