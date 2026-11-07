"""Sign-up, sign-in, token refresh (with rotation) and sign-out."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.errors import ApiError
from app.models import Membership, RefreshToken, User, Workspace
from app.schemas import LoginIn, MeOut, RefreshIn, RegisterIn, TokenOut, WorkspaceAccessOut
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
def register(body: RegisterIn, db: Session = Depends(get_db)) -> TokenOut:
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
def login(body: LoginIn, db: Session = Depends(get_db)) -> TokenOut:
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    valid = verify_password(user.password_hash if user else DUMMY_HASH, body.password)
    if user is None or not valid or not user.is_active:
        raise ApiError("INVALID_CREDENTIALS", "Email or password is incorrect.", 401)
    return _issue(db, user)


@router.post("/auth/refresh", response_model=TokenOut, response_model_by_alias=True)
def refresh(body: RefreshIn, db: Session = Depends(get_db)) -> TokenOut:
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
