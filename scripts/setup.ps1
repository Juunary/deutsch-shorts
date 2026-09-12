# One-time setup on the home PC: venv, dependencies, .env with a fresh APP_TOKEN, DB migration.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

if (-not (Test-Path .venv)) {
    Write-Host "Creating venv (.venv) with Python 3.12..."
    py -3.12 -m venv .venv
}
& .venv\Scripts\python -m pip install --upgrade pip
& .venv\Scripts\python -m pip install -r requirements.txt

if (-not (Test-Path .env)) {
    $token = [guid]::NewGuid().ToString('N')
    $content = (Get-Content .env.example -Raw) -replace 'APP_TOKEN=.*', "APP_TOKEN=$token"
    [IO.File]::WriteAllText((Join-Path $root '.env'), $content, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Created .env with a new APP_TOKEN: $token"
    Write-Host "Next: put your YouTube Data API key into .env (YOUTUBE_API_KEY=...)."
} else {
    Write-Host ".env already exists; leaving it untouched."
}

foreach ($d in @('logs', 'models', 'tools', 'data')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $root $d) | Out-Null
}

& .venv\Scripts\python -c "from app.db import open_db; c = open_db(); print('DB ready, schema version', c.execute('PRAGMA user_version').fetchone()[0])"
Write-Host "Setup done. Start the server with scripts\run-server.ps1"
