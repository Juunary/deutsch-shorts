# Run the FastAPI server (foreground). Used directly and by the DeutschShorts-Server scheduled task.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\run-server.ps1 [-Lan] [-Port 8000]
#   -Lan  binds 0.0.0.0 so phones on the same Wi-Fi can use http://<pc-ip>:8000 (no PWA/service worker without HTTPS)
param(
    [switch]$Lan,
    [int]$Port = 8000
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null

$bindHost = '127.0.0.1'
if ($Lan) { $bindHost = '0.0.0.0' }

$py = Join-Path $root '.venv\Scripts\python.exe'
$log = Join-Path $root 'logs\server.log'
Write-Host "Starting uvicorn on ${bindHost}:$Port (log: $log)"
# cmd.exe handles the stream redirection reliably under PowerShell 5.1
cmd /c "`"$py`" -m uvicorn app.main:app --host $bindHost --port $Port --log-level info >> `"$log`" 2>&1"
