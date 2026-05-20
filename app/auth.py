"""Simple session-based admin auth.

If ADMIN_PASSWORD is not set in .env, all operations are open (single-user mode).
If set, GET routes are public; write routes and admin pages require login.
"""
import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import Cookie, HTTPException, Request

from app.config import settings

SESSION_TTL_HOURS = 24
_sessions: dict[str, datetime] = {}


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def auth_enabled() -> bool:
    return bool(settings.ADMIN_PASSWORD)


def verify_password(password: str) -> bool:
    if not settings.ADMIN_PASSWORD:
        return True
    return _hash(password) == _hash(settings.ADMIN_PASSWORD)


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)
    return token


def is_admin_session(token: str | None) -> bool:
    if not auth_enabled():
        return True
    if not token or token not in _sessions:
        return False
    if datetime.utcnow() > _sessions[token]:
        _sessions.pop(token, None)
        return False
    return True


def revoke_session(token: str) -> None:
    _sessions.pop(token, None)


def require_admin(request: Request) -> None:
    """FastAPI dependency: raises 401 if not admin."""
    if not auth_enabled():
        return
    token = request.cookies.get("admin_token")
    if not is_admin_session(token):
        raise HTTPException(status_code=401, detail="Admin login required")
