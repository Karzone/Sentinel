# One-time setup: two Windows Scheduled Tasks.
#
#   Sentinel Morning    - daily at -Time (default 07:00): ingest + brief, so
#                         the day's report is waiting on the Reports page.
#   Sentinel Dashboard  - at every logon: starts the dashboard hidden, so
#                         http://localhost:8501 is simply always available.
#
# Run from a normal PowerShell (no admin needed - tasks are registered for the
# current user):   .\deploy\windows\register-tasks.ps1
# Undo with:       .\deploy\windows\unregister-tasks.ps1
param(
    [string]$Time = "07:00",
    [string]$Universe = "ai"
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot

$morning = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$here\morning.ps1`" -Universe $Universe" `
    -WorkingDirectory $here
$morningTrigger = New-ScheduledTaskTrigger -Daily -At $Time
# StartWhenAvailable: a laptop asleep at 07:00 runs the task when it wakes,
# instead of silently skipping the day.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName "Sentinel Morning" -Action $morning `
    -Trigger $morningTrigger -Settings $settings -Force | Out-Null
Write-Host "registered: Sentinel Morning  (daily $Time - ingest + brief, universe '$Universe')"

$dash = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$here\dashboard.ps1`"" `
    -WorkingDirectory $here
$dashTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$dashSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)   # the dashboard is meant to stay up
Register-ScheduledTask -TaskName "Sentinel Dashboard" -Action $dash `
    -Trigger $dashTrigger -Settings $dashSettings -Force | Out-Null
Write-Host "registered: Sentinel Dashboard (starts hidden at every logon)"

Write-Host ""
Write-Host "Start the dashboard NOW without logging out:"
Write-Host "  Start-ScheduledTask -TaskName 'Sentinel Dashboard'"
Write-Host "Then open http://localhost:8501"
