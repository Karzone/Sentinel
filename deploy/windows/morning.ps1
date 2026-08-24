# The morning run: fetch fresh data, then score the universe and write the brief.
#
# Runs from Task Scheduler (register-tasks.ps1) or by hand. Everything is
# logged to logs\morning-YYYY-MM-DD.log because a scheduled task that fails
# silently at 07:00 is worse than no schedule at all - the brief simply would
# not appear and nothing would say why.
param(
    [string]$Universe = "ai",
    [int]$History = 800
)

# Continue, NOT Stop: with Stop, Windows PowerShell 5.1 turns any stderr
# line from a native command under redirection into a terminating error --
# and uv/sentinel write their normal INFO logging to stderr, so the run
# would die on its first healthy log line. Exit codes are checked instead.
$ErrorActionPreference = "Continue"
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location $repo   # .env is only read from the working directory

$uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
if (-not (Test-Path $uv)) { $uv = "uv" }  # fall back to PATH

$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("morning-{0:yyyy-MM-dd}.log" -f (Get-Date))

"=== morning run started $(Get-Date -Format o) ===" | Add-Content $log
& $uv run sentinel ingest --universe $Universe --history $History *>> $log
$ingestExit = $LASTEXITCODE
"ingest exit code: $ingestExit" | Add-Content $log

# Exit code 2 is "data blocked" - the ingest itself ran; the quality layer
# found critical issues. The brief still runs: it reports those issues and
# scores what is scoreable, which beats an empty morning.
& $uv run sentinel brief --universe $Universe *>> $log
"brief exit code: $LASTEXITCODE" | Add-Content $log
"=== morning run finished $(Get-Date -Format o) ===" | Add-Content $log
