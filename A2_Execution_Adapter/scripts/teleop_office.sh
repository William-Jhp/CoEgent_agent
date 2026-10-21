#!/usr/bin/env bash
# Terminal keyboard for the already-running Isaac Office simulator, ROS domain72.
set -eo pipefail
a2_adapter_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
a2_isaac_root="${A2_ISAAC_ROOT:-/home/gritandgrind/dev/kts/a2-llm-isaac}"
source /opt/ros/jazzy/setup.bash
export ROS_DOMAIN_ID=72 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI="file://$a2_isaac_root/teamgrit-sim/config/cyclonedds.xml"
# Jazzy requires system Python 3.12, not Isaac's bundled Python or the model venv.
export PYTHONPATH="$a2_adapter_root:/opt/ros/jazzy/lib/python3.12/site-packages"
exec /usr/bin/python3 -m a2_execution_adapter.office_keyboard "$@"
