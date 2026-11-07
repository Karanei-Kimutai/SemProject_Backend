"""Sign-up, sign-in, token refresh (with rotation) and sign-out."""

from __future__ import annotations

import datetime as dt

import secrets

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.errors import ApiError
from app import email, ratelimit
from app.models import Membership, PasswordReset, RefreshToken, User, Workspace
from app.schemas import (
    ChangePasswordIn, LoginIn, MeOut, PasswordResetConfirmIn, PasswordResetRequestIn, RefreshIn, RegisterIn, TokenOut,
    WorkspaceAccessOut,
)
from app.security import (
    DUMMY_HASH, create_access_token, hash_password, hash_refresh_token, new_refresh_token, verify_password,
)

router = APIRouter(prefix="/v1", tags=["auth"])


def _issue(db: Session, user: User) -> TokenOut:
    access, lifetime = create_access_token(user.id)
    token, token_hash = new_refresh_token()
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=get_settings().refresh_token_days)
    db.add(RefreshToken(user_id=user.id, token_hash=token_hash, expires_at=expires))
    db.commit()
    return TokenOut(access_token=access, refresh_token=token, expires_in=lifetime)


@router.post("/auth/register", response_model=TokenOut, status_code=201, response_model_by_alias=True)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    ratelimit.hit("register_ip", ratelimit.client_ip(request))
    if not get_settings().allow_self_registration:
        raise ApiError("REGISTRATION_CLOSED", "New accounts are created by an administrator.", 403)
    user = User(email=body.email.lower(), password_hash=hash_password(body.password), display_name=body.display_name)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError("EMAIL_TAKEN", "An account with this email already exists.", 409) from None
    return _issue(db, user)


@router.post("/auth/login", response_model=TokenOut, response_model_by_alias=True)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    ratelimit.hit("login_ip", ratelimit.client_ip(request))
    ratelimit.hit("login_email", body.email.lower())
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    valid = verify_password(user.password_hash if user else DUMMY_HASH, body.password)
    if user is None or not valid or not user.is_active:
        raise ApiError("INVALID_CREDENTIALS", "Email or password is incorrect.", 401)
    return _issue(db, user)


@router.post("/auth/refresh", response_model=TokenOut, response_model_by_alias=True)
def refresh(body: RefreshIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    ratelimit.hit("refresh_ip", ratelimit.client_ip(request))
    now = dt.datetime.now(dt.timezone.utc)
    stored = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(body.refresh_token))
                       .with_for_update())
    if stored is None or stored.expires_at <= now:
        raise ApiError("INVALID_REFRESH_TOKEN", "Your session has expired. Sign in again.", 401)
    if stored.revoked_at is not None:
        # A rotated token was used again: assume it was stolen and end every session for this user.
        for token in db.scalars(select(RefreshToken).where(RefreshToken.user_id == stored.user_id,
                                                           RefreshToken.revoked_at.is_(None))):
            token.revoked_at = now
        db.commit()
        raise ApiError("INVALID_REFRESH_TOKEN", "Your session has expired. Sign in again.", 401)
    user = db.get(User, stored.user_id)
    if user is None or not user.is_active:
        raise ApiError("INVALID_REFRESH_TOKEN", "Your session has expired. Sign in again.", 401)
    stored.revoked_at = now
    return _issue(db, user)


@router.post("/auth/logout", status_code=204)
def logout(body: RefreshIn, db: Session = Depends(get_db)) -> None:
    stored = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(body.refresh_token)))
    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = dt.datetime.now(dt.timezone.utc)
        db.commit()


@router.get("/me", response_model=MeOut, response_model_by_alias=True)
def me(user: User = Depends(current_user), db: Session = Depends(get_db)) -> MeOut:
    rows = db.execute(select(Workspace, Membership.role).outerjoin(
        Membership, (Membership.workspace_id == Workspace.id) & (Membership.user_id == user.id))
        .where((Membership.user_id == user.id) | Workspace.is_reference).order_by(Workspace.is_reference.desc(), Workspace.name))
    access = [WorkspaceAccessOut(workspace_id=w.id, name=w.name, is_reference=w.is_reference,
                                 role=role.value if role else "REFERENCE") for w, role in rows]
    return MeOut(id=user.id, email=user.email, display_name=user.display_name, is_research=user.is_research,
                 workspaces=access)


def _revoke_all(db: Session, user_id) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    for token in db.scalars(select(RefreshToken).where(RefreshToken.user_id == user_id,
                                                       RefreshToken.revoked_at.is_(None))):
        token.revoked_at = now


@router.post("/auth/password-reset/request", status_code=202)
def request_password_reset(body: PasswordResetRequestIn, request: Request, db: Session = Depends(get_db)) -> dict:
    """Always 202, whether or not the email has an account (so accounts can't be discovered)."""
    ratelimit.hit("reset_request_ip", ratelimit.client_ip(request))
    ratelimit.hit("reset_request_email", body.email.lower())
    settings = get_settings()
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if user is not None and user.is_active:
        now = dt.datetime.now(dt.timezone.utc)
        for old in db.scalars(select(PasswordReset).where(PasswordReset.user_id == user.id,
                                                          PasswordReset.used_at.is_(None))):
            old.used_at = now  # only the newest code works
        code = f"{secrets.randbelow(10**8):08d}"
        db.add(PasswordReset(user_id=user.id, code_hash=hash_refresh_token(f"{user.id}:{code}"),
                             expires_at=now + dt.timedelta(minutes=settings.password_reset_minutes)))
        db.commit()
        email.send(user.email, f"{settings.app_name}: your password reset code",
                   f"Your password reset code is {code}.\n\nIt works once, for {settings.password_reset_minutes} "
                   "minutes. If you didn't ask to reset your password, ignore this email.")
    return {"status": "SENT_IF_ACCOUNT_EXISTS"}


@router.post("/auth/password-reset/confirm", response_model=TokenOut, response_model_by_alias=True)
def confirm_password_reset(body: PasswordResetConfirmIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    ratelimit.hit("reset_confirm_ip", ratelimit.client_ip(request))
    invalid = ApiError("INVALID_RESET_CODE", "That code is wrong or has expired. Ask for a new one.", 400)
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not user.is_active:
        raise invalid
    now = dt.datetime.now(dt.timezone.utc)
    reset = db.scalar(select(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
                      .order_by(PasswordReset.created_at.desc()).limit(1).with_for_update())
    if reset is None or reset.expires_at <= now or reset.attempts >= get_settings().password_reset_max_attempts:
        raise invalid
    if not secrets.compare_digest(reset.code_hash, hash_refresh_token(f"{user.id}:{body.code}")):
        reset.attempts += 1
        db.commit()
        raise invalid
    reset.used_at = now
    user.password_hash = hash_password(body.new_password)
    _revoke_all(db, user.id)  # sign out everywhere
    db.commit()
    return _issue(db, user)


@router.post("/auth/change-password", response_model=TokenOut, response_model_by_alias=True)
def change_password(body: ChangePasswordIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> TokenOut:
    if not verify_password(user.password_hash, body.current_password):
        raise ApiError("INVALID_CREDENTIALS", "Your current password is incorrect.", 401)
    user.password_hash = hash_password(body.new_password)
    _revoke_all(db, user.id)
    db.commit()
    return _issue(db, user)
