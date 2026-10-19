@echo off
rem CTM softphone embed demo. Run by the "ctmphone App" scheduled task.
rem Serves the Flask app with waitress on 127.0.0.1:8090; Cloudflare tunnel fronts it.
setlocal
cd /d C:\sites\ctmphone
rem Keep the log from growing forever: at start, move a log over 50 MB aside (one generation kept).
for %%F in (app.log) do if exist "%%F" if %%~zF GTR 52428800 move /y "%%F" "app.log.old" >nul
"C:\sites\ctmphone\.venv\Scripts\waitress-serve.exe" --host=127.0.0.1 --port=8090 --threads=8 app:app >> C:\sites\ctmphone\app.log 2>&1
