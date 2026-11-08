<#
.SYNOPSIS
    Registers two scheduled tasks: the server at boot, and the backup three
    times a day. Run once, as Administrator.

.DESCRIPTION
    The server task runs as SYSTEM with an "At startup" trigger, so it comes
    up when the machine powers on -- not when somebody signs in. That is the
    difference that made Docker Desktop unsuitable: it is a desktop app and
    waits for a login. PostgreSQL is already a Windows service and behaves the
    same way, so the whole stack is running before the counter PC reaches its
    lock screen.

    Both tasks are idempotent: re-running replaces them.

.EXAMPLE
    # In an Administrator PowerShell:
    cd E:\Pharmacy_APP
    .\ops\install-autostart.ps1
#>
[CmdletBinding()]
param(
    [string]   $ServerTaskName = "PharmacyPOS Server",
    [string]   $BackupTaskName = "PharmacyPOS Backup",
    [int]      $Port           = 8000,
    # Noon, mid-afternoon, closing: three even checkpoints through the day.
    [string[]] $BackupTimes    = @("12:00", "16:00", "22:00")
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this in an Administrator PowerShell -- a task that starts at boot needs it."
}

$root  = Split-Path -Parent $PSScriptRoot
$serve  = Join-Path $PSScriptRoot "serve.ps1"
$backup = Join-Path $PSScriptRoot "backup.ps1"
foreach ($script in @($serve, $backup)) {
    if (-not (Test-Path $script)) { throw "Missing $script" }
}

$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
# SYSTEM. It starts without a login, and it can read E:\ -- which a per-user
# task cannot be relied on to do before that user has signed in.
$asSystem = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# --- the server -----------------------------------------------------------
$serverAction = New-ScheduledTaskAction -Execute $powershell `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$serve`" -Port $Port" `
    -WorkingDirectory $root

$serverSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $ServerTaskName -Force `
    -Description "Pharmacy POS API and app. Starts at boot, before anyone signs in." `
    -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Action $serverAction -Principal $asSystem -Settings $serverSettings | Out-Null

Write-Host "Registered '$ServerTaskName' (at startup, as SYSTEM)." -ForegroundColor Green

# --- the backup -----------------------------------------------------------
$backupAction = New-ScheduledTaskAction -Execute $powershell `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$backup`"" `
    -WorkingDirectory $root

$backupTriggers = foreach ($time in $BackupTimes) {
    New-ScheduledTaskTrigger -Daily -At $time
}

$backupSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $BackupTaskName -Force `
    -Description "Dumps the pharmacy database to data\backups and pushes a copy to Supabase." `
    -Trigger $backupTriggers -Action $backupAction -Principal $asSystem -Settings $backupSettings | Out-Null

Write-Host "Registered '$BackupTaskName' (daily at $($BackupTimes -join ', '))." -ForegroundColor Green

Write-Host ""
Write-Host "Starting the server now so you do not have to reboot to use it..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $ServerTaskName
Start-Sleep -Seconds 12

try {
    $code = (Invoke-WebRequest "http://127.0.0.1:$Port/api/v1/health" -UseBasicParsing -TimeoutSec 5).StatusCode
    Write-Host "Server is up (HTTP $code). Open http://localhost:$Port" -ForegroundColor Green
} catch {
    Write-Host "Not answering yet. Check data\logs\server-*.log" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "To check after a reboot:  Get-ScheduledTask '$ServerTaskName'" -ForegroundColor Cyan
Write-Host "To stop:                  Stop-ScheduledTask '$ServerTaskName'" -ForegroundColor Cyan
Write-Host "To remove:                Unregister-ScheduledTask '$ServerTaskName' -Confirm:`$false" -ForegroundColor Cyan
