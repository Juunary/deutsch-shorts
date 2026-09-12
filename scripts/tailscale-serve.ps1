# Expose the local server over the tailnet with HTTPS (needed for the service worker / PWA on Android,
# and for the phone to reach the PC from anywhere). Persists across reboots (Tailscale runs as a service).
# Prerequisites: Tailscale installed on PC and phone; in the tailnet admin console enable MagicDNS and HTTPS certificates.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\tailscale-serve.ps1 [-Port 8000] [-Off]
param([int]$Port = 8000, [switch]$Off)
$ErrorActionPreference = 'Stop'

$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts) {
    $candidate = 'C:\Program Files\Tailscale\tailscale.exe'
    if (Test-Path $candidate) { $ts = $candidate } else { throw 'tailscale.exe not found. Install Tailscale first: https://tailscale.com/download/windows' }
} else { $ts = $ts.Source }

if ($Off) {
    & $ts serve --https=443 off
    Write-Host 'Tailscale serve disabled.'
    exit 0
}

& $ts serve --bg --https=443 "http://127.0.0.1:$Port"
& $ts serve status
$status = & $ts status --json | ConvertFrom-Json
$dns = $status.Self.DNSName.TrimEnd('.')
Write-Host ""
Write-Host "Phone URL: https://$dns"
Write-Host "First visit on the phone: https://$dns/#token=<APP_TOKEN from .env>"
