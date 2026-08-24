# Stop a running dashboard, wherever it was started from (task, terminal).
# Matches the streamlit process serving sentinel rather than killing every
# python on the machine.
$procs = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match "streamlit" -and $_.CommandLine -match "sentinel" }
if (-not $procs) {
    Write-Host "no sentinel dashboard is running"
    exit 0
}
foreach ($p in $procs) {
    Write-Host "stopping pid $($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
