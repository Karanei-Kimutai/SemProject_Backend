"""Request dependencies: the current user and workspace/store access checks.

Access rules:
* Reference workspaces are visible to every signed-in user. Everyone may read and generate
  forecasts there; nobody may change their data.
* Other workspaces need a membership. OWNER and MANAGER may generate forecasts and change data;
  VIEWER may only read.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import ApiError, forbidden, not_found
from app.models import Membership, Product, Role, Store, User, Workspace
from app.security import decode_access_token


def current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError("UNAUTHORIZED", "Sign in to continue.", 401)
    user_id = decode_access_token(authorization.split(" ", 1)[1].strip())
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        raise ApiError("UNAUTHORIZED", "Your session has expired. Sign in again.", 401)
    return user


@dataclass
class Access:
    workspace: Workspace
    role: Role | None  # None for Reference workspaces without a membership

    @property
    def is_reference(self) -> bool:
        return self.workspace.is_reference

    @property
    def can_forecast(self) -> bool:
        return self.is_reference or self.role in (Role.OWNER, Role.MANAGER)

    @property
    def can_edit(self) -> bool:
        return not self.is_reference and self.role in (Role.OWNER, Role.MANAGER)

    @property
    def effective_role(self) -> str:
        return self.role.value if self.role else "REFERENCE"


def workspace_access(db: Session, user: User, workspace_id: uuid.UUID) -> Access:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise not_found("Workspace")
    membership = db.scalar(select(Membership).where(
        Membership.workspace_id == workspace_id, Membership.user_id == user.id))
    if membership is None and not workspace.is_reference:
        raise not_found("Workspace")  # don't reveal workspaces the user can't see
    return Access(workspace, membership.role if membership else None)


def store_access(db: Session, user: User, store_id: uuid.UUID) -> tuple[Store, Access]:
    store = db.get(Store, store_id)
    if store is None:
        raise not_found("Store")
    try:
        return store, workspace_access(db, user, store.workspace_id)
    except ApiError:
        raise not_found("Store") from None


def product_in_store(db: Session, store: Store, product_id: uuid.UUID) -> Product:
    product = db.get(Product, product_id)
    if product is None or product.workspace_id != store.workspace_id:
        raise not_found("Product")
    return product


def require(condition: bool, message: str = "You don't have permission to do that.") -> None:
    if not condition:
        raise forbidden(message)
