# ctmphone.online — Windows host runbook

Hosts the CTM softphone embed example (`CTMJSON/ctm-softphone-embed-example`) on this Windows box,
the same way JayStakes / JayTrade / JayCrates are hosted: a local app behind a Cloudflare named tunnel,
started at boot by scheduled tasks and kept alive by a watchdog.

## What runs where

| Thing | Value |
|---|---|
| Checkout | `C:\sites\ctmphone` (git `main`) |
| Python | `C:\Users\email\AppData\Local\Programs\Python\Python312` (per-user, installed via winget) |
| App | `.venv\Scripts\waitress-serve.exe --host=127.0.0.1 --port=8090 app:app`, log `C:\sites\ctmphone\app.log` |
| Env | `C:\sites\ctmphone\.env` (gitignored; see `.env.example`) |
| Tunnel | `C:\Tools\cloudflared.exe` tunnel `ctmphone` (`cb5a57be-5276-4b9d-9346-eebc2e52be1e`), config `C:\cloudflared\config-ctmphone.yml`, metrics `127.0.0.1:20249`, log `C:\sites\ctmphone\tunnel.log` |
| Domain | `https://ctmphone.online` and `https://www.ctmphone.online` |

Ports in use on this box: 3001 JayTrade, 3002 JayStakes, 3456 JayCrates, 8080 Cass static, **8090 ctmphone**.
Cloudflared metrics: 20241-20244 the other sites, **20249 ctmphone**.

## Scheduled tasks

| Task | Runs | Trigger |
|---|---|---|
| `ctmphone App` | `C:\sites\ctmphone\start-ctmphone.cmd` | At system start-up |
| `ctmphone Tunnel` | `C:\sites\ctmphone\start-tunnel.cmd` | At system start-up, +30 s delay |
| `ctmphone Watchdog` | `C:\sites\ctmphone\deploy\watchdog.ps1` | Every minute, and at start-up |

All three run as `DESKTOP-VJU81JH\email`, logon type S4U (no stored password, runs with nobody logged in),
standard (non-elevated) run level. Register them (needs an elevated PowerShell — a UAC prompt):

```powershell
powershell -ExecutionPolicy Bypass -File C:\sites\ctmphone\deploy\register-tasks.ps1
powershell -ExecutionPolicy Bypass -File C:\sites\ctmphone\deploy\install-continuity.ps1
```

`deploy\setup-all.ps1` runs both (used for the initial install). Launchers live canonically in `deploy\`
and are copied to the repo root by `install-continuity.ps1`; the tasks run the root copies.

## Watchdog

Every minute `deploy\watchdog.ps1`:

1. Checks `http://127.0.0.1:8090/login`; after 2 failures restarts the app by PID (3 min cooldown).
2. Checks the tunnel by its metrics port `20249`; if gone, starts the `ctmphone Tunnel` task.
3. Once the domain is proxied by Cloudflare (a `CF-RAY` header is present), also checks
   `https://ctmphone.online/login` and restarts the tunnel if it answers badly for 3 minutes while
   the app is healthy (10 min cooldown, skipped if the host has no internet).

Writes `C:\sites\ctmphone\watchdog.log` (rotated at 2 MB) and counters in `watchdog.state.json`.
Pass `-NtfyTopic 'ctmphone-<hard-to-guess>'` to `install-continuity.ps1` for phone alerts via ntfy.sh.

## DNS / Cloudflare (DONE 2026-10-17)

`ctmphone.online` is now a zone on the Cloudflare account (`8e069a79…`), delegated from the registrar
(Namecheap) to Cloudflare NS `fred`/`ines.ns.cloudflare.com`. The two parking records were replaced with
proxied CNAMEs to the tunnel (the Namecheap MX/SPF email records were left alone):

| Name | Type | Proxied | Target |
|---|---|---|---|
| `ctmphone.online` | CNAME | yes | `cb5a57be-5276-4b9d-9346-eebc2e52be1e.cfargotunnel.com` |
| `www.ctmphone.online` | CNAME | yes | `cb5a57be-5276-4b9d-9346-eebc2e52be1e.cfargotunnel.com` |

To recreate (a `cert.pem` scoped to `ctmphone.online` is required):

```powershell
C:\Tools\cloudflared.exe tunnel login          # pick ctmphone.online in the browser
C:\Tools\cloudflared.exe tunnel route dns ctmphone ctmphone.online
C:\Tools\cloudflared.exe tunnel route dns ctmphone www.ctmphone.online
```

Caution: `cloudflared tunnel login` writes `C:\Users\email\.cloudflared\cert.pem` and refuses if one already
exists. The original jaystakes/-com origin cert is kept at
`C:\Users\email\.cloudflared\cert-jaystakes.com.pem`; the current `cert.pem` is the ctmphone.online one.
Also note `route dns` appends the name to whatever zone the active cert covers — an old jaystakes.com cert
once created bogus `ctmphone.online.jaystakes.com` records (deleted).

## CTM OAuth

1. The OAuth app's redirect URI must be **exactly** `https://ctmphone.online/oauth/callback`.
   The authorize URL is built as (verified live):
   `https://app.calltrackingmetrics.com/oauth2/authorize?client_id=3O2dhtd4oeuBHnzG8iZ3dUKxqzP-Yhnc41f11yLbs48&redirect_uri=https%3A%2F%2Fctmphone.online%2Foauth%2Fcallback&response_type=code&scope=profile+reports+activity+manage&state=…`
2. `.env` currently has `CTM_OAUTH_CLIENT_ID` set; the scope `profile reports activity manage` is a
   built-in default (keep `manage` or `phone_access` gets 403). `CTM_OAUTH_CLIENT_SECRET` is left blank —
   CTM's documented web flow does not require it. Add it if the token exchange fails without one.
   Identity/account are resolved from the token; if the API cannot supply them the app shows
   `/oauth/complete.html` once to fill email, first/last name, and account ID (or set
   `CTM_ACCOUNT_ID`/`CTM_USER_*` in `.env`).
3. Restart the app after changing `.env`. Easiest (no UAC): drop a sentinel file and let the
   watchdog do it within a minute (it runs in the same session as the app):

   ```powershell
   New-Item -ItemType File -Force C:\sites\ctmphone\reload.request | Out-Null
   ```

   Or run the elevated restart (kills/restarts app + tunnel; needs a UAC prompt):

   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\sites\ctmphone\deploy\restart.ps1
   ```

## Number lookup (IPQualityScore)

The phone-number lookup (`GET /ctm/phone-lookup`) uses `IPQS_API_KEY` (`.env`, key kept server-side).
Verified working 2026-10-17 (key returns `success: true`). If lookups return "IPQualityScore account
has insufficient credits", add credits in the IPQS dashboard.

## Deploy an update

```powershell
cd C:\sites\ctmphone
& 'C:\Program Files\Git\cmd\git.exe' pull
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt   # only if requirements changed
# restart app + tunnel (elevated; kills only the ctmphone processes by port/cmdline)
powershell -ExecutionPolicy Bypass -File C:\sites\ctmphone\deploy\restart.ps1
```

## Verify

```powershell
(Invoke-WebRequest http://127.0.0.1:8090/login -UseBasicParsing).StatusCode                 # 200
(Invoke-WebRequest https://ctmphone.online/login -UseBasicParsing).StatusCode              # 200 (after DNS)
Get-Content C:\sites\ctmphone\app.log -Tail 20
Get-Content C:\sites\ctmphone\tunnel.log -Tail 20
Get-Content C:\sites\ctmphone\watchdog.log -Tail 20                                         # silence is good
```

Open <https://ctmphone.online>, choose **Connect with CTM**, approve the scopes, then check
`/oauth/whoami` shows `granted_scope` including `manage` and `phone_access_bearer_status: 200`.

## Gotchas (each cost real time)

| Symptom | Cause / fix |
|---|---|
| App crashes at start: `ModuleNotFoundError: tzdata` / `ZoneInfoNotFoundError` | Windows Python has no IANA tz database. `pip install tzdata` in the venv (already done). |
| Activity logs (calls/text/forms), availability, news 500 with `ValueError: Invalid format string` | glibc-only `%-I`/`%-d` in `strftime` (app.py: 8 sites). Windows wants `%#I`/`%#d`. Patched locally 2026-10-17; re-apply after a `git pull` if upstream reverts it. |
| `start-tunnel.cmd` exits 1, `The process cannot access the file because it is being used by another process` | A detached/orphaned cloudflared still holds `tunnel.log`. Run `deploy\reset-tunnel.ps1` **elevated** (kills the ctmphone tunnel by metrics port/command line, clears the log, restarts the task). |
| `Register-ScheduledTask` / `Disable-ScheduledTask`: `Access is denied` | Registering/editing S4U boot tasks needs elevation. Run the deploy scripts from an elevated PowerShell (UAC). |
| `Get-CimInstance Win32_Process … CommandLine` is blank for the app/tunnel | Task Scheduler S4U processes hide their command line from unelevated queries. The watchdog identifies the tunnel by its metrics port `20249`, not by command line. |
| `cloudflared tunnel route dns` created `ctmphone.online.jaystakes.com` | The default `~/.cloudflared/cert.pem` is scoped to `jaystakes.com`, so the name was appended to that zone. Delete those records and re-run `tunnel login` for `ctmphone.online` (or add the CNAMEs by hand). |
| A new tunnel instance never takes the new config after an edit | Stopping the task does not always kill the cloudflared child; use `deploy\reset-tunnel.ps1` elevated. |
| Cloudflare 1033 / 502 | Tunnel or app not running — check both `ctmphone` tasks and the ports (`8090`, `20249`). |
| Cloudflare 1016 | CNAME points at a different tunnel id — recreate the record with `cb5a57be-…cfargotunnel.com`. |
| Never | `taskkill /IM node.exe` / `Stop-Process -Name cloudflared` (JayTrade/JayStakes/JayCrates/Cass share them); stop only the process whose metrics port is `20249`. Commit `.env` or the tunnel credentials `C:\cloudflared\cb5a57be-*.json`. |
