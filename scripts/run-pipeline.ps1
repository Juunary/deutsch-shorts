# Run the ingestion/enrichment pipeline once. Used by the DeutschShorts-Pipeline scheduled task (daily 04:30).
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\run-pipeline.ps1 [-Stage all] [-ExtraArgs "--limit 50"]
param(
    [string]$Stage = 'all',
    [string]$ExtraArgs = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null

$py = Join-Path $root '.venv\Scripts\python.exe'
$log = Join-Path $root ('logs\pipeline-task-' + (Get-Date -Format 'yyyyMMdd') + '.log')
Write-Host "Running pipeline stage '$Stage' $ExtraArgs (log: $log)"
cmd /c "`"$py`" -m pipeline $Stage $ExtraArgs >> `"$log`" 2>&1"
exit $LASTEXITCODE
