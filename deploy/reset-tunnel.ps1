# Elevated: stop the ctmphone tunnel task, kill any ctmphone cloudflared (including detached
# orphans holding tunnel.log), clear the log, and start the tunnel again on the current config.
$ErrorActionPreference = 'Continue'
Start-Transcript -Path 'C:\sites\ctmphone\reset-tunnel.log' -Append
try {
  Stop-ScheduledTask -TaskName 'ctmphone Tunnel' -ErrorAction SilentlyContinue
  Start-Sleep 2
  # by metrics port range (covers the old 20245 and new 20249)
  20245..20249 | ForEach-Object {
    $c = Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue
    if ($c) { $c.OwningProcess | Select-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue; Write-Host "killed port-owner $_ on $_" } }
  }
  # by command line (readable when elevated)
  Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*config-ctmphone.yml*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "killed cmdline match $($_.ProcessId)" }
  Start-Sleep 3
  Remove-Item -Force 'C:\sites\ctmphone\tunnel.log','C:\sites\ctmphone\tunnel.log.old' -ErrorAction SilentlyContinue
  Start-ScheduledTask -TaskName 'ctmphone Tunnel'
  Start-ScheduledTask -TaskName 'ctmphone App'
  Start-Sleep 10
  Write-Host ('tunnel state: ' + (Get-ScheduledTask -TaskName 'ctmphone Tunnel').State)
  Write-Host ('tunnel result: ' + (Get-ScheduledTaskInfo -TaskName 'ctmphone Tunnel').LastTaskResult)
  $c = Get-NetTCPConnection -LocalPort 20249 -State Listen -ErrorAction SilentlyContinue
  Write-Host ('metrics 20249 owner: ' + ($c.OwningProcess -join ','))
  Write-Host ('app 8090 owner: ' + ((Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue).OwningProcess -join ','))
} catch {
  Write-Host "reset-tunnel failed: $($_.Exception.Message)"
}
Stop-Transcript
