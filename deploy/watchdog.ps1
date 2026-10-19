# ctmphone watchdog. Run every minute by the "ctmphone Watchdog" task (see install-continuity.ps1).
#   1. App: HTTP 200 on 127.0.0.1:8090/login; restarts it (by PID) after 2 failures in a row.
#   2. Tunnel: starts the tunnel task if the ctmphone cloudflared is gone; restarts it if the
#      PUBLIC url is proxied by Cloudflare and answers badly for 3 minutes while the app is healthy.
#   3. Optional phone alert on every restart/warning via https://ntfy.sh/<NtfyTopic>.
# Public checks stay dormant until ctmphone.online is proxied by Cloudflare (a CF-RAY header).
# Writes C:\sites\ctmphone\watchdog.log (rotated at 2 MB) and keeps counters in watchdog.state.json.
param(
  [string]$NtfyTopic = ''
)

$root = 'C:\sites\ctmphone'
$logFile = Join-Path $root 'watchdog.log'
$stateFile = Join-Path $root 'watchdog.state.json'
$localHealth = 'http://127.0.0.1:8090/login'
$publicHealth = 'https://ctmphone.online/login'

function Write-Log([string]$level, [string]$msg) {
  $line = '{0} {1} {2}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $level, $msg
  if ((Test-Path $logFile) -and ((Get-Item $logFile).Length -gt 2MB)) {
    Move-Item -Force $logFile "$logFile.old"
  }
  Add-Content -Path $logFile -Value $line
}

function Send-Alert([string]$msg) {
  Write-Log 'ALERT' $msg
  if ($NtfyTopic) {
    try { Invoke-RestMethod -Uri "https://ntfy.sh/$NtfyTopic" -Method Post -Body "ctmphone: $msg" -TimeoutSec 10 | Out-Null } catch { }
  }
}

function Test-Url([string]$url, [int]$timeout) {
  try {
    $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $timeout
    return ($r.StatusCode -eq 200)
  } catch { return $false }
}

# WMI CommandLine is blank for S4U-started processes when queried unelevated, so identify
# the ctmphone tunnel by its dedicated metrics port (set in config-ctmphone.yml) instead.
$CtmMetricsPort = 20249
function Get-CtmTunnel {
  try {
    $c = Get-NetTCPConnection -LocalPort $CtmMetricsPort -State Listen -ErrorAction SilentlyContinue
    if ($c) { return @($c.OwningProcess | Select-Object -Unique) }
  } catch { }
  return @()
}

function Stop-CtmTunnel {
  foreach ($id in (Get-CtmTunnel)) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
  try { Stop-ScheduledTask -TaskName 'ctmphone Tunnel' -ErrorAction SilentlyContinue } catch { }
}

function Test-Internet {
  try {
    $c = New-Object System.Net.Sockets.TcpClient
    $iar = $c.BeginConnect('1.1.1.1', 443, $null, $null)
    $ok = $iar.AsyncWaitHandle.WaitOne(3000) -and $c.Connected
    $c.Close()
    return $ok
  } catch { return $false }
}

function Test-CloudflareFronted {
  # The public health restart only makes sense once the domain is proxied (CF-RAY present).
  try {
    $r = Invoke-WebRequest -Uri $publicHealth -UseBasicParsing -TimeoutSec 10
    return [bool]$r.Headers['CF-RAY']
  } catch { return $false }
}

$state = [pscustomobject]@{ appFail = 0; publicFail = 0; lastAppRestart = ''; lastTunnelRestart = '' }
if (Test-Path $stateFile) {
  try {
    $saved = Get-Content $stateFile -Raw | ConvertFrom-Json
    foreach ($p in $state.PSObject.Properties.Name) { if ($null -ne $saved.$p) { $state.$p = $saved.$p } }
  } catch { }
}

function Minutes-Since([string]$stamp) {
  if (-not $stamp) { return 99999 }
  try { return ((Get-Date) - [datetime]::Parse($stamp)).TotalMinutes } catch { return 99999 }
}

# ---- 0. on-demand reload -----------------------------------------------------------------------
# Touch C:\sites\ctmphone\reload.request (any session) to restart the app without an elevated UAC
# prompt; the watchdog runs in the same session as the app, so it can stop it.
$reload = Join-Path $root 'reload.request'
if (Test-Path $reload) {
  Remove-Item -Force $reload -ErrorAction SilentlyContinue
  $conn = Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue
  if ($conn) { Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue }
  Stop-ScheduledTask -TaskName 'ctmphone App' -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
  Start-ScheduledTask -TaskName 'ctmphone App'
  Write-Log 'INFO' 'reload.request seen; restarted the app'
  $state.reloadedAt = (Get-Date).ToString('o')
  $state | ConvertTo-Json | Set-Content -Path $stateFile
  Start-Sleep -Seconds 3
}

# ---- 1. the app ----------------------------------------------------------------------------------
$appOk = $false
if (Test-Url $localHealth 8) {
  $appOk = $true
  if ($state.appFail -gt 0) { Write-Log 'INFO' "app healthy again after $($state.appFail) failed check(s)" }
  $state.appFail = 0
} else {
  $state.appFail = [int]$state.appFail + 1
  Write-Log 'WARN' "app health check failed ($($state.appFail))"
  if ($state.appFail -ge 2 -and (Minutes-Since $state.lastAppRestart) -ge 3) {
    $conn = Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue
    if ($conn) { Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 3 }
    Start-ScheduledTask -TaskName 'ctmphone App'
    $state.lastAppRestart = (Get-Date).ToString('o')
    Send-Alert 'app was not answering; restarted it'
  }
}

# ---- 2. the tunnel -------------------------------------------------------------------------------
$tunnelTask = Get-ScheduledTask -TaskName 'ctmphone Tunnel' -ErrorAction SilentlyContinue
$tunnelUp = ((Get-CtmTunnel).Count -gt 0) -or ($tunnelTask -and $tunnelTask.State -eq 'Running')
if (-not $tunnelUp) {
  if ((Minutes-Since $state.lastTunnelRestart) -ge 2) {
    Start-ScheduledTask -TaskName 'ctmphone Tunnel'
    $state.lastTunnelRestart = (Get-Date).ToString('o')
    Send-Alert 'the ctmphone tunnel was not running; started its task'
  }
} elseif ($appOk -and (Test-CloudflareFronted)) {
  if (Test-Url $publicHealth 10) {
    if ($state.publicFail -gt 0) { Write-Log 'INFO' 'public site reachable again' }
    $state.publicFail = 0
  } else {
    $state.publicFail = [int]$state.publicFail + 1
    Write-Log 'WARN' "public health check failed ($($state.publicFail))"
    if ($state.publicFail -ge 3 -and (Minutes-Since $state.lastTunnelRestart) -ge 10) {
      if (Test-Internet) {
        Stop-CtmTunnel
        Start-Sleep -Seconds 3
        Start-ScheduledTask -TaskName 'ctmphone Tunnel'
        $state.lastTunnelRestart = (Get-Date).ToString('o')
        Send-Alert 'site unreachable from outside while the app is healthy; restarted the tunnel'
      } else {
        Write-Log 'WARN' 'no internet from this host; not restarting the tunnel'
      }
    }
  }
}

$state | ConvertTo-Json | Set-Content -Path $stateFile
