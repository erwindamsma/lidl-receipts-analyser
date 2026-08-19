"""OAuth2 authorization-code + PKCE flow against accounts.lidl.com.

Login happens in the user's own browser rather than an automated one. Lidl
fronts the form with reCAPTCHA Enterprise. Driving a browser, the route the
other Lidl Plus clients take, means keeping pace with a captcha built to stay
ahead. A human browser solves it for free, and handler.py catches
the callback, so nothing has to be pasted back by hand.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import urllib.parse
from pathlib import Path

from .config import CONFIG_DIR
from .http import request_json

AUTH_API = "https://accounts.lidl.com"
CLIENT_ID = "LidlPlusNativeClient"
CLIENT_SECRET = "secret"  # public value baked into the mobile app
REDIRECT_URI = "com.lidlplus.app://callback"
SCOPES = "openid profile offline_access lpprofile lpapis"

PKCE_PATH = CONFIG_DIR / "pkce.json"


def _basic_auth_header() -> str:
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def generate_pkce() -> tuple[str, str]:
    """Return (verifier, challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(challenge: str, country: str, language: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "Country": country.upper(),
        "language": f"{language.lower()}-{country.upper()}",
    }
    return f"{AUTH_API}/connect/authorize?{urllib.parse.urlencode(params)}"


def save_verifier(verifier: str) -> None:
    """Persist the PKCE verifier so `login --code` can finish a broken run."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(PKCE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"verifier": verifier}, handle)


def load_verifier() -> str:
    if not PKCE_PATH.exists():
        raise RuntimeError(
            "No pending login found. Run `lidl login` (without --code) first."
        )
    return json.loads(PKCE_PATH.read_text())["verifier"]


def clear_verifier() -> None:
    Path(PKCE_PATH).unlink(missing_ok=True)


def extract_code(pasted: str) -> str:
    """Accept either the full callback URL or a bare authorization code."""
    pasted = pasted.strip().strip('"').strip("'")
    if not pasted:
        raise ValueError("empty input")
    if "code=" in pasted:
        match = re.search(r"[?&#]code=([^&\s#]+)", pasted)
        if not match:
            raise ValueError(f"could not find a code in: {pasted[:120]}")
        return urllib.parse.unquote(match.group(1))
    if re.fullmatch(r"[A-Za-z0-9._~\-]+", pasted):
        return pasted
    raise ValueError(
        "input looks like neither a callback URL nor an authorization code"
    )


def exchange_code(code: str, verifier: str) -> dict:
    """Trade an authorization code for an access/refresh token pair.

    Never retried. An authorization code is single-use and a refresh token
    rotates on every exchange, so a second attempt spends a credential that
    the first attempt may already have consumed, and logs you out.
    """
    return request_json(
        f"{AUTH_API}/connect/token",
        retries=1,
        method="POST",
        headers={"Authorization": _basic_auth_header()},
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
    )


def refresh_tokens(refresh_token: str) -> dict:
    """Exchange a refresh token for a fresh access token.

    Not retried, for the same reason as exchange_code: the response carries a
    new refresh token and invalidates the old one.
    """
    return request_json(
        f"{AUTH_API}/connect/token",
        retries=1,
        method="POST",
        headers={"Authorization": _basic_auth_header()},
        form={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
