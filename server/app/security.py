"""Passwords, sessions, CSRF, TOTP and secret-field encryption."""
import base64
import hashlib
import secrets
from datetime import timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request

from .config import settings
from .db import Session, User, get_db, utcnow

COOKIE = "sid"
ROLE_RANK = {"viewer": 0, "manager": 1, "admin": 2}
MIN_PASSWORD = 12
MAX_FAILED = 5
LOCK_MINUTES = 15
CLAIM_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"      # no look-alike characters

_hasher = PasswordHasher()
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()))


class LoginRequired(Exception):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerificationError, ValueError):
        return False


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def new_claim_code() -> str:
    raw = "".join(secrets.choice(CLAIM_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalise_claim(code: str) -> str:
    return code.upper().replace(" ", "").replace("-", "")


def temp_password() -> str:
    return secrets.token_urlsafe(12)


def encrypt(text: str) -> str:
    return _fernet.encrypt(text.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def totp_ok(user: User, code: str) -> bool:
    secret = decrypt(user.totp_secret) if user.totp_secret else None
    return bool(secret) and pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def start_session(db, user: User) -> str:
    token = secrets.token_urlsafe(32)
    db.add(Session(token_hash=sha256_hex(token), csrf=secrets.token_urlsafe(24), user_id=user.id,
                   expires_at=utcnow() + timedelta(hours=settings.session_hours)))
    return token


def load_session(request: Request, db) -> Session | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    sess = db.query(Session).filter_by(token_hash=sha256_hex(token)).first()
    if not sess or sess.expires_at < utcnow() or not sess.user.active:
        return None
    return sess


def need(role: str = "viewer"):
    """Route dependency: signed-in user with at least this role; POSTs must carry the CSRF token."""
    async def dep(request: Request, db=Depends(get_db)) -> User:
        sess = load_session(request, db)
        if not sess:
            raise LoginRequired()
        if request.method == "POST":
            form = await request.form()
            if not secrets.compare_digest(str(form.get("csrf", "")), sess.csrf):
                raise HTTPException(403, "Form expired. Go back, reload the page and try again.")
        request.state.user = sess.user
        request.state.csrf = sess.csrf
        if sess.user.must_change and not request.url.path.startswith(("/account", "/logout")):
            raise HTTPException(307, headers={"Location": "/account"})
        if ROLE_RANK[sess.user.role] < ROLE_RANK[role]:
            raise HTTPException(403, "Your account does not have permission for this.")
        return sess.user
    return dep
