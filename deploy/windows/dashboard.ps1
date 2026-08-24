# Start the dashboard. Blocks while serving - Task Scheduler runs it hidden
# at logon so http://localhost:8501 is simply always there.
# Continue, NOT Stop: with Stop, Windows PowerShell 5.1 turns any stderr
# line from a native command under redirection into a terminating error --
# and uv/sentinel write their normal INFO logging to stderr, so the run
# would die on its first healthy log line. Exit codes are checked instead.
$ErrorActionPreference = "Continue"
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location $repo

$uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
if (-not (Test-Path $uv)) { $uv = "uv" }

$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
& $uv run sentinel dashboard *>> (Join-Path $logDir "dashboard.log")
