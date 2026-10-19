# Registers the two start-up tasks that run the ctmphone app and its tunnel.
#   powershell -ExecutionPolicy Bypass -File C:\sites\ctmphone\deploy\register-tasks.ps1
# Same shape as the JayStakes tasks: At system start-up, run as the owner with logon
# type S4U (no stored password, runs with nobody logged in), standard (non-elevated).
$ErrorActionPreference = 'Stop'
$root = 'C:\sites\ctmphone'

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$appAction = New-ScheduledTaskAction -Execute "$root\start-ctmphone.cmd"
Register-ScheduledTask -TaskName 'ctmphone App' -Action $appAction -Trigger (New-ScheduledTaskTrigger -AtStartup) `
  -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Task 'ctmphone App' registered."

$tunTrigger = New-ScheduledTaskTrigger -AtStartup
$tunTrigger.Delay = 'PT30S'
$tunAction = New-ScheduledTaskAction -Execute "$root\start-tunnel.cmd"
Register-ScheduledTask -TaskName 'ctmphone Tunnel' -Action $tunAction -Trigger $tunTrigger `
  -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Task 'ctmphone Tunnel' registered."
