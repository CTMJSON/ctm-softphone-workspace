# One-time setup of the continuity checks. Run in PowerShell on the host (no admin needed):
#   powershell -ExecutionPolicy Bypass -File C:\sites\ctmphone\deploy\install-continuity.ps1 -NtfyTopic 'ctmphone-<something-hard-to-guess>'
# The parameter is optional. Safe to re-run (it replaces the watchdog task and refreshes the launchers).
param(
  [string]$NtfyTopic = ''
)
$ErrorActionPreference = 'Stop'
$root = 'C:\sites\ctmphone'

# 1. Refresh the launcher copies in the repo root (tasks run these, not the deploy/ originals).
Copy-Item -Force (Join-Path $root 'deploy\start-ctmphone.cmd') (Join-Path $root 'start-ctmphone.cmd')
Copy-Item -Force (Join-Path $root 'deploy\start-tunnel.cmd') (Join-Path $root 'start-tunnel.cmd')
Write-Host 'Updated start-ctmphone.cmd and start-tunnel.cmd (used at the next restart).'

# 2. Make the app and tunnel tasks keep retrying if they exit, instead of giving up.
$retry = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
foreach ($name in 'ctmphone App', 'ctmphone Tunnel') {
  try { Set-ScheduledTask -TaskName $name -Settings $retry | Out-Null; Write-Host "Task '$name': restart on failure, no limit." }
  catch { Write-Host "Could not change '$name' ($($_.Exception.Message)); the watchdog still covers it." }
}

# 3. The watchdog: every minute, at start-up too, same account as the other tasks, hidden, no password.
$taskArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$root\deploy\watchdog.ps1`""
if ($NtfyTopic) { $taskArgs += " -NtfyTopic `"$NtfyTopic`"" }
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskArgs
$triggers = @(
  (New-ScheduledTaskTrigger -AtStartup),
  (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650))
)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
  -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'ctmphone Watchdog' -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Task 'ctmphone Watchdog' registered (every minute)."

Start-ScheduledTask -TaskName 'ctmphone Watchdog'
Start-Sleep -Seconds 8
Write-Host '--- watchdog.log ---'
if (Test-Path "$root\watchdog.log") { Get-Content "$root\watchdog.log" -Tail 10 } else { Write-Host '(empty so far: nothing to report is good)' }
if ($NtfyTopic) { Write-Host "Phone alerts: install the ntfy app and subscribe to topic $NtfyTopic" }
