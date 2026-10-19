"""
CTM Softphone Web App — desk mode
----------------------------------
Two-panel layout: CTM phone embed (left) + activity log (right).
Phone embed authenticates via /ctm-phone-access using identity from .env
or a one-time login form.
"""

import base64
import html
import functools
import json
import os
import re
import secrets
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlencode
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests

from flask import (
    Flask, session, request, redirect, url_for,
    render_template, jsonify, Response
)
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)


def _asset_version(filename):
    """mtime-based version so a changed stylesheet gets a fresh URL."""
    try:
        return int(os.path.getmtime(os.path.join(app.root_path, "static", filename)))
    except OSError:
        return 0


app.jinja_env.globals["asset_v"] = _asset_version("style.css")


@app.before_request
def force_apex_host():
    """Canonicalize www.<host> down to <host>.

    The CTM OAuth app registers the apex redirect URI, and the OAuth `state`
    lives in a host-scoped session cookie, so a flow started on www would fail
    at the apex callback. Sending www to the apex keeps one origin.
    """
    host = request.host.split(":", 1)[0].lower()
    if host.startswith("www."):
        target = "https://" + host[4:] + request.path
        if request.query_string:
            target += "?" + request.query_string.decode()
        return redirect(target, code=301)


@app.after_request
def allow_embedded_webgl(response):
    """Let game iframes use WebGL. A parent Permissions-Policy otherwise
    disables the feature for cross-origin embeds such as Webretro."""
    response.headers.setdefault(
        "Permissions-Policy",
        "fullscreen=*, gamepad=*, autoplay=*, clipboard-write=*, "
        "accelerometer=*, gyroscope=*",
    )
    return response

DEFAULT_CTM_ACCOUNT_ID = os.environ.get("CTM_ACCOUNT_ID", os.environ.get("DEFAULT_ACCOUNT_ID", ""))
CTM_BASE               = "https://app.calltrackingmetrics.com/api/v1"

# Optional: pre-configure user identity so no login form is needed
DEFAULT_CTM_USER_EMAIL = os.environ.get("CTM_USER_EMAIL", "")
DEFAULT_CTM_USER_FIRST = os.environ.get("CTM_USER_FIRST", "")
DEFAULT_CTM_USER_LAST  = os.environ.get("CTM_USER_LAST", "")
DEFAULT_CTM_CHAT_TOKEN = os.environ.get("CTM_CHAT_TOKEN", "").strip()
APP_TIMEZONE = ZoneInfo("America/New_York")

# ── CTM OAuth2 (web authorization-code flow) ─────────────────────────────────
# Client credentials are read from the environment, and fall back to the macOS
# keychain (service "ctm-env", account "ctm_oauth_softphone" / "oauth_secret_id").
CTM_OAUTH_AUTHORIZE_URL = os.environ.get(
    "CTM_OAUTH_AUTHORIZE_URL", "https://app.calltrackingmetrics.com/oauth2/authorize"
)
CTM_OAUTH_TOKEN_URL = os.environ.get(
    "CTM_OAUTH_TOKEN_URL", "https://api.calltrackingmetrics.com/oauth2/token"
)
CTM_OAUTH_SCOPE = os.environ.get("CTM_OAUTH_SCOPE", "profile reports activity manage").strip()
CTM_OAUTH_REDIRECT_URI = os.environ.get("CTM_OAUTH_REDIRECT_URI", "").strip()
KEYCHAIN_SERVICE = os.environ.get("CTM_KEYCHAIN_SERVICE", "ctm-env")
KEYCHAIN_CLIENT_ID_ACCOUNT = os.environ.get("CTM_KEYCHAIN_CLIENT_ID", "ctm_oauth_softphone")
KEYCHAIN_CLIENT_SECRET_ACCOUNT = os.environ.get("CTM_KEYCHAIN_CLIENT_SECRET", "oauth_secret_id")

# IPQualityScore phone validation (key read from env or the macOS keychain).
KEYCHAIN_IPQS_ACCOUNT = os.environ.get("IPQS_KEYCHAIN_ACCOUNT", "ipqskeeper")
IPQS_BASE = "https://www.ipqualityscore.com/api/json/phone"
DEFAULT_CHAT_TOKENS = {
    "11774": "eyJhbGciOiJub25lIn0.eyJyIjoicHJvZHVjdGlvbiIsImkiOjIwOTYsImEiOjExNzc0LCJwIjoiaHR0cHM6IiwiaCI6ImNoYXQuYWN0bS54eXoifQ.",
}
DEFAULT_OPS_WEATHER_CITY = os.environ.get("OPS_WEATHER_CITY", "New York, NY").strip()
DEFAULT_OPS_NEWS_FEED_URL = os.environ.get("OPS_NEWS_FEED_URL", "https://feeds.npr.org/1001/rss.xml").strip()
WEATHER_CODE_LABELS = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Heavy freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Rain showers",
    81: "Heavy showers",
    82: "Violent showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm and hail",
    99: "Severe thunderstorm",
}


def keychain_read(account, service=KEYCHAIN_SERVICE):
    """Read a secret from the macOS login keychain. Returns "" on any failure."""
    if not account:
        return ""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def get_oauth_client_id():
    return (
        os.environ.get("CTM_OAUTH_CLIENT_ID", "").strip()
        or keychain_read(KEYCHAIN_CLIENT_ID_ACCOUNT)
    )


def get_oauth_client_secret():
    return (
        os.environ.get("CTM_OAUTH_CLIENT_SECRET", "").strip()
        or keychain_read(KEYCHAIN_CLIENT_SECRET_ACCOUNT)
    )


def get_ipqs_api_key():
    # Prefer env, then the working keychain entry, then legacy fallbacks.
    env_key = os.environ.get("IPQS_API_KEY", "").strip()
    if env_key:
        return env_key
    for account in (KEYCHAIN_IPQS_ACCOUNT, "ipqs3", "ipqs"):
        value = keychain_read(account)
        if value:
            return value
    return ""


def normalize_phone_number(raw):
    """Strip a phone number to digits, keeping a leading + if present."""
    if not raw:
        return ""
    cleaned = re.sub(r"[^\d+]", "", str(raw).strip())
    if cleaned.startswith("+"):
        cleaned = "+" + cleaned[1:].replace("+", "")
    else:
        cleaned = cleaned.replace("+", "")
    return cleaned


def lookup_phone_number(raw, country="US"):
    """Validate a phone number via the IPQualityScore Phone Validation API.

    Returns a normalized dict for the UI. Raises RuntimeError if no key or the
    number/key is rejected.
    """
    api_key = get_ipqs_api_key()
    if not api_key:
        raise RuntimeError("IPQS API key is not configured.")

    digits = normalize_phone_number(raw)
    if len(re.sub(r"\D", "", digits)) < 7:
        raise RuntimeError("Enter a valid phone number.")

    params = {}
    if country:
        params["country[]"] = country

    response = requests.get(f"{IPQS_BASE}/{api_key}/{quote_plus(digits)}", params=params, timeout=15)
    response.raise_for_status()
    data = response.json()

    if not data.get("success"):
        message = (data.get("message") or "Lookup failed.").strip()
        lowered = message.lower()
        if "credit" in lowered:
            message = (
                "IPQualityScore account has insufficient credits for phone lookups. "
                "Add credits in your IPQS dashboard and try again."
            )
        elif "unauthorized" in lowered or "invalid" in lowered:
            message = (
                "IPQualityScore rejected the API key. Check the key stored in the "
                "keychain (service ctm-env, account ipqs3)."
            )
        raise RuntimeError(message)

    return {
        "number": raw.strip(),
        "formatted": first_non_empty(data.get("formatted"), digits),
        "valid": bool(data.get("valid")),
        "active": bool(data.get("active")),
        "carrier": data.get("carrier") or "",
        "line_type": data.get("line_type") or "",
        "country": data.get("country") or "",
        "city": data.get("city") or "",
        "timezone": data.get("timezone") or "",
        "dialing_code": data.get("dialing_code") or "",
        "fraud_score": data.get("fraud_score"),
        "risky": bool(data.get("risky")),
        "recent_abuse": bool(data.get("recent_abuse")),
        "voip": bool(data.get("VOIP")),
        "prepaid": bool(data.get("prepaid")),
        "do_not_call": bool(data.get("do_not_call")),
        "spammer": bool(data.get("spammer")),
        "leaked": bool(data.get("leaked")),
        "name": data.get("name") or "",
    }


def oauth_configured():
    return bool(get_oauth_client_id())


def get_oauth_redirect_uri():
    if CTM_OAUTH_REDIRECT_URI:
        return CTM_OAUTH_REDIRECT_URI
    # Default to localhost:PORT to match the URI typically registered for this
    # OAuth app. Override with CTM_OAUTH_REDIRECT_URI if you registered a
    # different host/port.
    port = os.environ.get("PORT", "8080")
    return f"http://localhost:{port}/oauth/callback"


def build_authorize_url(state):
    params = {
        "client_id": get_oauth_client_id(),
        "redirect_uri": get_oauth_redirect_uri(),
        "response_type": "code",
        "scope": CTM_OAUTH_SCOPE,
        "state": state,
    }
    return f"{CTM_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def oauth_token_request(grant, **fields):
    """POST to the CTM token endpoint. Returns parsed JSON on success."""
    payload = {"client_id": get_oauth_client_id(), **fields}
    secret = get_oauth_client_secret()
    if secret:
        payload["client_secret"] = secret
    response = requests.post(CTM_OAUTH_TOKEN_URL, data=payload, timeout=15)
    if response.status_code >= 400:
        raise RuntimeError(
            f"CTM token request failed ({response.status_code}): {response.text[:300]}"
        )
    return response.json()


def store_oauth_token(payload):
    expires_in = int(payload.get("expires_in") or 0)
    session["oauth"] = {
        "access_token": payload.get("access_token", ""),
        "refresh_token": payload.get("refresh_token", ""),
        "expires_at": (time.time() + expires_in) if expires_in else 0,
        # Store any scope info CTM echoes so we can surface it for debugging.
        "scope": first_non_empty(payload.get("scope"), payload.get("scopes")),
    }


def describe_access_token():
    """Best-effort decode of the current OAuth token: scopes + expiry.

    CTM tokens may be JWTs (decode the payload) or opaque strings. Either way we
    also report the scopes we *requested* so a 403 can be diagnosed quickly.
    """
    token = extract_mapping(session.get("oauth"))
    access_token = token.get("access_token", "")
    info = {
        "requested_scope": CTM_OAUTH_SCOPE,
        "granted_scope": token.get("scope") or "(not echoed by CTM)",
        "is_jwt": False,
        "jwt_scope": None,
        "expires_in_seconds": None,
    }
    if not access_token:
        return info

    expires_at = token.get("expires_at") or 0
    if expires_at:
        info["expires_in_seconds"] = int(expires_at - time.time())

    parts = access_token.split(".")
    if len(parts) == 3:
        info["is_jwt"] = True
        try:
            padding = "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
            info["jwt_scope"] = first_non_empty(claims.get("scope"), claims.get("scopes"))
            info["jwt_claims"] = {k: claims[k] for k in ("scope", "scopes", "client_id", "account_id", "sub", "exp") if k in claims}
        except Exception as exc:
            info["jwt_decode_error"] = str(exc)
    return info


def refresh_oauth_token_if_needed():
    token = extract_mapping(session.get("oauth"))
    if not token.get("access_token"):
        return None
    expires_at = token.get("expires_at") or 0
    # Refresh when expired or within 60 seconds of expiring.
    if expires_at and time.time() < (expires_at - 60):
        return token["access_token"]
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        return token["access_token"] if time.time() < (expires_at or 0) else None
    try:
        payload = oauth_token_request(
            "refresh_token",
            grant_type="refresh_token",
            refresh_token=refresh_token,
        )
    except (requests.RequestException, RuntimeError):
        return None
    store_oauth_token(payload)
    return payload.get("access_token")


def current_access_token():
    token = extract_mapping(session.get("oauth"))
    if not token.get("access_token"):
        return ""
    return refresh_oauth_token_if_needed() or token.get("access_token", "")


def oauth_enabled_session():
    """True when we authenticated via OAuth (has a usable access token)."""
    return bool(extract_mapping(session.get("oauth")).get("access_token"))


def oauth_api_get(path, params=None, access_token=None):
    token = access_token or current_access_token()
    if not token:
        raise RuntimeError("No OAuth access token available")
    r = requests.get(
        f"{CTM_BASE}{path}",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def resolve_oauth_identity():
    """Best-effort lookup of email / name / account id from the OAuth token.

    CTM does not expose a documented profile endpoint, so we probe the accounts
    resource (which accepts Bearer auth) and fall back to session-stored values.
    """
    token = current_access_token()
    if not token:
        return None

    identity = {
        # An explicit CTM_USER_* setting wins (useful to pin the demo to a
        # specific CTM softphone user); otherwise use whatever the token gave us.
        "email": first_non_empty(
            os.environ.get("CTM_USER_EMAIL", ""), session.get("oauth_email")
        ),
        "first_name": first_non_empty(
            os.environ.get("CTM_USER_FIRST", ""), session.get("oauth_first_name")
        ),
        "last_name": first_non_empty(
            os.environ.get("CTM_USER_LAST", ""), session.get("oauth_last_name")
        ),
        "account_id": "",
    }

    for path in ("/accounts.json", "/accounts"):
        try:
            payload = oauth_api_get(path, {"per_page": 1}, access_token=token)
        except (requests.RequestException, RuntimeError):
            continue

        records = extract_collection(payload)
        if not records and isinstance(payload, dict):
            records = [payload]

        for record in records:
            if not isinstance(record, dict):
                continue
            identity["account_id"] = first_non_empty(
                identity["account_id"],
                record.get("id"),
                record.get("account_id"),
            )
            user = extract_mapping(
                record.get("user") or record.get("owner") or record.get("account") or {}
            )
            identity["email"] = first_non_empty(identity["email"], user.get("email"))
            identity["first_name"] = first_non_empty(
                identity["first_name"], user.get("first_name")
            )
            identity["last_name"] = first_non_empty(
                identity["last_name"], user.get("last_name")
            )
        if identity["account_id"]:
            break

    if not identity["account_id"]:
        identity["account_id"] = normalize_account_id_input(
            session.get("oauth_account_id", "")
        )

    return identity


def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        _auto_login()
        if not login_session_ready():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def _auto_login():
    """OAuth sessions carry their own identity; nothing to auto-fill."""
    return


def get_runtime_config():
    config = extract_mapping(session.get("ctm_config"))
    account_id = first_non_empty(
        config.get("account_id"),
        session.get("oauth_account_id"),
        DEFAULT_CTM_ACCOUNT_ID,
    )
    return {
        "account_id": account_id,
        "chat_token": first_non_empty(config.get("chat_token"), DEFAULT_CTM_CHAT_TOKEN),
        "weather_city": first_non_empty(config.get("weather_city"), DEFAULT_OPS_WEATHER_CITY),
        "news_feed_url": first_non_empty(config.get("news_feed_url"), DEFAULT_OPS_NEWS_FEED_URL),
    }


def login_session_ready():
    config = get_runtime_config()
    has_identity = bool(
        session.get("email") and session.get("first_name") and session.get("last_name")
    )
    return bool(has_identity and oauth_enabled_session() and config["account_id"])


def missing_ctm_config():
    config = get_runtime_config()
    missing = []
    if not oauth_enabled_session():
        missing.append("CTM OAuth session")
    if not config["account_id"]:
        missing.append("CTM_ACCOUNT_ID")
    return missing


def config_error_message():
    missing = missing_ctm_config()
    if not missing:
        return None
    return "Missing CTM configuration: " + ", ".join(missing)


def resolve_chat_token(account_id, configured_token):
    token = (configured_token or "").strip()
    if token:
        return token
    return DEFAULT_CHAT_TOKENS.get(str(account_id or "").strip(), "")


def ctm_get(path, params=None):
    config_error = config_error_message()
    if config_error:
        raise RuntimeError(config_error)
    # All data access uses the OAuth Bearer token.
    return oauth_api_get(path, params)


def extract_collection(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    for key in ("calls", "messages", "texts", "activities", "items", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_collection(value)
            if nested:
                return nested
    return []


def extract_mapping(value):
    return value if isinstance(value, dict) else {}


def first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if value:
                return value
            continue
        if value == 0:
            return value
        if value:
            return value
    return ""


def normalize_account_id_input(value):
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"/accounts/(\d+)", text, re.IGNORECASE) or re.fullmatch(r"(\d+)", text)
    if match:
        return match.group(1)
    return text


def pick_number(*values):
    for value in values:
        if not value:
            continue
        if isinstance(value, (int, float)):
            value = str(int(value))
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return ""


def parse_datetime(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).astimezone(APP_TIMEZONE)

    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    for fmt in (
        "%Y-%m-%d %I:%M %p %z",
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M %z",
        "%Y-%m-%d %I:%M:%S %p %z",
    ):
        try:
            return datetime.strptime(text, fmt).astimezone(APP_TIMEZONE)
        except ValueError:
            continue

    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=APP_TIMEZONE)
            return dt.astimezone(APP_TIMEZONE)
        except ValueError:
            continue
    return None


def format_datetime_parts(value):
    dt = parse_datetime(value)
    if not dt:
        return "Unknown time", "No date"
    return dt.strftime("%#I:%M %p"), dt.strftime("%b %#d")


def datetime_sort_value(value):
    dt = parse_datetime(value)
    if not dt:
        return 0
    return int(dt.timestamp())


def format_duration(value):
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return "—"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def summarize_direction(raw_direction, default_outbound_label="Outbound"):
    direction = str(raw_direction or "").lower()
    if "chat" in direction:
        return "Chat", "chat"
    if any(word in direction for word in ("inbound", "incoming", "received")):
        return "Inbound", "inbound"
    if any(word in direction for word in ("outbound", "outgoing", "sent")):
        return default_outbound_label, "outbound"
    if "sms" in direction or "message" in direction or "text" in direction:
        return "Text", "text"
    return default_outbound_label, "outbound"


def summarize_call_status(raw_status):
    status = str(raw_status or "").strip()
    lowered = status.lower()
    if "answer" in lowered and "no" not in lowered:
        return "Answered", "answered"
    if "miss" in lowered or "abandon" in lowered:
        return "Missed", "missed"
    if "busy" in lowered:
        return "Busy", "busy"
    if "voicemail" in lowered:
        return "Voicemail", "other"
    if "fail" in lowered:
        return "Failed", "other"
    return status.title() if status else "Unknown", "other"


def fetch_collection(paths, params=None):
    last_exc = None
    for path in paths:
        try:
            payload = ctm_get(path, params)
            records = extract_collection(payload)
            if records:
                return payload, records, path
            if path == paths[-1]:
                return payload, records, path
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    return {}, [], paths[-1]


def normalize_calls(records):
    normalized = []
    for raw in records:
        record = extract_mapping(raw)
        if not record:
            continue

        caller = extract_mapping(record.get("caller"))
        contact = extract_mapping(record.get("contact"))
        agent_obj = extract_mapping(record.get("agent"))

        direction_label, direction_key = summarize_direction(
            first_non_empty(record.get("direction"), record.get("call_type"), record.get("type")),
        )
        if direction_key in {"chat", "text"} or str(record.get("direction") or "").lower().startswith("msg_"):
            continue
        status_label, status_key = summarize_call_status(record.get("status"))

        caller_number = pick_number(
            record.get("caller_number"),
            record.get("caller_phone"),
            caller.get("number"),
            caller.get("phone_number"),
            contact.get("phone_number"),
            record.get("phone_number"),
        )
        dial_number = pick_number(
            record.get("to_number") if direction_key == "outbound" else "",
            caller_number,
            contact.get("phone_number"),
            record.get("phone_number"),
        )
        caller_name = first_non_empty(
            record.get("caller_name"),
            caller.get("name"),
            contact.get("name"),
            contact.get("display_name"),
            caller_number,
            "Unknown caller",
        )
        agent_name = first_non_empty(
            agent_obj.get("name"),
            record.get("agent_name"),
            record.get("answered_by"),
            record.get("user_name"),
        )
        summary = first_non_empty(
            record.get("summary"),
            record.get("ai_summary"),
            record.get("notes"),
            record.get("note"),
            record.get("description"),
        )
        source = first_non_empty(
            record.get("source_name"),
            record.get("utm_source"),
            record.get("source"),
            contact.get("source"),
        )
        raw_time = first_non_empty(record.get("called_at"), record.get("created_at"), record.get("started_at"))
        time_label, date_label = format_datetime_parts(raw_time)
        duration_label = format_duration(first_non_empty(record.get("duration"), record.get("talk_time"), 0))
        callback_priority = 0
        if status_key == "missed":
            callback_priority = 3
        elif direction_key == "inbound":
            callback_priority = 2
        elif direction_key == "outbound":
            callback_priority = 1
        signals = [direction_key, status_key]
        if not summary:
            signals.append("summary-missing")
        if not agent_name:
            signals.append("agent-missing")
        if callback_priority >= 2:
            signals.append("priority-callback")
        search_text = " ".join(
            str(part).lower()
            for part in (
                caller_name,
                caller_number,
                agent_name,
                summary,
                source,
                direction_label,
                status_label,
            )
            if part
        )

        normalized.append(
            {
                "time_label": time_label,
                "date_label": date_label,
                "caller_name": caller_name,
                "caller_number": caller_number,
                "agent_name": agent_name,
                "summary": summary,
                "source": source,
                "direction_label": direction_label,
                "direction_key": direction_key,
                "status_label": status_label,
                "status_key": status_key,
                "duration_label": duration_label,
                "dial_number": dial_number,
                "record_id": str(first_non_empty(record.get("id"), record.get("sid"), uuid.uuid4())),
                "sort_ts": datetime_sort_value(raw_time),
                "callback_priority": callback_priority,
                "signals": signals,
                "search_text": search_text,
            }
        )
    return normalized


def normalize_messages(records):
    normalized = []
    for raw in records:
        record = extract_mapping(raw)
        if not record:
            continue

        message_obj = extract_mapping(record.get("message"))
        contact = extract_mapping(record.get("contact"))
        raw_type = first_non_empty(
            record.get("type"),
            record.get("direction"),
            record.get("message_type"),
            record.get("activity_type"),
            record.get("channel"),
            message_obj.get("type"),
        )
        lowered_type = str(raw_type).lower()
        if lowered_type and not any(token in lowered_type for token in ("sms", "text", "message", "mms", "inbound", "outbound", "sent", "received")):
            continue

        body = first_non_empty(
            record.get("message_body"),
            record.get("body"),
            record.get("message"),
            record.get("content"),
            record.get("text"),
            message_obj.get("body"),
            message_obj.get("content"),
        )
        from_number = pick_number(
            record.get("from_number"),
            record.get("sender"),
            record.get("from"),
            message_obj.get("from_number"),
            message_obj.get("sender"),
            record.get("caller_number"),
            contact.get("phone_number"),
        )
        to_number = pick_number(
            record.get("to_number"),
            record.get("recipient"),
            record.get("to"),
            message_obj.get("to_number"),
            message_obj.get("recipient"),
            record.get("tracking_number"),
            record.get("receiving_number"),
        )
        direction_label, direction_key = summarize_direction(raw_type, default_outbound_label="Outbound")
        if direction_key == "outbound":
            reply_number = to_number or from_number
        else:
            reply_number = from_number or to_number

        contact_name = first_non_empty(
            record.get("contact_name"),
            record.get("name"),
            contact.get("name"),
            contact.get("display_name"),
        )
        status = first_non_empty(record.get("status"), message_obj.get("status"))
        raw_time = first_non_empty(record.get("created_at"), record.get("sent_at"), record.get("updated_at"), record.get("called_at"))
        time_label, date_label = format_datetime_parts(raw_time)
        callback_priority = 2 if direction_key == "inbound" else 1
        signals = [direction_key]
        if not contact_name:
            signals.append("contact-missing")
        if record.get("message_media"):
            signals.append("media")
        if direction_key == "inbound":
            signals.append("priority-reply")
        search_text = " ".join(
            str(part).lower()
            for part in (
                contact_name,
                from_number,
                to_number,
                body,
                status,
                direction_label,
            )
            if part
        )

        normalized.append(
            {
                "time_label": time_label,
                "date_label": date_label,
                "from_number": from_number or "—",
                "to_number": to_number or "—",
                "contact_name": contact_name,
                "body": body,
                "status": status,
                "direction_label": direction_label,
                "direction_key": direction_key,
                "reply_number": reply_number,
                "has_media": bool(record.get("message_media")),
                "record_id": str(first_non_empty(record.get("id"), record.get("message_id"), record.get("sid"), uuid.uuid4())),
                "sort_ts": datetime_sort_value(raw_time),
                "callback_priority": callback_priority,
                "signals": signals,
                "search_text": search_text,
            }
        )
    return normalized


def call_metrics(calls):
    return {
        "Recent Calls": len(calls),
        "Answered": sum(1 for call in calls if call["status_key"] == "answered"),
        "Missed": sum(1 for call in calls if call["status_key"] == "missed"),
        "Outbound": sum(1 for call in calls if call["direction_key"] == "outbound"),
    }


def message_metrics(messages):
    return {
        "Recent Texts": len(messages),
        "Inbound": sum(1 for msg in messages if msg["direction_key"] == "inbound"),
        "Outbound": sum(1 for msg in messages if msg["direction_key"] == "outbound"),
        "With Content": sum(1 for msg in messages if msg["body"]),
    }


def call_focus_cards(calls):
    return [
        {
            "label": "Missed Callbacks",
            "count": sum(1 for call in calls if "missed" in call["signals"]),
            "hint": "Most urgent return calls first.",
            "filter": "missed",
            "sort": "callback",
            "signals": "missed",
        },
        {
            "label": "Unassigned Inbound",
            "count": sum(1 for call in calls if {"inbound", "agent-missing"}.issubset(set(call["signals"]))),
            "hint": "Inbound calls without an assigned agent.",
            "filter": "inbound",
            "sort": "callback",
            "signals": "inbound agent-missing",
        },
        {
            "label": "Needs Notes",
            "count": sum(1 for call in calls if "summary-missing" in call["signals"]),
            "hint": "Calls missing a usable summary.",
            "filter": "all",
            "sort": "newest",
            "signals": "summary-missing",
        },
    ]


def message_focus_cards(messages):
    return [
        {
            "label": "Reply Queue",
            "count": sum(1 for message in messages if "priority-reply" in message["signals"]),
            "hint": "Inbound texts needing follow-up.",
            "filter": "inbound",
            "sort": "callback",
            "signals": "inbound",
        },
        {
            "label": "Media Follow-up",
            "count": sum(1 for message in messages if "media" in message["signals"]),
            "hint": "Texts with attachments or images.",
            "filter": "all",
            "sort": "newest",
            "signals": "media",
        },
        {
            "label": "Unknown Contacts",
            "count": sum(1 for message in messages if "contact-missing" in message["signals"]),
            "hint": "Texts without a mapped contact name.",
            "filter": "all",
            "sort": "callback",
            "signals": "contact-missing",
        },
    ]


def humanize_key(key):
    """company_name -> Company Name."""
    return str(key or "").replace("_", " ").replace("-", " ").strip().title() or "Field"


def normalize_forms(records):
    """Turn CTM form-submission activities (direction=form) into display rows.

    The CTM activity carries a `form` object holding the submitted field
    names and values, e.g. name/email and custom[{label,value,id}] entries.
    """
    forms = []
    routing_keys = {"number_to_call", "customer_number_to_dial", "trackback_id", "form_id"}
    for record in records:
        payload = extract_mapping(record.get("form")) or {}

        meta_fields = []
        user_fields = []
        for key, value in payload.items():
            if key in ("custom", "form_name"):
                continue
            if value in (None, "", [], {}):
                continue
            entry = {"label": humanize_key(key), "value": str(value), "id": key}
            if key in routing_keys:
                meta_fields.append(entry)
            else:
                user_fields.append(entry)

        custom = payload.get("custom")
        if isinstance(custom, list):
            for item in custom:
                item_map = extract_mapping(item) or {}
                value = item_map.get("value")
                if value in (None, ""):
                    continue
                user_fields.append({
                    "label": str(item_map.get("label") or humanize_key(item_map.get("id") or "field")),
                    "value": str(value),
                    "id": str(item_map.get("id") or ""),
                })

        form_name = first_non_empty(payload.get("form_name"), record.get("form_name")) or "Form submission"
        number = first_non_empty(
            payload.get("customer_number_to_dial"),
            record.get("customer_number"),
            record.get("caller_number"),
            record.get("from"),
        )
        dial_number = pick_number(
            payload.get("customer_number_to_dial"),
            record.get("customer_number"),
            record.get("caller_number"),
            record.get("from"),
        )
        contact = first_non_empty(record.get("contact_name"), record.get("name")) or "Unknown contact"

        created_value = first_non_empty(record.get("created_at"), record.get("start_time"), record.get("time"))
        date_label, time_label = format_datetime_parts(created_value)
        created_dt = parse_datetime(created_value)

        signals = []
        if not any(field["id"] == "email" for field in user_fields):
            signals.append("no-email")
        if not user_fields:
            signals.append("no-fields")

        forms.append({
            "id": first_non_empty(record.get("id"), record.get("activity_id")),
            "form_name": str(form_name).strip(),
            "contact": contact,
            "number": number or "",
            "dial_number": dial_number or "",
            "date": date_label,
            "time": time_label,
            "sort": datetime_sort_value(created_value),
            "created_dt": created_dt,
            "fields": user_fields,
            "meta": meta_fields,
            "field_count": len(user_fields),
            "signals": signals,
            "created": created_value or "",
        })

    forms.sort(key=lambda form: form["sort"], reverse=True)
    return forms


def form_metrics(forms):
    today = datetime.now(timezone.utc).date()
    return {
        "total": len(forms),
        "today": sum(1 for form in forms if form["created_dt"] and form["created_dt"].date() == today),
        "with_email": sum(1 for form in forms if "no-email" not in form["signals"]),
        "empty": sum(1 for form in forms if "no-fields" in form["signals"]),
    }


def form_focus_cards(forms):
    today = datetime.now(timezone.utc).date()
    return [
        {
            "label": "Submissions Today",
            "count": sum(1 for form in forms if form["created_dt"] and form["created_dt"].date() == today),
            "hint": "Form captures received since midnight.",
            "filter": "all",
            "sort": "newest",
            "signals": "today",
        },
        {
            "label": "Missing Email",
            "count": sum(1 for form in forms if "no-email" in form["signals"]),
            "hint": "Submissions with no email address field.",
            "filter": "all",
            "sort": "newest",
            "signals": "no-email",
        },
        {
            "label": "No Field Data",
            "count": sum(1 for form in forms if "no-fields" in form["signals"]),
            "hint": "Submissions that carried no user-entered values.",
            "filter": "all",
            "sort": "newest",
            "signals": "no-fields",
        },
    ]


def status_duration_label(status):
    stats = status.get("stats") or []
    if not stats:
        return "Just updated"
    entry = stats[0] or {}
    minutes = int(entry.get("min") or 0)
    seconds = int(entry.get("sec") or 0)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def normalize_agent(agent):
    status = extract_mapping(agent.get("status"))
    value = str(status.get("value") or "offline").lower()
    accepting = bool(status.get("accept"))
    logged_out = bool(status.get("logged_out"))
    queue_total = int(status.get("queue_total") or 0)
    chatting = int(status.get("chatting") or 0)

    availability = "Offline"
    availability_key = "offline"
    if value == "online" and accepting and not logged_out:
        availability = "Available"
        availability_key = "available"
    elif value in {"outbound", "inbound", "call", "busy"} or queue_total > 0:
        availability = "On Call"
        availability_key = "busy"
    elif chatting > 0 or value == "chat":
        availability = "Chatting"
        availability_key = "chat"
    elif logged_out or value == "offline":
        availability = "Offline"
        availability_key = "offline"
    else:
        availability = value.title() if value else "Offline"
        availability_key = "offline"

    return {
        "name": first_non_empty(agent.get("name"), agent.get("email"), "Unknown agent"),
        "initials": first_non_empty(agent.get("initials"), "?"),
        "email": agent.get("email") or "",
        "availability": availability,
        "availability_key": availability_key,
        "queue_total": queue_total,
        "duration_label": status_duration_label(status),
        "accepting": accepting,
    }


def agent_metrics(agents):
    return {
        "Available": sum(1 for agent in agents if agent["availability_key"] == "available"),
        "Busy": sum(1 for agent in agents if agent["availability_key"] == "busy"),
        "Chatting": sum(1 for agent in agents if agent["availability_key"] == "chat"),
        "Offline": sum(1 for agent in agents if agent["availability_key"] == "offline"),
    }


def fetch_agents():
    payload = ctm_get(f"/accounts/{get_runtime_config()['account_id']}/agents/history.json", {"bypass": "cache"})
    agents = [normalize_agent(agent) for agent in payload.get("agents", [])]
    agents.sort(key=lambda agent: (agent["availability_key"] != "available", agent["availability_key"] == "offline", agent["name"].lower()))
    return agents


def weather_code_label(code):
    try:
        return WEATHER_CODE_LABELS.get(int(code), "Current conditions")
    except (TypeError, ValueError):
        return "Current conditions"


def fetch_weather_snapshot(latitude, longitude, *, location_name, timezone_name="auto"):
    forecast = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,weather_code,wind_speed_10m,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "timezone": timezone_name or "auto",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "forecast_days": 1,
        },
        timeout=10,
    )
    forecast.raise_for_status()
    payload = forecast.json()
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    temperature = current.get("temperature_2m")
    if temperature is None:
        return None
    return {
        "location": location_name,
        "temperature": round(float(temperature)),
        "condition": weather_code_label(current.get("weather_code")),
        "wind_speed": round(float(current.get("wind_speed_10m") or 0)),
        "high": round(float((daily.get("temperature_2m_max") or [temperature])[0])),
        "low": round(float((daily.get("temperature_2m_min") or [temperature])[0])),
        "is_day": bool(current.get("is_day", 1)),
        "observed_at": current.get("time") or "",
    }


def geocode_weather_city(weather_city):
    if not weather_city:
        return None
    geocode = requests.get(
        f"https://geocoding-api.open-meteo.com/v1/search?name={quote_plus(weather_city)}&count=1&language=en&format=json",
        timeout=10,
    )
    geocode.raise_for_status()
    results = geocode.json().get("results") or []
    if not results:
        return None
    place = results[0]
    return {
        "location": ", ".join(part for part in [place.get("name"), place.get("admin1")] if part),
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "timezone": place.get("timezone") or "auto",
    }


def fetch_weather_brief():
    weather_city = get_runtime_config()["weather_city"]
    if not weather_city:
        return None
    try:
        place = geocode_weather_city(weather_city)
        if not place:
            return None
        snapshot = fetch_weather_snapshot(
            place["latitude"],
            place["longitude"],
            location_name=place["location"],
            timezone_name=place["timezone"],
        )
        if not snapshot:
            return None
        return {
            "label": f"Weather {snapshot['location']}",
            "detail": f"{snapshot['temperature']}F, {snapshot['condition']}, wind {snapshot['wind_speed']} mph",
        }
    except Exception:
        return None


def fetch_local_weather_snapshot():
    try:
        location_response = requests.get("https://ipwho.is/", timeout=10)
        location_response.raise_for_status()
        location = location_response.json()
        if location.get("success"):
            snapshot = fetch_weather_snapshot(
                location.get("latitude"),
                location.get("longitude"),
                location_name=", ".join(
                    part for part in [location.get("city"), location.get("region")] if part
                ) or (location.get("country") or "Your area"),
                timezone_name=((location.get("timezone") or {}).get("id") if isinstance(location.get("timezone"), dict) else None) or "auto",
            )
            if snapshot:
                snapshot["source"] = "ip"
                return snapshot
    except Exception:
        pass

    weather_city = get_runtime_config()["weather_city"]
    if not weather_city:
        return None
    try:
        place = geocode_weather_city(weather_city)
        if not place:
            return None
        snapshot = fetch_weather_snapshot(
            place["latitude"],
            place["longitude"],
            location_name=place["location"],
            timezone_name=place["timezone"],
        )
        if snapshot:
            snapshot["source"] = "fallback"
        return snapshot
    except Exception:
        return None


# Multi-source "News Feed". Each source is tagged with a category so the UI
# can filter (All / Tech / AI & SaaS / Music / Culture / World).
NEWS_SOURCES = [
    {"key": "tech",    "label": "Tech",      "url": "https://techcrunch.com/feed/"},
    {"key": "tech",    "label": "Tech",      "url": "https://www.theverge.com/rss/index.xml"},
    {"key": "hacker",  "label": "Hacker News", "url": "https://hnrss.org/frontpage"},
    {"key": "music",   "label": "Music",     "url": "https://pitchfork.com/feed/feed-news/rss"},
    {"key": "music",   "label": "Music",     "url": "https://www.stereogum.com/feed/"},
    {"key": "culture", "label": "Culture",   "url": "https://variety.com/feed/"},
    {"key": "world",   "label": "World",     "url": "https://feeds.npr.org/1001/rss.xml"},
]

NEWS_CATEGORIES = [
    {"key": "all",     "label": "All"},
    {"key": "tech",    "label": "Tech"},
    {"key": "hacker",  "label": "HN"},
    {"key": "music",   "label": "Music"},
    {"key": "culture", "label": "Culture"},
    {"key": "world",   "label": "World"},
]


def _parse_news_feed(news_feed_url, source_label="News", source_key="news", limit=20, lookback_hours=96):
    """Parse an RSS or Atom feed into normalized news items."""
    if not news_feed_url:
        return []
    try:
        response = requests.get(
            news_feed_url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CTM-Softphone-Demo/1.0)"},
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
    except Exception:
        return []

    cutoff = datetime.now(APP_TIMEZONE) - timedelta(hours=lookback_hours)

    def text_of(node, *names):
        for name in names:
            found = node.findtext(name)
            if found and found.strip():
                return found.strip()
        return ""

    def link_of(node):
        atom_link = "{http://www.w3.org/2005/Atom}link"
        link = text_of(node, "link", atom_link)
        if link:
            return link
        # Atom: <link href="..."> (the element is namespaced in Atom feeds)
        for child in list(node.findall("link")) + list(node.findall(atom_link)):
            href = child.get("href")
            if href and child.get("rel", "alternate") in ("alternate", ""):
                return href.strip()
        # Fallback: some feeds only carry the story URL in <guid>/<id>.
        guid = text_of(node, "guid", "{http://www.w3.org/2005/Atom}id")
        if guid.startswith("http"):
            return guid
        return ""

    entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry") or root.findall(".//entry")

    parsed_items = []
    for entry in entries:
        title = text_of(entry, "title", "{http://www.w3.org/2005/Atom}title")
        if not title:
            continue
        pub_date = text_of(
            entry,
            "pubDate", "published", "updated",
            "{http://www.w3.org/2005/Atom}published",
            "{http://www.w3.org/2005/Atom}updated",
        )
        try:
            published_dt = parsedate_to_datetime(pub_date).astimezone(APP_TIMEZONE)
        except Exception:
            published_dt = None
        parsed_items.append((published_dt, html.unescape(title), link_of(entry)))

    def format_item(published_dt, title, link):
        if published_dt:
            if published_dt.date() == datetime.now(APP_TIMEZONE).date():
                published = published_dt.strftime("%#I:%M %p")
            else:
                published = published_dt.strftime("%a %#I:%M %p")
        else:
            published = "Latest"
        return {
            "label": published,
            "detail": title,
            "link": link,
            "source": source_label,
            "category": source_key,
            "sort_dt": published_dt or datetime.now(APP_TIMEZONE),
        }

    recent_items = [
        format_item(published_dt, title, link)
        for published_dt, title, link in parsed_items
        if published_dt is None or published_dt >= cutoff
    ][:limit]
    if recent_items:
        return recent_items
    return [format_item(published_dt, title, link) for published_dt, title, link in parsed_items[:limit]]


def fetch_news_briefs(limit=30, lookback_hours=96):
    """Aggregate headlines from every configured source, newest first."""
    sources = list(NEWS_SOURCES)
    configured_feed_url = get_runtime_config().get("news_feed_url")
    if configured_feed_url and configured_feed_url not in {s["url"] for s in sources}:
        sources.insert(0, {"key": "custom", "label": "Custom", "url": configured_feed_url})

    collected = []
    with ThreadPoolExecutor(max_workers=min(8, len(sources) or 1)) as pool:
        futures = [
            pool.submit(
                _parse_news_feed,
                source["url"],
                source.get("label", "News"),
                source.get("key", "news"),
                limit,
                lookback_hours,
            )
            for source in sources
        ]
        for future in futures:
            try:
                collected.extend(future.result())
            except Exception:
                continue

    # De-duplicate by headline, then sort newest-first.
    seen = set()
    unique = []
    for item in collected:
        headline = item["detail"].strip().lower()
        if headline and headline not in seen:
            seen.add(headline)
            unique.append(item)
    unique.sort(key=lambda it: it["sort_dt"], reverse=True)
    return unique[:limit]


def build_news_items():
    items = fetch_news_briefs()
    if not items:
        items.append({
            "label": "Latest",
            "detail": "No recent stories are available from the configured feeds right now.",
            "source": "News",
            "category": "news",
        })
    return items


# ── ESPN sports ticker ───────────────────────────────────────────────────────

ESPN_LEAGUES = [
    {"key": "mlb",   "label": "MLB",    "path": "baseball/mlb"},
    {"key": "nfl",   "label": "NFL",    "path": "football/nfl"},
    {"key": "nba",   "label": "NBA",    "path": "basketball/nba"},
    {"key": "wnba",  "label": "WNBA",   "path": "basketball/wnba"},
    {"key": "ncaam", "label": "NCAAM",  "path": "basketball/mens-college-basketball"},
    {"key": "ncaaw", "label": "NCAAW",  "path": "basketball/womens-college-basketball"},
    {"key": "nhl",   "label": "NHL",    "path": "hockey/nhl"},
]
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"


def fetch_sports_scores():
    """Fetch current/next scores for MLB, NFL, NBA and NCAA Men's Basketball.

    Uses the public ESPN scoreboard API (no key required). Returns a list of
    {league, label, away, home, away_score, home_score, status, state, short}.
    """
    games = []
    for league in ESPN_LEAGUES:
        try:
            response = requests.get(
                ESPN_SCOREBOARD.format(path=league["path"]),
                params={"limit": 12},
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            app.logger.warning("ESPN %s fetch failed: %s", league["key"], exc)
            continue

        for event in payload.get("events", [])[:6]:
            competition = (event.get("competitions") or [{}])[0]
            competitors = competition.get("competitors") or []
            if len(competitors) < 2:
                continue

            home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
            away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
            status = event.get("status", {})
            status_type = status.get("type", {})

            def abbr(side):
                team = side.get("team") or {}
                return first_non_empty(
                    team.get("abbreviation"), team.get("shortDisplayName"), team.get("name")
                )

            games.append({
                "league": league["label"],
                "key": league["key"],
                "away": abbr(away),
                "home": abbr(home),
                "away_score": str(away.get("score", "")),
                "home_score": str(home.get("score", "")),
                "status": status_type.get("shortDetail") or status_type.get("description") or "",
                "state": status_type.get("state") or "pre",  # pre | in | post
                "short": status.get("type", {}).get("shortDetail", ""),
                "time": event.get("date", ""),
            })
    return games


# Top markets shown in the markets ticker (Yahoo Finance chart API, no key).
MARKET_SYMBOLS = [
    {"key": "dji",  "label": "DOW",     "symbol": "^DJI"},
    {"key": "spx",  "label": "S&P 500", "symbol": "^GSPC"},
    {"key": "ixic", "label": "NASDAQ",  "symbol": "^IXIC"},
    {"key": "rut",  "label": "RUSSELL", "symbol": "^RUT"},
    {"key": "vix",  "label": "VIX",     "symbol": "^VIX"},
    {"key": "gold", "label": "GOLD",    "symbol": "GC=F"},
    {"key": "oil",  "label": "CRUDE",   "symbol": "CL=F"},
    {"key": "btc",  "label": "BITCOIN", "symbol": "BTC-USD"},
    {"key": "eth",  "label": "ETHEREUM", "symbol": "ETH-USD"},
    {"key": "usd",  "label": "USD/EUR", "symbol": "EURUSD=X"},
    {"key": "tnx",  "label": "10Y YIELD", "symbol": "^TNX"},
]

MARKET_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def fetch_market_quotes():
    """Fetch top-market quotes from the public Yahoo Finance chart API.

    No API key required. Returns a list of
    {label, symbol, price, change, change_pct, direction, currency}.
    """
    quotes = []
    for market in MARKET_SYMBOLS:
        try:
            response = requests.get(
                MARKET_CHART.format(symbol=requests.utils.quote(market["symbol"])),
                params={"interval": "1d", "range": "1d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()
            meta = payload["chart"]["result"][0]["meta"]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            app.logger.warning("Market %s fetch failed: %s", market["key"], exc)
            continue

        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None:
            continue

        change = change_pct = None
        if prev:
            change = price - prev
            change_pct = (change / prev) * 100

        quotes.append({
            "key": market["key"],
            "label": market["label"],
            "symbol": market["symbol"],
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "direction": "up" if (change or 0) >= 0 else "down",
            "currency": meta.get("currency", ""),
        })
    return quotes


# ── Pages ────────────────────────────────────────────────────────────────────

@app.get("/oauth/whoami")
@login_required
def oauth_whoami():
    """Diagnostic: show the OAuth token's scopes and the phone_access outcome.

    Open this in the browser while logged in to confirm whether the token carries
    the 'manage' scope required by the phone embed.
    """
    info = describe_access_token()
    info["auth_mode"] = session.get("auth_mode")
    info["account_id"] = get_runtime_config().get("account_id")
    info["phone_access_bearer_status"] = None

    if oauth_enabled_session():
        try:
            r = requests.post(
                f"{CTM_BASE}/accounts/{info['account_id']}/phone_access",
                json={
                    "email": session.get("email"),
                    "first_name": session.get("first_name"),
                    "last_name": session.get("last_name"),
                    "session_id": session.get("session_id"),
                },
                headers={"Authorization": f"Bearer {current_access_token()}"},
                timeout=30,
            )
            info["phone_access_bearer_status"] = r.status_code
            info["phone_access_bearer_body"] = r.text[:300]
        except requests.RequestException as exc:
            info["phone_access_bearer_error"] = str(exc)

    return Response(json.dumps(info, indent=2, default=str), mimetype="application/json")


@app.get("/login")
def login():
    _auto_login()
    if login_session_ready():
        return redirect(url_for("index"))
    return render_template(
        "login.html",
        error=request.args.get("error"),
        oauth_available=oauth_configured(),
    )


@app.get("/oauth/login")
def oauth_login():
    """Start the CTM OAuth2 web (authorization-code) flow."""
    if not oauth_configured():
        return redirect(url_for("login", error="OAuth is not configured on this machine."))
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    session["auth_mode"] = "oauth"
    return redirect(build_authorize_url(state))


@app.get("/oauth/callback")
def oauth_callback():
    """Handle the CTM redirect back with an authorization code."""
    if request.args.get("error"):
        reason = request.args.get("error_description") or request.args["error"]
        return redirect(url_for("login", error=f"CTM authorization failed: {reason}"))

    state = request.args.get("state", "")
    if not state or state != session.get("oauth_state"):
        return redirect(url_for("login", error="OAuth state mismatch. Please try again."))
    session.pop("oauth_state", None)

    code = request.args.get("code", "")
    if not code:
        return redirect(url_for("login", error="CTM did not return an authorization code."))

    try:
        payload = oauth_token_request(
            "authorization_code",
            grant_type="authorization_code",
            code=code,
            redirect_uri=get_oauth_redirect_uri(),
        )
    except (requests.RequestException, RuntimeError) as exc:
        app.logger.error("CTM OAuth token exchange failed: %s", exc)
        return redirect(url_for("login", error="CTM token exchange failed. Please try again."))

    session.clear()
    session["auth_mode"] = "oauth"
    store_oauth_token(payload)

    identity = resolve_oauth_identity() or {}
    session["oauth_account_id"] = identity.get("account_id", "")
    session["email"] = identity.get("email", "")
    session["first_name"] = identity.get("first_name", "")
    session["last_name"] = identity.get("last_name", "")
    session["session_id"] = str(uuid.uuid4())

    # If we could not fully resolve identity, ask for the missing pieces once.
    if login_session_ready():
        return redirect(url_for("index"))
    return render_template(
        "oauth_complete.html",
        error=None,
        email=session.get("email", ""),
        first_name=session.get("first_name", ""),
        last_name=session.get("last_name", ""),
        account_id=session.get("oauth_account_id", ""),
    )


@app.post("/oauth/complete")
def oauth_complete():
    """Finish OAuth setup when the API could not supply the full identity."""
    if not oauth_enabled_session():
        return redirect(url_for("login"))

    email      = request.form.get("email", "").strip()
    first_name = request.form.get("first_name", "").strip()
    last_name  = request.form.get("last_name", "").strip()
    account_id = normalize_account_id_input(request.form.get("account_id", ""))

    if not all([email, first_name, last_name, account_id]):
        return render_template(
            "oauth_complete.html",
            error="Email, first name, last name, and account ID are all required.",
            email=email,
            first_name=first_name,
            last_name=last_name,
            account_id=account_id,
        )

    session["email"] = email
    session["first_name"] = first_name
    session["last_name"] = last_name
    session["oauth_account_id"] = account_id
    session["session_id"] = session.get("session_id") or str(uuid.uuid4())
    return redirect(url_for("index"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.get("/")
@login_required
def index():
    config = get_runtime_config()
    return render_template(
        "phone.html",
        user_name=f"{session['first_name']} {session['last_name']}",
        account_id=config["account_id"],
        chat_token=resolve_chat_token(config["account_id"], config["chat_token"]),
    )


# ── Phone embed auth ─────────────────────────────────────────────────────────

@app.post("/ctm-phone-access")
@login_required
def ctm_phone_access():
    config_error = config_error_message()
    if config_error:
        return jsonify({"status": "error", "message": config_error}), 400
    config = get_runtime_config()

    if not oauth_enabled_session():
        return jsonify({
            "status": "error",
            "message": (
                "Launching the softphone requires an OAuth session. "
                "Reconfigure the demo and connect with CTM."
            ),
        }), 400

    payload = {
        "email":      session["email"],
        "first_name": session["first_name"],
        "last_name":  session["last_name"],
        "session_id": session["session_id"],
    }
    url = f"{CTM_BASE}/accounts/{config['account_id']}/phone_access"
    try:
        # phone_access accepts an OAuth2 Bearer token, but only with the
        # 'manage' scope (it isn't in any scoped endpoint list).
        r = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {current_access_token()}"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as exc:
        app.logger.error("CTM phone_access error: %s", exc)
        if isinstance(exc, requests.Timeout):
            message = (
                "CTM timed out while starting the phone session. This is usually "
                "temporary \u2014 try again in a moment."
            )
        elif getattr(getattr(exc, "response", None), "status_code", None) == 403:
            message = (
                "CTM denied the phone session (403). The OAuth token needs the "
                "'manage' scope, and the authorizing user must still be an "
                "owner/admin of the account. Reconfigure the demo to re-authorize."
            )
        else:
            message = str(exc)
        return jsonify({"status": "error", "message": message}), 502

    data.update({
        "sessionId":  session["session_id"],
        "email":      session["email"],
        "first_name": session["first_name"],
        "last_name":  session["last_name"],
        "session_id": session["session_id"],
    })
    return jsonify(data)


@app.post("/ctm-phone-refresh")
@login_required
def ctm_phone_refresh():
    return ctm_phone_access()


@app.get("/ctm-phone-status")
@login_required
def ctm_phone_status():
    """Report whether the session can obtain a phone token, without calling
    phone_access (which would compete with the embed's own request)."""
    config_error = config_error_message()
    if config_error:
        return jsonify({"ok": False, "message": config_error})
    if not oauth_enabled_session():
        return jsonify({
            "ok": False,
            "message": "Connect with CTM (OAuth) to launch the softphone.",
        })
    return jsonify({"ok": True})


# ── Data proxies ─────────────────────────────────────────────────────────────

@app.get("/ctm/calls")
@login_required
def ctm_calls():
    config = get_runtime_config()
    try:
        _, records, _ = fetch_collection([f"/accounts/{config['account_id']}/calls.json"], {"per_page": 75})
        calls = normalize_calls(records)
        error = None
    except Exception as exc:
        calls, error = [], str(exc)
    return render_template(
        "data/calls.html",
        calls=calls,
        error=error,
        metrics=call_metrics(calls),
        focus_cards=call_focus_cards(calls),
        updated_at=datetime.now().strftime("%#I:%M %p"),
    )


@app.get("/ctm/messages")
@login_required
def ctm_messages():
    config = get_runtime_config()
    try:
        _, records, _ = fetch_collection(
            [
                f"/accounts/{config['account_id']}/calls.json",
                f"/accounts/{config['account_id']}/messages.json",
                f"/accounts/{config['account_id']}/texts.json",
                f"/accounts/{config['account_id']}/activities.json",
            ],
            {
                "per_page": 75,
                "direction[]": ["msg_inbound", "msg_outbound"],
            },
        )
        messages = normalize_messages(records)
        error = None
    except Exception as exc:
        messages, error = [], str(exc)
    return render_template(
        "data/messages.html",
        messages=messages,
        error=error,
        metrics=message_metrics(messages),
        focus_cards=message_focus_cards(messages),
        updated_at=datetime.now().strftime("%#I:%M %p"),
    )


@app.get("/ctm/forms")
@login_required
def ctm_forms():
    config = get_runtime_config()
    try:
        _, records, _ = fetch_collection(
            [
                f"/accounts/{config['account_id']}/activities.json",
                f"/accounts/{config['account_id']}/calls.json",
            ],
            {
                "per_page": 75,
                "direction[]": ["form"],
            },
        )
        forms = normalize_forms(records)
        error = None
    except Exception as exc:
        forms, error = [], str(exc)
    return render_template(
        "data/forms.html",
        forms=forms,
        error=error,
        metrics=form_metrics(forms),
        focus_cards=form_focus_cards(forms),
        updated_at=datetime.now().strftime("%#I:%M %p"),
    )


@app.get("/ctm/phone-lookup")
@login_required
def ctm_phone_lookup():
    """Phone number validation via IPQualityScore (key kept server-side)."""
    raw = request.args.get("number", "").strip()
    country = request.args.get("country", "US").strip().upper() or "US"
    if not raw:
        return jsonify({"ok": False, "error": "Enter a phone number to look up."}), 400
    try:
        result = lookup_phone_number(raw, country=country)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200
    except requests.RequestException as exc:
        app.logger.warning("IPQS lookup failed: %s", exc)
        return jsonify({"ok": False, "error": "Lookup service is unavailable right now."}), 200
    return jsonify({"ok": True, "result": result})


@app.get("/ctm/ops-panel")
@login_required
def ctm_ops_panel():
    try:
        agents = fetch_agents()
        error = None
    except Exception as exc:
        agents = []
        error = str(exc)
    return render_template(
        "data/ops_panel.html",
        agents=agents,
        metrics=agent_metrics(agents),
        error=error,
        updated_at=datetime.now().strftime("%#I:%M %p"),
    )


@app.get("/ctm/ops-feed")
@login_required
def ctm_ops_feed():
    return render_template(
        "data/ops_feed.html",
        feed_items=build_news_items(),
        categories=NEWS_CATEGORIES,
        updated_at=datetime.now().strftime("%#I:%M %p"),
    )


@app.get("/ctm/sports-ticker")
@login_required
def ctm_sports_ticker():
    try:
        sports = fetch_sports_scores()
    except Exception as exc:
        app.logger.warning("Sports ticker unavailable: %s", exc)
        sports = []
    return render_template("data/sports_ticker.html", sports=sports, leagues=ESPN_LEAGUES)


@app.get("/ctm/markets-ticker")
@login_required
def ctm_markets_ticker():
    try:
        quotes = fetch_market_quotes()
    except Exception as exc:
        app.logger.warning("Markets ticker unavailable: %s", exc)
        quotes = []
    return render_template("data/markets_ticker.html", quotes=quotes)


@app.get("/ctm/local-weather")
@login_required
def ctm_local_weather():
    snapshot = fetch_local_weather_snapshot()
    if not snapshot:
        return jsonify({"status": "error", "message": "Weather unavailable"}), 502
    return jsonify({
        "status": "ok",
        "location": snapshot["location"],
        "temperature": snapshot["temperature"],
        "condition": snapshot["condition"],
        "wind_speed": snapshot["wind_speed"],
        "high": snapshot["high"],
        "low": snapshot["low"],
        "is_day": snapshot["is_day"],
        "observed_at": snapshot["observed_at"],
        "source": snapshot.get("source", "ip"),
    })


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 8080))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
