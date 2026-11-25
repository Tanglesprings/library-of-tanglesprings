import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import decode_token
from app.database import get_db
from app.services import google_signin as google

START = "/auth/google/start"
CALLBACK = google.CALLBACK_PATH
ACCOUNT = "/account/google"


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", f"https://testserver{CALLBACK}")
    monkeypatch.setenv("GOOGLE_ALLOWED_EMAILS", "admin@example.com")
    exchanges = []

    async def identity(config, code, flow):
        exchanges.append((code, flow))
        return code, "admin@example.com"

    monkeypatch.setattr(google, "verified_identity", identity)
    return exchanges


@pytest.fixture
def signed_in(client, admin_user):
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token")
    response = client.post("/login", data={"username": "admin", "password": "password123"}, follow_redirects=False)
    assert response.status_code == 303
    return client


def _state(url):
    return parse_qs(urlsplit(url).query)["state"][0]


def _start_login(client):
    response = client.get(START, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert urlsplit(location).netloc == "accounts.google.com"
    assert parse_qs(urlsplit(location).query)["code_challenge_method"] == ["S256"]
    return _state(location)


def _start_link(client):
    response = client.post(f"{ACCOUNT}/link", data={
        "password": "password123", "_csrf": client.cookies["csrf_token"],
    })
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("httponly", "secure", "samesite=lax", "path=/"))
    return _state(response.headers["refresh"].split("url=", 1)[1])


def _callback(client, state, code):
    return client.get(CALLBACK, params={"state": state, "code": code},
                      headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})


def test_google_is_optional(client, admin_user, monkeypatch):
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "GOOGLE_ALLOWED_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    assert "Sign in with Google" not in client.get("/login").text
    assert client.get(START).status_code == 404
    response = client.post("/login", data={"username": "admin", "password": "password123"}, follow_redirects=False)
    assert response.status_code == 303


def test_oauth_links_and_signs_in_the_existing_admin(signed_in, admin_user, provider, db):
    assert 'href="/account/google"' in signed_in.get("/").text
    state = _start_link(signed_in)
    # A Google navigation supplies the Lax flow cookie and withholds Strict.
    signed_in.cookies.delete("access_token")
    done = _callback(signed_in, state, "google-subject")
    assert done.headers["refresh"] == f"0;url={ACCOUNT}"
    assert done.headers["referrer-policy"] == "no-referrer"
    assert done.headers["cache-control"] == "no-store"
    assert "admin@example.com" in signed_in.get(ACCOUNT).text
    assert signed_in.cookies.get(google.FLOW_COOKIE) is None

    signed_in.post("/logout", follow_redirects=False)
    assert "Sign in with Google" in signed_in.get("/login").text
    done = _callback(signed_in, _start_login(signed_in), "google-subject")
    assert done.headers["refresh"] == "0;url=/"
    assert signed_in.get("/api/users").json()[0]["id"] == admin_user["id"]
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert len(provider) == 2


def test_oauth_rejects_unknown_identity_and_mismatched_state(client, admin_user, provider, db):
    state = _start_login(client)
    assert _callback(client, state + "wrong", "google-subject").headers["refresh"] == "0;url=/login?google=failed"
    assert not provider
    assert _callback(client, _start_login(client), "google-subject").headers["refresh"] == "0;url=/login?google=failed"
    assert len(provider) == 1
    assert client.cookies.get("access_token") is None
    assert not db.execute("SELECT 1 FROM google_identities").fetchone()


def test_link_requires_csrf_and_current_password(signed_in, provider):
    assert signed_in.post(f"{ACCOUNT}/link", data={"password": "password123"}).status_code == 403
    assert signed_in.post(f"{ACCOUNT}/link", data={
        "password": "wrong", "_csrf": signed_in.cookies["csrf_token"],
    }).status_code == 401
    assert signed_in.cookies.get(google.FLOW_COOKIE) is None


def test_only_an_approved_verified_email_can_link(signed_in, provider, monkeypatch, db):
    monkeypatch.setenv("GOOGLE_ALLOWED_EMAILS", "different@example.com")
    result = _callback(signed_in, _start_link(signed_in), "google-subject")
    assert result.headers["refresh"] == f"0;url={ACCOUNT}?google=failed"
    assert not db.execute("SELECT 1 FROM google_identities").fetchone()


def test_link_cannot_replace_an_existing_binding(signed_in, provider):
    assert _callback(signed_in, _start_link(signed_in), "first-subject").headers["refresh"] == f"0;url={ACCOUNT}"
    assert _callback(signed_in, _start_link(signed_in), "second-subject").headers["refresh"] == f"0;url={ACCOUNT}?google=failed"
    signed_in.post("/logout", follow_redirects=False)
    assert _callback(signed_in, _start_login(signed_in), "second-subject").headers["refresh"] == "0;url=/login?google=failed"


def test_approved_gmail_invitation_links_only_to_its_own_admin(signed_in, provider, monkeypatch):
    email = "second-admin@gmail.com"
    monkeypatch.setenv("GOOGLE_ALLOWED_EMAILS", f"admin@example.com,{email.upper()}")

    async def identity(config, code, flow):
        return "second-admin-subject", email

    monkeypatch.setattr(google, "verified_identity", identity)
    with get_db() as db:
        user_id = db.execute(
            "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, 'admin')",
            ("second-admin", "unused", "Second Admin"),
        ).lastrowid
        db.execute("INSERT INTO google_invitations (user_id, email) VALUES (?, ?)", (user_id, email))

    refused = _callback(signed_in, _start_link(signed_in), "invitation-code")
    assert refused.headers["refresh"] == f"0;url={ACCOUNT}?google=failed"
    signed_in.post("/logout", follow_redirects=False)
    done = _callback(signed_in, _start_login(signed_in), "invitation-code")
    assert done.headers["refresh"] == "0;url=/"
    assert decode_token(signed_in.cookies["access_token"])["sub"] == str(user_id)
    assert signed_in.get("/api/users").status_code == 200
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
        assert db.execute("SELECT user_id FROM google_identities").fetchone()[0] == user_id
        assert db.execute("SELECT COUNT(*) FROM google_invitations").fetchone()[0] == 0


def test_unlink_revokes_google_access_and_prior_sessions(signed_in, provider):
    _callback(signed_in, _start_link(signed_in), "google-subject")
    prior_session = signed_in.cookies["access_token"]
    response = signed_in.post(f"{ACCOUNT}/unlink", data={
        "password": "password123", "_csrf": signed_in.cookies["csrf_token"],
    })
    assert response.status_code == 200
    assert "Link Google account" in response.text
    assert signed_in.get("/api/users").status_code == 200
    signed_in.cookies.delete("access_token")
    signed_in.cookies.set("access_token", prior_session)
    assert signed_in.get("/api/users", follow_redirects=False).headers["location"] == "/login"
    assert _callback(signed_in, _start_login(signed_in), "google-subject").headers["refresh"] == "0;url=/login?google=failed"


@pytest.mark.parametrize("extra", [{}, {"nonce": "other"}, {"email_verified": False},
                                  {"aud": "other"}, {"iss": "https://other.example"}])
def test_id_token_signature_and_claims(monkeypatch, extra):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(google._google_keys, "get_signing_key_from_jwt", lambda _: SimpleNamespace(key=key.public_key()))
    claims = {
        "iss": "https://accounts.google.com", "aud": "test-client", "sub": "google-subject",
        "nonce": "nonce", "email": "admin@example.com", "email_verified": True,
        "iat": int(time.time()), "exp": int(time.time()) + 60, **extra,
    }
    token = jwt.encode(claims, key, algorithm="RS256")
    if extra:
        with pytest.raises(jwt.InvalidTokenError):
            google._verify_id_token(token, "test-client", "nonce")
    else:
        assert google._verify_id_token(token, "test-client", "nonce") == ("google-subject", "admin@example.com")
        with pytest.raises(jwt.InvalidTokenError):
            google._verify_id_token(token[:-5] + "invalid", "test-client", "nonce")
