"""Login delegated to a real Jellyfin instance, plus the token gate.

The proxy has no user database of its own. A login is forwarded to JELLYFIN_URL
and, if Jellyfin accepts the credentials, we mint our own opaque token and
remember which Jellyfin user it belongs to (db.create_session). Jellyfin is then
out of the request path entirely - every later call is one indexed lookup.

Fails closed: if JELLYFIN_URL is unset, or Jellyfin is unreachable, or anything
about the response is unexpected, nobody gets in.
"""

import logging
import os
import re
import secrets
import threading
import time

import requests

import db

log = logging.getLogger(__name__)

# Reached over the LAN: the proxy and Jellyfin sit on the same network, so the
# public hostname doesn't resolve usefully from in here. Overridable, and
# setting JELLYFIN_URL to an empty string still makes every login fail closed.
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://192.168.1.78:30013").rstrip("/")
TOKEN_TTL_DAYS = int(os.environ.get("PROXY_TOKEN_TTL_DAYS", "30"))
LOGIN_TIMEOUT = float(os.environ.get("JELLYFIN_LOGIN_TIMEOUT", "10"))

# The auth endpoint proxies straight to a real Jellyfin, so without a limiter it
# is a password oracle against it. Count failures per client address.
MAX_FAILURES = int(os.environ.get("PROXY_LOGIN_MAX_FAILURES", "8"))
LOCKOUT_SECONDS = int(os.environ.get("PROXY_LOGIN_LOCKOUT_SECONDS", "300"))

_failure_lock = threading.Lock()
_failures: dict[str, list[float]] = {}

# Which addresses are allowed to tell us the real client IP. Behind cloudflared
# every remote request arrives from the tunnel process, so without this the
# rate limiter sees one address for the whole internet. Only these sources are
# believed - otherwise any LAN client could forge CF-Connecting-IP and poison
# the limiter.
TRUSTED_PROXY_IPS = frozenset(
    ip.strip() for ip in
    os.environ.get("TRUSTED_PROXY_IPS", "127.0.0.1,::1").split(",")
    if ip.strip()
)

# Endpoints reachable without a token. Keyed on Flask endpoint name rather than
# path so the lowercase route aliases are covered without listing them twice.
PUBLIC_ENDPOINTS = frozenset({
    "system_info_public",
    "ping",
    "authenticate",
    # Jellyfin marks image endpoints [AllowAnonymous] and clients rely on it -
    # art is fetched from views that never attach the token. Serving it needs
    # no user data: we look up a cover id and redirect to Tidal's public CDN.
    "item_image",
})

_BEARER_RE = re.compile(r'token\s*=\s*"?([^",]+)"?', re.IGNORECASE)

# Tokens travel in the query string because that is how Jellyfin clients do it,
# and both werkzeug and gunicorn log the full request line. Without this the
# access log becomes a store of working credentials.
_TOKEN_IN_QUERY = re.compile(r"((?:api_?key|token)=)[^&\s\"']+", re.IGNORECASE)


class RedactTokens(logging.Filter):
    """Replace token values in log records with a placeholder.

    Attach to the *handlers*, not the loggers: a filter on a logger doesn't see
    records propagated up from werkzeug or gunicorn.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_QUERY.sub(r"\1<redacted>", record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_IN_QUERY.sub(r"\1<redacted>", arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


def install_log_redaction() -> None:
    """Scrub tokens from every handler that log records can reach."""
    redactor = RedactTokens()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
    for name in ("werkzeug", "gunicorn.access", "gunicorn.error"):
        logger = logging.getLogger(name)
        logger.addFilter(redactor)
        for handler in logger.handlers:
            handler.addFilter(redactor)


def is_configured() -> bool:
    return bool(JELLYFIN_URL)


def client_ip(request) -> str:
    """The address to rate-limit on.

    Believes CF-Connecting-IP / X-Forwarded-For only when the request actually
    came from a trusted proxy, so a direct LAN caller can't forge it.
    """
    remote = request.remote_addr or "unknown"
    if remote not in TRUSTED_PROXY_IPS:
        return remote
    for header in ("CF-Connecting-IP", "True-Client-IP"):
        if value := request.headers.get(header):
            return value.strip()
    if forwarded := request.headers.get("X-Forwarded-For"):
        return forwarded.split(",")[0].strip()
    return remote


# ------------------------------------------------------------- rate limiting

def _recent_failures(key: str) -> int:
    cutoff = time.monotonic() - LOCKOUT_SECONDS
    with _failure_lock:
        hits = [t for t in _failures.get(key, []) if t > cutoff]
        if hits:
            _failures[key] = hits
        else:
            _failures.pop(key, None)
        return len(hits)


def is_locked_out(key: str) -> bool:
    return _recent_failures(key) >= MAX_FAILURES


def record_failure(key: str) -> int:
    with _failure_lock:
        _failures.setdefault(key, []).append(time.monotonic())
    remaining = MAX_FAILURES - _recent_failures(key)
    return max(remaining, 0)


def clear_failures(key: str) -> None:
    with _failure_lock:
        _failures.pop(key, None)


# ------------------------------------------------------------------- login

def verify_credentials(username: str, password: str, *, device_id: str) -> dict | None:
    """Ask the real Jellyfin whether these credentials are good.

    Returns {"user_id", "user_name"} on success, None on any failure. The
    password is passed through and never logged.
    """
    if not is_configured():
        log.error("JELLYFIN_URL is not set; refusing every login")
        return None

    url = f"{JELLYFIN_URL}/Users/AuthenticateByName"
    headers = {
        # Jellyfin requires an identifying authorization header even to log in.
        "Authorization": (
            'MediaBrowser Client="FlexProxy", Device="FlexProxy", '
            f'DeviceId="{device_id}", Version="10.9.0"'
        ),
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(
            url,
            json={"Username": username, "Pw": password},
            headers=headers,
            timeout=LOGIN_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.error("could not reach Jellyfin at %s: %s", url, exc)
        return None

    if response.status_code == 401:
        log.info("Jellyfin rejected the credentials for %r", username)
        return None
    if not response.ok:
        log.error("Jellyfin returned %s for a login attempt", response.status_code)
        return None

    try:
        user = response.json()["User"]
        user_id, user_name = user["Id"], user["Name"]
    except (ValueError, KeyError, TypeError):
        log.exception("could not read the user out of Jellyfin's login response")
        return None

    log.info("Jellyfin accepted %r (user id %s)", user_name, user_id)
    return {"user_id": user_id, "user_name": user_name}


def issue_token(user_id: str, user_name: str) -> str | None:
    token = secrets.token_urlsafe(32)
    if not db.create_session(token, user_id, user_name, TOKEN_TTL_DAYS):
        log.error("could not store the session for %s; refusing the login", user_id)
        return None
    return token


# -------------------------------------------------------------- token check

def token_from_request(request) -> str | None:
    """Pull the token out of wherever this particular client chose to put it.

    Jellyfin clients are inconsistent: a bare header, a token embedded in an
    Authorization/X-Emby-Authorization value, or a query parameter whose casing
    varies (Finamp's own download URLs use apiKey).
    """
    for header in ("X-Emby-Token", "X-MediaBrowser-Token"):
        if value := request.headers.get(header):
            return value

    for header in ("Authorization", "X-Emby-Authorization"):
        value = request.headers.get(header)
        if value and (match := _BEARER_RE.search(value)):
            return match.group(1)

    for key, value in request.args.items():
        if key.lower() in ("api_key", "apikey") and value:
            return value
    return None


def session_for_request(request) -> dict | None:
    return db.lookup_session(token_from_request(request) or "")
