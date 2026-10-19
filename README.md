# CTM softphone inside a custom app

<img width="1918" height="927" alt="image" src="https://github.com/user-attachments/assets/0350f5ff-f591-4357-a992-361ee5420d6c" />


This is a working example of dropping the [CallTrackingMetrics](https://www.calltrackingmetrics.com/) phone widget and activity APIs into **your own** web application. The surrounding UI is a demo workspace (weather, news, sports, markets, games, phone lookup). Those panels are not CTM products. They exist so you can see the phone and the activity logs living next to an app you control.

The pattern is the same one you would use inside a CRM, EMR, EHR, or internal desk:

1. Your app owns the page, the layout, and the agent identity.
2. CTM OAuth proves who the agent is.
3. Your backend exchanges that grant for a short-lived phone embed token.
4. The `<ctm-phone-embed>` widget renders inside your page and talks only to your token endpoints.
5. Your backend calls the CTM activities API and renders calls, texts, and form submissions in your own UI.

There is no API key in the browser. The OAuth client secret never leaves the server.

## What you will see

| Region | Source | Purpose |
| --- | --- | --- |
| Left phone panel | CTM phone embed | Place and receive calls in-page |
| Call Log / Text Log / Forms | CTM activities API | Your own rendering of CTM activity |
| Agent availability | CTM realtime API | Who is ready, busy, or offline |
| Weather, news, sports, markets, games, IPQS lookup | Third-party or local demo code | Stand-ins for "the rest of your app" |

## Architecture

```text
Browser (your app)
  login page
    -> GET /oauth/login
    -> CTM authorize (scope: profile reports activity manage)
    -> GET /oauth/callback
  phone page
    <ctm-phone-embed access="/ctm-phone-access" refresh="/ctm-phone-refresh">
    tabs fetch /ctm/calls, /ctm/messages, /ctm/forms, /ctm/ops-feed

Your Flask server
  stores the OAuth refresh token in the user session
  refreshes the access token server-side
  POST /accounts/{id}/phone_access  -> token the embed can use
  GET  /accounts/{id}/activities    -> calls, texts, forms
  GET  /accounts/{id}/realtime      -> agent presence
```

The embed script is loaded from CTM, not vendored:

```html
<script src="https://app.calltrackingmetrics.com/ctm-phone-embed-1.0.js"></script>

<ctm-phone-embed
  id="my-phone"
  access="/ctm-phone-access"
  refresh="/ctm-phone-refresh"
  auto="true">
</ctm-phone-embed>
```

`access` and `refresh` are **your** URLs. The widget calls them. It does not receive your OAuth client secret, and it does not call `phone_access` itself.

Supported embed attributes used here are only `access`, `refresh`, and `auto`. Theme is not a host attribute. Do not expect to recolor the phone chrome from the parent page.

## OAuth, and why `manage` is required

Login is the authorization-code flow. There is no API-key login in this example.

1. `GET /oauth/login` builds the authorize URL and stores a random `state`.
2. The agent signs in at `https://app.calltrackingmetrics.com/oauth2/authorize`.
3. CTM redirects to `GET /oauth/callback` with `code` and `state`.
4. The server POSTs the code to `https://api.calltrackingmetrics.com/oauth2/token`.
5. Access and refresh tokens live in the Flask session. The browser never sees the client secret.

Requested scopes:

```text
profile reports activity manage
```

`reports` and `activity` are what the call, text, and form feeds need. **`manage` is what `phone_access` needs.** Assigning the OAuth app to the account is not enough. The scope has to be in the authorize URL, and the token that comes back has to include it.

Check a live session at `/oauth/whoami`. A healthy embed setup looks like:

```json
{
  "granted_scope": "profile reports activity manage",
  "phone_access_bearer_status": 200
}
```

If `phone_access` returns 403, the usual cause is a token that was issued without `manage`. Log out and connect again after the OAuth app is allowed to request that scope.

If the OAuth app was created by dynamic client registration, the `manage` token only works while the authorizing user is an owner or admin of the account.

## Phone embed token endpoints

`POST /ctm-phone-access` is what the widget calls on load (`auto="true"` skips the Launch Phone button).

The server then calls:

```http
POST https://app.calltrackingmetrics.com/api/v1/accounts/{account_id}/phone_access
Authorization: Bearer {oauth access token}
Content-Type: application/json

{
  "email": "agent@example.com",
  "first_name": "Ada",
  "last_name": "Lovelace",
  "session_id": "stable-per-browser-session"
}
```

CTM returns a phone access payload. This app passes that payload back to the embed. `POST /ctm-phone-refresh` repeats the same call when the embed asks for a new token.

Identity notes:

- The email and name do not have to match an existing CTM user for `phone_access` to return 200.
- They **should** match the agent you want credited for calls, presence, and reporting.
- `session_id` is generated once per Flask session so refresh does not look like a brand-new agent.

The parent page listens for `ctm:*` events from the embed (`ctm:ready`, `ctm:incoming`, `ctm:oncall`, `ctm:wrapup`, `ctm:error`, and the rest) and maps them onto the small status dot next to the agent name. That dot is your app's status. The green Ready bar inside the phone is the embed's own status. They describe the same agent, so this example keeps the outer one to a dot plus a short word instead of a second pill.

To dial from your own UI, call the embed helper:

```javascript
window.populateEmbeddedPhone("+15555550100");
```

The Forms tab uses that for the submission's dialable number.

## Activities: calls, texts, and forms

All three logs come from one CTM endpoint, filtered by direction:

```http
GET /api/v1/accounts/{account_id}/activities
    ?direction[]=inbound&direction[]=outbound     # calls
    ?direction[]=inbound_sms&direction[]=outbound_sms
    ?direction[]=form
```

The server normalizes each payload into the fields the templates expect. The browser then fetches HTML fragments:

| Your route | Template | CTM direction |
| --- | --- | --- |
| `GET /ctm/calls` | `templates/data/calls.html` | inbound, outbound |
| `GET /ctm/messages` | `templates/data/messages.html` | inbound_sms, outbound_sms |
| `GET /ctm/forms` | `templates/data/forms.html` | form |

Form submissions are the least obvious shape. The activity has a `form` object: flat fields plus a `custom` array of `{label, value, id}`. This app flattens both into a labeled grid and keeps routing metadata (`number_to_call`, `customer_number_to_dial`, `trackback_id`, `form_id`) as chips. The Call button uses `customer_number_to_dial` or the contact phone, then `populateEmbeddedPhone`.

Agent availability is separate from activities:

```http
GET /api/v1/accounts/{account_id}/realtime
```

`GET /ctm/ops-feed` renders that into the Agent Availability card. "Offline" there is CTM presence. It is not the embed's Ready button, and it is not a second login.

## What is intentionally not CTM

These are here so the example looks like a real desk. Replace them with your own CRM widgets.

- **Weather** uses Open-Meteo from the browser. No key.
- **News** aggregates public RSS feeds server-side (`NEWS_SOURCES` in `app.py`).
- **Sports** uses the ESPN scoreboard API. No key.
- **Markets** uses the Yahoo Finance chart API (`/v8/finance/chart/{symbol}`). The quote endpoint returns 401 without a key; the chart endpoint does not.
- **Number lookup** calls IPQualityScore with a server-side key (`IPQS_API_KEY`). The key is not sent to the browser.
- **Games** are iframes or local demo pages (Doom, Retro, Pac-Man, Breakout). They are not part of the CTM integration. Retro is a same-origin NES page (`static/retro.html`) because several hosted emulators cannot create a WebGL context inside a cross-origin iframe.

## Run it

Requirements: Python 3.9+, a CTM OAuth app, and a CTM account the authorizing user can access.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

```bash
CTM_OAUTH_CLIENT_ID=...
CTM_OAUTH_CLIENT_SECRET=...
CTM_OAUTH_REDIRECT_URI=http://localhost:8080/oauth/callback
CTM_ACCOUNT_ID=123456
CTM_USER_EMAIL=agent@example.com
CTM_USER_FIRST=Ada
CTM_USER_LAST=Lovelace
FLASK_SECRET_KEY=generate-a-long-random-string
```

Register that exact redirect URI on the OAuth app. Then:

```bash
python app.py
```

Open `http://localhost:8080`, choose **Connect with CTM**, approve the requested scopes, and confirm `/oauth/whoami` shows `manage` plus `phone_access_bearer_status: 200`.

```bash
python -m unittest discover tests
```

## Files worth reading

| File | Why it matters |
| --- | --- |
| `app.py` | OAuth, token refresh, `phone_access`, activities, forms, realtime |
| `templates/phone.html` | Embed tag, tab shell, `ctm:*` status events, dial helper |
| `templates/login.html` | OAuth start link. It is a GET, not a form POST |
| `templates/data/calls.html` | How a call activity becomes a row in your UI |
| `templates/data/messages.html` | Same idea for SMS |
| `templates/data/forms.html` | Form field grid plus Click-to-call |
| `templates/data/ops_panel.html` | Realtime agent availability |
| `.env.example` | Every setting the server expects |

## Deploying the Doom tab

`DOOM_TAB_DEPLOYMENT.md` documents the exact embed URL, iframe attributes, CSS sizing rules, and response headers that make the Doom tab render locally, plus a triage list for a public host such as `ctmphone.online`. If the game is blank or cropped after deploying, start there.
## Security notes for anyone copying this

- Keep `CTM_OAUTH_CLIENT_SECRET`, `FLASK_SECRET_KEY`, and `IPQS_API_KEY` in the environment or a gitignored `.env`.
- Do not put an API key or the client secret in a template, a static file, or the embed tag.
- The phone access token is short-lived and is only returned to the embed from your backend.
- Session cookies should be `Secure` and `HttpOnly` once this is not on localhost. Set `SESSION_COOKIE_SECURE=1` behind HTTPS.
- This demo's game and news routes are unauthenticated only in the sense that they do not call CTM. The phone page and every `/ctm/*` route require a logged-in session.

## Questions, support, and feedback

Jason Smith, [jason.smith@ctm.com](mailto:jason.smith@ctm.com).

The Retro tab plays a local NES ROM. [Vimm's Lair](https://vimm.net/) is a place to find ROMs to try. Download a `.nes` file, then load it with the file picker in the Retro tab. This example does not host or bundle ROMs.

## License

Example code for integrating with CallTrackingMetrics. CTM names, the phone embed script, and the CTM API are CallTrackingMetrics products. The demo widgets around them are illustrative only.
