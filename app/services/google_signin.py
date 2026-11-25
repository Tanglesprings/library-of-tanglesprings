"""Google OpenID Connect for existing library users."""

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from app.auth import get_secret_key
from app.config import JWT_ALGORITHM

CALLBACK_PATH = "/auth/google/callback"
FLOW_COOKIE = "__Host-library_google"
FLOW_AUDIENCE = "library-google-oauth"
FLOW_SECONDS = 600
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_google_keys = jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs", timeout=5)


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    allowed_emails: frozenset[str]


def config_for(request: Request) -> Config | None:
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    uri = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
    emails = frozenset(
        email.strip().casefold() for email in os.environ.get("GOOGLE_ALLOWED_EMAILS", "").split(",")
        if email.strip()
    )
    if not client_id or not secret or not uri or not emails:
        return None
    try:
        origin = urlsplit(uri)
    except ValueError:
        return None
    if (origin.scheme != "https" or origin.path != CALLBACK_PATH
            or origin.query or origin.fragment or origin.username is not None
            or request.url.scheme != origin.scheme or request.url.netloc != origin.netloc):
        return None
    return Config(client_id, secret, uri, emails)


def enabled(request: Request) -> bool:
    return config_for(request) is not None


def new_flow(config: Config, user: dict | None = None) -> tuple[str, str]:
    flow = {
        "aud": FLOW_AUDIENCE,
        "exp": int(time.time()) + FLOW_SECONDS,
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "verifier": secrets.token_urlsafe(48),
    }
    challenge = hashlib.sha256(flow["verifier"].encode()).digest()
    params = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": "openid email",
        "state": flow["state"],
        "nonce": flow["nonce"],
        "code_challenge": base64.urlsafe_b64encode(challenge).decode().rstrip("="),
        "code_challenge_method": "S256",
    }
    if user:
        flow["link"] = {key: user[key] for key in ("id", "username", "token_version")}
        params["prompt"] = "select_account"
    signed_flow = jwt.encode(flow, get_secret_key(), algorithm=JWT_ALGORITHM)
    return f"{_GOOGLE_AUTH_URL}?{urlencode(params)}", signed_flow


def set_flow_cookie(response: Response, flow: str) -> None:
    response.set_cookie(FLOW_COOKIE, flow, max_age=FLOW_SECONDS, httponly=True,
                        secure=True, samesite="lax")


def clear_flow_cookie(response: Response) -> None:
    response.delete_cookie(FLOW_COOKIE, httponly=True, secure=True, samesite="lax")


def read_flow(request: Request, state: str) -> dict | None:
    signed = request.cookies.get(FLOW_COOKIE)
    if not signed or not state:
        return None
    try:
        flow = jwt.decode(signed, get_secret_key(), algorithms=[JWT_ALGORITHM],
                          audience=FLOW_AUDIENCE, options={"require": ["exp"]})
    except jwt.InvalidTokenError:
        return None
    if not hmac.compare_digest(flow["state"].encode(), state.encode()):
        return None
    return flow


def _verify_id_token(token: str, client_id: str, nonce: str) -> tuple[str, str]:
    key = _google_keys.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token, key.key, algorithms=["RS256"], audience=client_id, issuer=_GOOGLE_ISSUERS,
        options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"], "strict_aud": True},
    )
    subject, email = claims["sub"], claims.get("email")
    token_nonce = claims["nonce"]
    if (claims.get("azp", client_id) != client_id or not isinstance(token_nonce, str)
            or not hmac.compare_digest(token_nonce.encode(), nonce.encode())
            or not subject or not isinstance(email, str) or not email
            or claims.get("email_verified") is not True):
        raise jwt.InvalidTokenError("Google identity verification failed")
    return subject, email


async def verified_identity(config: Config, code: str, flow: dict) -> tuple[str, str]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(_GOOGLE_TOKEN_URL, data={
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code": code,
            "code_verifier": flow["verifier"],
            "grant_type": "authorization_code",
            "redirect_uri": config.redirect_uri,
        })
        response.raise_for_status()
        payload = response.json()
    token = payload.get("id_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise jwt.InvalidTokenError("Google did not return an ID token")
    return await run_in_threadpool(_verify_id_token, token, config.client_id, flow["nonce"])
