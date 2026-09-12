#!/usr/bin/env bash
# Smoke-test an exported GGUF on the server with llama.cpp's server (OpenAI-compatible API on :8081).
# Usage: bash ml/serve/llama-server.sh ml/runs/student_v1/student-q4_k_m.gguf [port]
set -euo pipefail
GGUF="${1:?path to .gguf}"
PORT="${2:-8081}"
LLAMA_DIR="${LLAMA_CPP_DIR:-$HOME/llama.cpp}"
BIN="$LLAMA_DIR/build/bin/llama-server"
[ -x "$BIN" ] || BIN="$(command -v llama-server)"
exec "$BIN" -m "$GGUF" --host 127.0.0.1 --port "$PORT" -ngl 99 -c 4096 --jinja --alias student-v1
