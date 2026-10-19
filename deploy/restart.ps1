# Elevated: cleanly restart the ctmphone app and tunnel.
# Needed from an interactive shell because the S4U tasks run in session 0 and their processes
# cannot be killed from session 1 without elevation (the watchdog itself runs in session 0, so it can).
$ErrorActionPreference = 'Continue'
Start-Transcript -Path 'C:\sites\ctmphone\restart.log' -Append
try {
  foreach ($t in 'ctmphone App', 'ctmphone Tunnel') { Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue }
  Start-Sleep 3

  # app: whatever holds 8090, plus any python running our venv
  $c = Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue
  if ($c) { $c.OwningProcess | Select-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue; Write-Host "killed app pid $_" } }
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -like '*\sites\ctmphone\.venv\*' -or $_.CommandLine -like '*waitress-serve*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "killed venv python $($_.ProcessId)" }

  # tunnel: by command line, then by our metrics ports
  Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*config-ctmphone.yml*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "killed tunnel $($_.ProcessId)" }
  20245..20249 | ForEach-Object {
    $x = Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue
    if ($x) { $x.OwningProcess | Select-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue; Write-Host "killed port-owner $_ on $_" } }
  }
  Start-Sleep 3

  Remove-Item -Force 'C:\sites\ctmphone\app.log','C:\sites\ctmphone\tunnel.log','C:\sites\ctmphone\app.log.old','C:\sites\ctmphone\tunnel.log.old' -ErrorAction SilentlyContinue

  Start-ScheduledTask -TaskName 'ctmphone App'
  Start-ScheduledTask -TaskName 'ctmphone Tunnel'
  Start-Sleep 12
  Write-Host ('app state:    ' + (Get-ScheduledTask -TaskName 'ctmphone App').State)
  Write-Host ('app 8090 pid: ' + ((Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue).OwningProcess -join ','))
  Write-Host ('tunnel state: ' + (Get-ScheduledTask -TaskName 'ctmphone Tunnel').State)
  Write-Host ('tunnel 20249: ' + ((Get-NetTCPConnection -LocalPort 20249 -State Listen -ErrorAction SilentlyContinue).OwningProcess -join ','))
  try { Write-Host ('local /login: ' + (Invoke-WebRequest http://127.0.0.1:8090/login -UseBasicParsing -TimeoutSec 10).StatusCode) } catch { Write-Host 'local /login: ERR' }
} catch {
  Write-Host "restart failed: $($_.Exception.Message)"
}
Stop-Transcript
