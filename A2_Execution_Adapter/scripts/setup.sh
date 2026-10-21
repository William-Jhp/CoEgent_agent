#!/usr/bin/env bash
set -euo pipefail
a2_adapter_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
a2_workspace_root="$(cd -- "$a2_adapter_root/.." && pwd)"
"$a2_workspace_root/.venv/bin/python" -m pip install --no-deps --no-build-isolation -e "$a2_adapter_root"
docker build --pull=false -t coegent-a2-adapter:sim -f "$a2_adapter_root/Dockerfile" "$a2_adapter_root"
