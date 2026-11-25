import logging

import httpx
import jwt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.auth import create_token, require_role, set_auth_cookie, verify_password
from app.database import get_db
from app.services import google_signin as google

router = APIRouter(include_in_schema=False)
logger = logging.getLogger(__name__)
ACCOUNT_PATH = "/account/google"
START_PATH = "/auth/google/start"
_SESSION_COLUMNS = "u.id, u.username, u.role, u.display_name, u.token_version"
_PRIVATE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _config(request: Request) -> google.Config:
    config = google.config_for(request)
    if config is None:
        raise HTTPException(status_code=404)
    return config


def _continue(request: Request, target: str, label: str):
    # A new document releases the form-action CSP restriction and starts a
    # same-origin navigation after the OAuth callback sets the Strict session.
    return request.app.state.templates.TemplateResponse(
        request, "auth_continue.html", {"target": target, "label": label},
        headers={**_PRIVATE, "Refresh": f"0;url={target}"},
    )


def _set_session(request: Request, response, user) -> None:
    token = create_token(user["id"], user["username"], user["role"],
                         user["display_name"], user["token_version"])
    set_auth_cookie(request, response, token)


def _account_page(request: Request, user: dict, *, error=None, status=200):
    with get_db() as db:
        identity = db.execute(
            "SELECT email FROM google_identities WHERE user_id = ?", (user["id"],),
        ).fetchone()
    return request.app.state.templates.TemplateResponse(
        request, "account_google.html",
        {"google_email": identity["email"] if identity else None, "error": error},
        status_code=status, headers=_PRIVATE,
    )


async def _password_matches(user: dict, password: str) -> bool:
    with get_db() as db:
        row = db.execute("SELECT password FROM users WHERE id = ? AND token_version = ?",
                         (user["id"], user["token_version"])).fetchone()
    if not row:
        return False
    try:
        return await run_in_threadpool(verify_password, password, row["password"])
    except ValueError:
        return False


@router.get(ACCOUNT_PATH)
async def google_account(request: Request, user=Depends(require_role("viewer"))):
    _config(request)
    error = None
    if request.query_params.get("google") == "failed":
        error = "Google linking didn’t finish. Try again or choose a different Google account."
    return _account_page(request, user, error=error)


@router.post(f"{ACCOUNT_PATH}/link")
async def google_link(request: Request, password: str = Form(...), user=Depends(require_role("viewer"))):
    config = _config(request)
    if not await _password_matches(user, password):
        return _account_page(request, user, error="Current library password is incorrect.", status=401)
    target, flow = google.new_flow(config, user)
    response = _continue(request, target, "Continue to Google")
    google.set_flow_cookie(response, flow)
    return response


@router.post(f"{ACCOUNT_PATH}/unlink")
async def google_unlink(request: Request, password: str = Form(...), user=Depends(require_role("viewer"))):
    _config(request)
    if not await _password_matches(user, password):
        return _account_page(request, user, error="Current library password is incorrect.", status=401)
    with get_db() as db:
        current = db.execute(
            "UPDATE users SET token_version = token_version + 1 "
            "WHERE id = ? AND token_version = ? RETURNING id, username, role, display_name, token_version",
            (user["id"], user["token_version"]),
        ).fetchone()
        if current is None:
            return RedirectResponse("/login", status_code=303, headers=_PRIVATE)
        db.execute("DELETE FROM google_identities WHERE user_id = ?", (current["id"],))
    response = RedirectResponse(ACCOUNT_PATH, status_code=303, headers=_PRIVATE)
    google.clear_flow_cookie(response)
    _set_session(request, response, current)
    return response


@router.get(START_PATH)
async def google_start(request: Request):
    config = _config(request)
    if request.headers.get("sec-fetch-site") == "cross-site":
        return RedirectResponse("/login", status_code=303, headers=_PRIVATE)
    target, flow = google.new_flow(config)
    response = RedirectResponse(target, status_code=303, headers=_PRIVATE)
    google.set_flow_cookie(response, flow)
    return response


def _link_user(request: Request, link: dict, subject: str, email: str):
    current = request.state.user
    if current and current["id"] != link["id"]:
        return None
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute(
            f"SELECT {_SESSION_COLUMNS} FROM users u WHERE u.id = ? AND u.username = ? AND u.token_version = ?",
            (link["id"], link["username"], link["token_version"]),
        ).fetchone()
        if user is None:
            return None
        inserted = db.execute(
            "INSERT INTO google_identities (user_id, subject, email) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            (user["id"], subject, email),
        )
        return user if inserted.rowcount else None


def _linked_user(subject: str):
    with get_db() as db:
        return db.execute(
            f"SELECT {_SESSION_COLUMNS} FROM users u JOIN google_identities g ON g.user_id = u.id "
            "WHERE g.subject = ?", (subject,),
        ).fetchone()


@router.get(google.CALLBACK_PATH)
async def google_callback(request: Request, state: str = "", code: str = ""):
    config = _config(request)
    flow = google.read_flow(request, state)
    link = flow.get("link") if flow else None
    user = None
    if flow and code:
        try:
            subject, email = await google.verified_identity(config, code, flow)
        except (httpx.HTTPError, jwt.PyJWTError, ValueError) as exc:
            logger.warning("Google sign-in failed during identity verification (%s)", type(exc).__name__)
        else:
            user = _link_user(request, link, subject, email) if link else _linked_user(subject)

    if user:
        destination = ACCOUNT_PATH if link else "/"
        response = _continue(request, destination, "Return to the library")
        _set_session(request, response, user)
    else:
        destination = f"{ACCOUNT_PATH}?google=failed" if link else "/login?google=failed"
        response = _continue(request, destination, "Return to the library")
    google.clear_flow_cookie(response)
    return response
