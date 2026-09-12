# Serve the fine-tuned student model on the home PC (RTX 4070 8GB) with an OpenAI-compatible API
# that pipeline/llm_backends.LocalBackend calls (LOCAL_LLM_URL in .env).
#
# Two options, because Windows Smart App Control (ON on this PC) may block unsigned binaries:
#   1) llama.cpp prebuilt CUDA build unzipped into tools\llama\ (llama-server.exe). Port 8081.
#   2) Ollama (signed installer). Import the GGUF once with a Modelfile, then Ollama serves it on port 11434
#      (set LOCAL_LLM_URL=http://127.0.0.1:11434 and LOCAL_LLM_MODEL=student in .env).
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\llama-server.ps1 [-Model models\student.gguf] [-Port 8081] [-Ollama]
param(
    [string]$Model = 'models\student.gguf',
    [int]$Port = 8081,
    [int]$Ctx = 4096,
    [switch]$Ollama
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null
$modelPath = Join-Path $root $Model
if (-not (Test-Path $modelPath)) { throw "Model not found: $modelPath (pull it with ml\sync\pull_model.ps1)" }

if ($Ollama) {
    $ol = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ol) { throw 'ollama not found. Install from https://ollama.com/download' }
    $modelfile = Join-Path $root 'models\Modelfile'
    $mf = "FROM $modelPath`nPARAMETER temperature 0.2`nPARAMETER num_ctx $Ctx`n"
    [IO.File]::WriteAllText($modelfile, $mf, (New-Object System.Text.UTF8Encoding($false)))
    & ollama create student -f $modelfile
    Write-Host 'Model registered in Ollama as "student". Ollama serves http://127.0.0.1:11434/v1 (set LOCAL_LLM_URL / LOCAL_LLM_MODEL=student in .env).'
    exit 0
}

$exe = Join-Path $root 'tools\llama\llama-server.exe'
if (-not (Test-Path $exe)) {
    throw "tools\llama\llama-server.exe not found. Download the latest llama.cpp Windows CUDA release zip from https://github.com/ggml-org/llama.cpp/releases and unzip into tools\llama\ (or use -Ollama)."
}
$log = Join-Path $root 'logs\llm.log'
Write-Host "Starting llama-server on port $Port with $modelPath (log: $log)"
cmd /c "`"$exe`" -m `"$modelPath`" --port $Port --host 127.0.0.1 -ngl 99 -c $Ctx --jinja --alias student-v1 >> `"$log`" 2>&1"
