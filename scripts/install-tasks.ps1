# Register the three Windows scheduled tasks that keep the app running unattended:
#   DeutschShorts-Server    at startup  -> scripts\run-server.ps1
#   DeutschShorts-LLM       at startup  -> scripts\llama-server.ps1   (only if a student model exists)
#   DeutschShorts-Pipeline  daily 04:30 -> scripts\run-pipeline.ps1 all
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1 [-Remove]
# Runs as the current user with S4U logon (no password stored, runs whether logged on or not).
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$user = "$env:USERDOMAIN\$env:USERNAME"

$hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 1)
$tasks = @(
    @{ Name = 'DeutschShorts-Server';      Script = 'run-server.ps1';   Args = '';      Trigger = (New-ScheduledTaskTrigger -AtStartup) },
    @{ Name = 'DeutschShorts-LLM';         Script = 'llama-server.ps1'; Args = '';      Trigger = (New-ScheduledTaskTrigger -AtStartup) },
    @{ Name = 'DeutschShorts-Pipeline';    Script = 'run-pipeline.ps1'; Args = '-Stage all -ExtraArgs "--limit 8"'; Trigger = (New-ScheduledTaskTrigger -Daily -At 4:30AM) },
    # The lab server collects transcripts and runs the teacher model; pull its export every hour (scp over ssh alias Mustree)
    @{ Name = 'DeutschShorts-Pull';        Script = 'run-pipeline.ps1'; Args = '-Stage pull'; Trigger = $hourly }
)

foreach ($t in $tasks) {
    $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
        Write-Host "Removed existing task $($t.Name)"
    }
    if ($Remove) { continue }
    if ($t.Name -eq 'DeutschShorts-LLM' -and -not (Test-Path (Join-Path $root 'models\student.gguf'))) {
        Write-Host "Skipping DeutschShorts-LLM (no models\student.gguf yet)."
        continue
    }
    $scriptPath = Join-Path $root ('scripts\' + $t.Script)
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" $($t.Args)" `
        -WorkingDirectory $root
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $t.Trigger -Settings $settings -Principal $principal | Out-Null
    Write-Host "Registered $($t.Name)"
}

if (-not $Remove) {
    Write-Host "Done. Start the server now with:  Start-ScheduledTask -TaskName DeutschShorts-Server"
    Write-Host "Optional firewall rule for LAN access (admin PowerShell):"
    Write-Host "  New-NetFirewallRule -DisplayName 'DeutschShorts 8000' -Direction Inbound -Protocol TCP -LocalPort 8000 -Profile Private -Action Allow"
}
