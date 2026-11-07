"""Password hashing (Argon2id), short-lived JWT access tokens, and rotating refresh tokens."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings

_hasher = PasswordHasher()
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# A real hash to verify against when the email is unknown, so timing doesn't reveal accounts.
DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def create_access_token(user_id: uuid.UUID) -> tuple[str, int]:
    settings = get_settings()
    now = dt.datetime.now(dt.timezone.utc)
    lifetime = settings.access_token_minutes * 60
    payload = {"sub": str(user_id), "iss": settings.jwt_issuer, "iat": now,
               "exp": now + dt.timedelta(seconds=lifetime), "type": "access"}
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM), lifetime


def decode_access_token(token: str) -> uuid.UUID | None:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM], issuer=settings.jwt_issuer,
                             options={"require": ["exp", "sub", "iss"]})
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "access":
        return None
    try:
        return uuid.UUID(payload["sub"])
    except ValueError:
        return None


def new_refresh_token() -> tuple[str, str]:
    """Returns (opaque token for the client, SHA-256 hash to store)."""
    token = secrets.token_urlsafe(48)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
