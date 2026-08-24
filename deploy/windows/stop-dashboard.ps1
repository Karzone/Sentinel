# Stop a running dashboard, wherever it was started from (task, terminal).
# Two discovery paths, because either alone can miss:
#   - The command-line match ("streamlit" + "sentinel") finds it by name, but
#     Win32_Process hides CommandLine across elevation boundaries — a
#     scheduled task's process can be invisible to the user's shell, which is
#     exactly how this script once printed "no sentinel dashboard is running"
#     while one was serving the browser.
#   - The owner of the listening port cannot hide: whatever holds $Port IS
#     the dashboard (or an impostor that equally needs to die before a
#     restart can bind).
# The script only trusts its own success if the port is actually free at the
# end — "I stopped it" is a claim about the port, not about a process list.
param([int]$Port = 8501)
$ErrorActionPreference = "Continue"

$dashboardPids = @()

$procs = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match "streamlit" -and $_.CommandLine -match "sentinel" }
$dashboardPids += @($procs | ForEach-Object { [int]$_.ProcessId })

$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
$dashboardPids += @($listeners | ForEach-Object { [int]$_.OwningProcess })

$dashboardPids = $dashboardPids | Where-Object { $_ -and $_ -ne $PID } | Sort-Object -Unique
if (-not $dashboardPids) {
    Write-Host "no sentinel dashboard is running (no matching process, nothing listening on port $Port)"
    exit 0
}

$stuck = @()
foreach ($id in $dashboardPids) {
    Write-Host "stopping pid $id"
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 200
    if (Get-Process -Id $id -ErrorAction SilentlyContinue) {
        $stuck += $id
    }
}

Start-Sleep -Milliseconds 500
$still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($still -or $stuck) {
    if ($stuck) {
        Write-Host "could not stop pid(s) $($stuck -join ', ') — if the scheduled task runs elevated, run this script from an admin PowerShell"
    }
    if ($still) {
        Write-Host "warning: port $Port is still in use — the dashboard is NOT stopped"
    }
    exit 1
}
Write-Host "dashboard stopped; port $Port is free"
