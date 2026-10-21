#!/usr/bin/env bash
# Plan only: no ROS imports, simulator commands or robot API execution.
set -euo pipefail
a2_task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$a2_task_root"
export PYTHONPATH="$a2_task_root/CoEgent_Agent:$a2_task_root/Executor_Adapters${PYTHONPATH:+:$PYTHONPATH}"
exec "$a2_task_root/.venv/bin/python" -m coegent plan \
  --tool-profile a2-isaac --planner auto --generation-planner inprocess \
  --map-scene "$a2_task_root/A2_Execution_Adapter/maps/cuvslam360_9230e3f6" \
  --max-tokens 1024 --max-repairs 1 \
  --out "$a2_task_root/CoEgent_Agent/results/a2_isaac_plan.json" "$@"
