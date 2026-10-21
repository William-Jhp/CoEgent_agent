#!/usr/bin/env bash
# Existing browser-based Dangjin workbench and its keyboard controls. No ROS bridge.
set -euo pipefail
a2_dangjin_root="${DANGJIN_SIM_ROOT:-/home/gritandgrind/dev/kts/dangjin-a2-sim}"
if [[ ! -f "$a2_dangjin_root/scripts/run_workbench.sh" ]]; then
  echo "Dangjin workbench launcher missing: $a2_dangjin_root/scripts/run_workbench.sh" >&2
  exit 1
fi
export A2SIM_WORKBENCH_PORT="${A2SIM_WORKBENCH_PORT:-8876}"
export A2SIM_WORLD_MODE="${A2SIM_WORLD_MODE:-assets}"
printf 'Dangjin workbench: http://127.0.0.1:%s\n' "$A2SIM_WORKBENCH_PORT"
printf 'Click the 3D view. W/S: forward/reverse, A/D: turn, Q/E: sidestep, Space: stop.\n'
printf 'Foreground server; Ctrl+C exits. This workbench does not start cuVSLAM or Isaac Sim.\n'
exec bash "$a2_dangjin_root/scripts/run_workbench.sh"
