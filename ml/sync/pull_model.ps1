# Pull an exported GGUF from the lab server into models\student.gguf (served by scripts\llama-server.ps1 or Ollama).
# Usage: powershell -ExecutionPolicy Bypass -File ml\sync\pull_model.ps1 [-Run student_v1] [-Quant q4_k_m] [-Remote Mustree]
param([string]$Run = 'student_v1', [string]$Quant = 'q4_k_m', [string]$Remote = 'Mustree', [string]$Dest = '~/deutsch-shorts')
$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
New-Item -ItemType Directory -Force -Path (Join-Path $root 'models') | Out-Null
$src = "$Dest/ml/runs/$Run/student-$Quant.gguf"
$tmp = Join-Path $root "models\student-$Run-$Quant.gguf"
scp "${Remote}:$src" $tmp
Copy-Item $tmp (Join-Path $root 'models\student.gguf') -Force
Write-Host "models\student.gguf <- $src"
Write-Host "Next: set LLM_BACKEND=local in .env and start scripts\llama-server.ps1 (or -Ollama)."
