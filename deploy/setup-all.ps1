# Elevated one-shot: registers the ctmphone App/Tunnel/Watchdog tasks.
# Launched via UAC by the operator. Output goes to C:\sites\ctmphone\install.log.
$ErrorActionPreference = 'Continue'
Start-Transcript -Path 'C:\sites\ctmphone\install.log' -Append
try {
  & 'C:\sites\ctmphone\deploy\register-tasks.ps1'
  & 'C:\sites\ctmphone\deploy\install-continuity.ps1'
} catch {
  Write-Host "setup-all failed: $($_.Exception.Message)"
}
Stop-Transcript
