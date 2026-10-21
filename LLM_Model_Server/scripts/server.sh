#!/usr/bin/env bash
set -euo pipefail
SERVER_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$SERVER_ROOT${PYTHONPATH:+:$PYTHONPATH}"
SERVER_PYTHON=${SERVER_PYTHON:-"$SERVER_ROOT/.venv/bin/python"}
if [[ ! -x "$SERVER_PYTHON" ]]; then
    SERVER_PYTHON=python3.12
fi
exec "$SERVER_PYTHON" -m local_llm_server "$@"
