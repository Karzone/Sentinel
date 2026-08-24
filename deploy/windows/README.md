# Sentinel on Windows — schedule + always-on dashboard

One-time setup, from the repo root in PowerShell:

```powershell
.\deploy\windows\register-tasks.ps1              # 07:00 morning run + dashboard at logon
.\deploy\windows\register-tasks.ps1 -Time 06:30  # different hour
Start-ScheduledTask -TaskName 'Sentinel Dashboard'   # start it now, no re-login needed
```

After that, nothing is manual:

| Want | Do |
|---|---|
| See the dashboard | open http://localhost:8501 — it starts itself at logon |
| Today's brief | it ran at 07:00; Reports page (or `logs\morning-*.log` if missing) |
| Run the morning now | `.\deploy\windows\morning.ps1` (or the dashboard's Run buttons) |
| Stop the dashboard | `.\deploy\windows\stop-dashboard.ps1` |
| Start it again | `Start-ScheduledTask -TaskName 'Sentinel Dashboard'` |
| Stop all automation | `.\deploy\windows\unregister-tasks.ps1` |
| Phone access | `uv run sentinel phone` (deliberately manual — it publishes a URL) |

Notes:

- The machine must be awake for the 07:00 run; a laptop asleep at 07:00 runs
  it on wake instead (`StartWhenAvailable`). To wake the machine for it, tick
  "Wake the computer to run this task" in Task Scheduler → Sentinel Morning →
  Conditions — a power setting the script deliberately does not change.
- Everything logs to `logs\` next to the repo: `morning-YYYY-MM-DD.log`,
  `dashboard.log`. A missing brief always has its reason there.
- The tasks run as your user with the repo as working directory, so `.env`
  and `sentinel.toml` are read exactly as in a terminal.
