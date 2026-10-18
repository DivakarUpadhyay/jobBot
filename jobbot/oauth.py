"""Connecting a mailbox by signing in to Google or Microsoft (OAuth 2.0), so the service never sees a password.

The customer presses "Connect Gmail" (or Outlook), signs in at Google or Microsoft and agrees to let the service
send mail and read replies. The provider sends them back to /oauth/callback with a one-time code, which is
exchanged for a refresh token. That token is stored encrypted (secrets_box) in the account's own .env as
SENDER_n_OAUTH_TOKEN, next to SENDER_n_OAUTH=google|microsoft. To send, the refresh token buys a short-lived access
token, used with SMTP and IMAP through XOAUTH2. The customer can withdraw access at any time in their Google or
Microsoft account, or by removing the mailbox in the portal.

The operator sets up one app with each provider and puts its keys in DATA/.env:
    JOBBOT_PUBLIC_URL            https://your-domain (the callback is <this>/oauth/callback)
    GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
    MICROSOFT_OAUTH_CLIENT_ID, MICROSOFT_OAUTH_CLIENT_SECRET
Google treats full Gmail access (https://mail.google.com/) as a restricted scope: until the app passes Google's
verification and security assessment, only test users added in the Google Cloud console can connect.
"""
import base64
import hashlib
import json
import secrets
import time
import urllib.parse

import requests

from . import control as ct
from . import secrets_box
from .ops import env

PROVIDERS = {
    "google": {
        "name": "Gmail",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "openid email https://mail.google.com/",
        "extra": {"access_type": "offline", "prompt": "consent"},
        "smtp": ("smtp.gmail.com", 465, "ssl"), "imap": "imap.gmail.com",
        "client_id": "GOOGLE_OAUTH_CLIENT_ID", "client_secret": "GOOGLE_OAUTH_CLIENT_SECRET",
    },
    "microsoft": {
        "name": "Outlook / Microsoft 365",
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "openid email offline_access https://outlook.office.com/SMTP.Send https://outlook.office.com/IMAP.AccessAsUser.All",
        "extra": {"prompt": "select_account"},
        "smtp": ("smtp.office365.com", 587, "starttls"), "imap": "outlook.office365.com",
        "client_id": "MICROSOFT_OAUTH_CLIENT_ID", "client_secret": "MICROSOFT_OAUTH_CLIENT_SECRET",
    },
}
STATE_MINUTES = 15
_states = {}          # state -> (account_id, user_id, provider, PKCE verifier, expires); one process serves the callback
_access = {}          # (provider, email) -> (access token, expires)


def configured():
    """The providers whose app keys are set, for the portal to offer."""
    return [{"provider": k, "name": p["name"]} for k, p in PROVIDERS.items()
            if env(p["client_id"]) and env(p["client_secret"]) and env("JOBBOT_PUBLIC_URL")]


def redirect_uri():
    return env("JOBBOT_PUBLIC_URL", "").rstrip("/") + "/oauth/callback"


def start(account_id, user_id, provider):
    """The address to send the customer to. Raises ValueError when the provider is not set up."""
    p = PROVIDERS.get(provider)
    if not p:
        raise ValueError("provider must be google or microsoft")
    if not (env(p["client_id"]) and env(p["client_secret"]) and env("JOBBOT_PUBLIC_URL")):
        raise ValueError(f"signing in with {p['name']} is not set up on this service yet")
    now = time.time()
    for k in [k for k, v in _states.items() if v[4] < now]:
        _states.pop(k, None)
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    _states[state] = (account_id, user_id, provider, verifier, now + STATE_MINUTES * 60)
    query = {"client_id": env(p["client_id"]), "redirect_uri": redirect_uri(), "response_type": "code", "scope": p["scope"],
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256", **p["extra"]}
    return p["authorize"] + "?" + urllib.parse.urlencode(query)


def _id_email(id_token):
    """The email claim of the ID token we were just given by the provider over TLS (so its signature need not be checked)."""
    payload = id_token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return (claims.get("email") or claims.get("preferred_username") or "").lower()


def finish(state, code):
    """Exchange the code; returns (account_id, user_id, provider, email, refresh token). Raises ValueError."""
    entry = _states.pop(state or "", None)
    if not entry or entry[4] < time.time():
        raise ValueError("this sign-in link has expired; start again from the portal")
    account_id, user_id, provider, verifier, _ = entry
    p = PROVIDERS[provider]
    r = requests.post(p["token"], timeout=30, data={
        "client_id": env(p["client_id"]), "client_secret": env(p["client_secret"]), "code": code, "code_verifier": verifier,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("refresh_token"):
        raise ValueError(f"{p['name']} did not grant access: {data.get('error_description') or data.get('error') or r.status_code}")
    email = _id_email(data.get("id_token", ""))
    if not ct.EMAIL.match(email):
        raise ValueError(f"{p['name']} did not say which mailbox was connected")
    _access[(provider, email)] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 120)
    return account_id, user_id, provider, email, data["refresh_token"]


def access_token(sender):
    """A current access token for a sender connected by OAuth (refreshed when it is about to run out)."""
    provider, email = sender["oauth"], sender["email"].lower()
    cached = _access.get((provider, email))
    if cached and cached[1] > time.time():
        return cached[0]
    p = PROVIDERS[provider]
    r = requests.post(p["token"], timeout=30, data={
        "client_id": env(p["client_id"]), "client_secret": env(p["client_secret"]), "grant_type": "refresh_token",
        "refresh_token": secrets_box.open_(sender["oauth_token"])})
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("access_token"):
        raise RuntimeError(f"{email}: {p['name']} refused to renew access ({data.get('error') or r.status_code}); "
                           f"connect the mailbox again in the portal")
    _access[(provider, email)] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 120)
    return data["access_token"]


def xoauth2(email, token):
    """The XOAUTH2 string SMTP and IMAP expect."""
    return f"user={email}\x01auth=Bearer {token}\x01\x01"
