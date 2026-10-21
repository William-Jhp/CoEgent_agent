#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
export ROS_DOMAIN_ID=72 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI=file:///rig/teamgrit-sim/config/cyclonedds.xml
export PYTHONPATH="/adapter:/coegent:/executor_adapters:/rig/planner${PYTHONPATH:+:$PYTHONPATH}"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export ROS_HOME=/runtime/ros ROS_LOG_DIR=/runtime/ros/log
mkdir -p "$ROS_LOG_DIR"
cd /runtime
exec "$@"
