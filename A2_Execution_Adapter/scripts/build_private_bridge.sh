#!/usr/bin/env bash
# Build only the CoEgent-owned bridge; never overwrite KTS source/install files.
set -euo pipefail
a2_bridge_package="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
a2_bridge_source="${A2_ISAAC_ROOT:-/home/gritandgrind/dev/kts/a2-llm-isaac}"
mkdir -p "$a2_bridge_package/runtime/private_bridge"
exec docker run --rm --entrypoint /bin/bash --user "$(id -u):$(id -g)" \
  --volume "$a2_bridge_source:/rig:ro" \
  --volume "$a2_bridge_package:/adapter:ro" \
  --volume "$a2_bridge_package/runtime:/runtime:rw" \
  coegent-a2-adapter:sim \
  -c 'python3 /adapter/a2_execution_adapter/private_bridge.py /rig /runtime/private_bridge'
