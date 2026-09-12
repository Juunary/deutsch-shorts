#!/usr/bin/env bash
# Serve the teacher model with vLLM on the lab server (OpenAI-compatible API on :8000).
# Usage: bash ml/serve/vllm-teacher.sh [model]   (default from ml/configs/teacher.yaml)
set -euo pipefail
MODEL="${1:-Qwen/Qwen3-32B-AWQ}"
EXTRA=()
case "$MODEL" in
  *AWQ*|*awq*) EXTRA+=(--quantization awq_marlin) ;;
  *GPTQ*|*gptq*) EXTRA+=(--quantization gptq_marlin) ;;
esac
exec vllm serve "$MODEL" --tensor-parallel-size 2 --max-model-len 8192 --gpu-memory-utilization 0.92 \
  --port 8000 --served-model-name "$MODEL" "${EXTRA[@]}"
