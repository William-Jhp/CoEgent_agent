#!/usr/bin/env bash
set -euo pipefail
SERVER_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
SERVER_CONTROL_PYTHON=${SERVER_CONTROL_PYTHON:-python3}
if [[ ${1:-} == --source ]]; then
    if [[ $# != 2 ]]; then
        echo 'Usage: setup_llamacpp.sh --source /path/to/llama.cpp' >&2
        exit 2
    fi
    SERVER_SOURCE_DIR=$(cd -- "$2" && pwd)
    SERVER_BUILD_DIR="$SERVER_ROOT/runtimes/llamacpp-build"
    cmake -S "$SERVER_SOURCE_DIR" -B "$SERVER_BUILD_DIR" \
      -DGGML_CUDA="${SERVER_LLAMA_CUDA:-ON}" -DCMAKE_BUILD_TYPE=Release \
      -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON
    cmake --build "$SERVER_BUILD_DIR" --target llama-server --parallel "${SERVER_BUILD_JOBS:-2}"
    "$SERVER_CONTROL_PYTHON" "$SERVER_ROOT/scripts/setup_llamacpp.py" \
      --from-build "$SERVER_BUILD_DIR" --license "$SERVER_SOURCE_DIR/LICENSE"
else
    exec "$SERVER_CONTROL_PYTHON" "$SERVER_ROOT/scripts/setup_llamacpp.py" "$@"
fi
