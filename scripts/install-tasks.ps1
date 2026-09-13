# Register the Windows scheduled tasks that keep the app running unattended:
#   DeutschShorts-Server    at logon (or at startup with -Service) -> scripts\run-server.ps1
#   DeutschShorts-LLM       at logon (or at startup with -Service) -> scripts\llama-server.ps1  (only if models\student.gguf exists)
#   DeutschShorts-Pipeline  daily 04:30 -> scripts\run-pipeline.ps1 all --limit 8
#   DeutschShorts-Pull      hourly      -> scripts\run-pipeline.ps1 pull   (lab server -> PC content sync over ssh alias Mustree)
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1 [-Remove] [-Service]
#   default : logon tasks for the current user (no admin rights needed; they run while you are logged on)
#   -Service: from an ADMIN PowerShell -> S4U tasks that run at boot without logging on
param([switch]$Remove, [switch]$Service)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$user = "$env:USERDOMAIN\$env:USERNAME"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($Service -and -not $isAdmin) { throw '-Service needs an elevated (admin) PowerShell.' }
$useService = $Service -and $isAdmin

if ($useService) {
    $startTrigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
} else {
    $startTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
}
$hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 1)
$hourly30 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(30) -RepetitionInterval (New-TimeSpan -Hours 1)
$tasks = @(
    @{ Name = 'DeutschShorts-Server';   Script = 'run-server.ps1';   Args = '';                                 Trigger = $startTrigger },
    @{ Name = 'DeutschShorts-LLM';      Script = 'llama-server.ps1'; Args = '';                                 Trigger = $startTrigger },
    @{ Name = 'DeutschShorts-Pipeline'; Script = 'run-pipeline.ps1'; Args = '-Stage all -ExtraArgs "--limit 8"'; Trigger = (New-ScheduledTaskTrigger -Daily -At 4:30AM) },
    @{ Name = 'DeutschShorts-Pull';     Script = 'run-pipeline.ps1'; Args = '-Stage pull';                      Trigger = $hourly },
    @{ Name = 'DeutschShorts-Transcripts'; Script = 'run-pipeline.ps1'; Args = '-Stage transcripts -ExtraArgs "--limit 6"'; Trigger = $hourly30 }
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
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $t.Trigger -Settings $settings -Principal $principal | Out-Null
    Write-Host "Registered $($t.Name) ($(if ($useService) { 'service/S4U' } else { 'logon/interactive' }))"
}

if (-not $Remove) {
    Write-Host "Done. Start the server now with:  Start-ScheduledTask -TaskName DeutschShorts-Server"
    if (-not $useService) { Write-Host "Tasks run while you are logged on. For boot-time tasks re-run from an admin PowerShell with -Service." }
    Write-Host "Optional firewall rule for LAN access (admin PowerShell):"
    Write-Host "  New-NetFirewallRule -DisplayName 'DeutschShorts 8000' -Direction Inbound -Protocol TCP -LocalPort 8000 -Profile Private -Action Allow"
}
