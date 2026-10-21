#!/usr/bin/env bash
set -euo pipefail
SERVER_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if ! command -v uv >/dev/null 2>&1; then
    echo 'uv is required for the isolated runtime install.' >&2
    exit 1
fi
if [[ ! -x "$SERVER_ROOT/.venv/bin/python" ]]; then
    uv venv --python "${SERVER_PYTHON:-python3.12}" "$SERVER_ROOT/.venv"
fi
SERVER_REQUIREMENTS="$SERVER_ROOT/requirements-runtime.lock.txt"
if [[ ! -s "$SERVER_REQUIREMENTS" ]]; then
    SERVER_REQUIREMENTS="$SERVER_ROOT/requirements-runtime.txt"
fi
uv pip install --python "$SERVER_ROOT/.venv/bin/python" -r "$SERVER_REQUIREMENTS"
if [[ "$SERVER_REQUIREMENTS" == "$SERVER_ROOT/requirements-runtime.txt" ]]; then
    uv pip freeze --exclude-editable --python "$SERVER_ROOT/.venv/bin/python" > "$SERVER_ROOT/requirements-runtime.lock.txt"
fi
uv pip install --python "$SERVER_ROOT/.venv/bin/python" -e "$SERVER_ROOT"
"$SERVER_ROOT/.venv/bin/python" -c 'import importlib.metadata; print("vLLM", importlib.metadata.version("vllm")); print("transformers", importlib.metadata.version("transformers"))'
