@echo off
rem Cloudflare tunnel for ctmphone.online. Run by the "ctmphone Tunnel" task.
setlocal
cd /d C:\sites\ctmphone
for %%F in (tunnel.log) do if exist "%%F" if %%~zF GTR 52428800 move /y "%%F" "tunnel.log.old" >nul
C:\Tools\cloudflared.exe tunnel --config C:\cloudflared\config-ctmphone.yml run >> C:\sites\ctmphone\tunnel.log 2>&1
