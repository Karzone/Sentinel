# Remove both scheduled tasks. The repo, data and dashboard are untouched -
# this only stops things happening automatically.
foreach ($name in "Sentinel Morning", "Sentinel Dashboard") {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "removed: $name"
    } else {
        Write-Host "not registered: $name"
    }
}
