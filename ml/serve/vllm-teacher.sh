#!/usr/bin/env bash
# Serve the teacher model with vLLM on the lab server (OpenAI-compatible API on :8000).
# Usage: bash ml/serve/vllm-teacher.sh [model] [max_num_seqs]
#   default model Qwen/Qwen3-14B-AWQ (Qwen3-32B-AWQ does not fit 2x12GB with a usable KV cache, see configs/teacher.yaml)
# Run inside tmux so it survives the SSH session:  tmux new -d -s teacher 'bash ml/serve/vllm-teacher.sh'
set -euo pipefail
MODEL="${1:-Qwen/Qwen3-14B-AWQ}"
MAX_NUM_SEQS="${2:-16}"          # 256 (default) -> sampler warm-up OOM on 12 GB GPUs
PORT="${PORT:-8000}"
EXTRA=()
case "$MODEL" in
  *AWQ*|*awq*) EXTRA+=(--quantization awq_marlin) ;;
  *GPTQ*|*gptq*) EXTRA+=(--quantization gptq_marlin) ;;
esac
exec vllm serve "$MODEL" --tensor-parallel-size 2 --max-model-len 8192 --gpu-memory-utilization 0.92 \
  --max-num-seqs "$MAX_NUM_SEQS" --port "$PORT" --served-model-name "$MODEL" "${EXTRA[@]}"
