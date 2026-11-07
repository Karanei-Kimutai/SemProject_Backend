"""Workspaces and stores."""

from __future__ import annotations

import uuid
import zoneinfo

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user, require, store_access, workspace_access
from app.errors import ApiError
from app.models import Membership, Role, Store, User, Workspace
from app.schemas import StoreIn, StoreOut, WorkspaceIn, WorkspaceOut

router = APIRouter(prefix="/v1", tags=["workspaces"])


def store_out(store: Store) -> StoreOut:
    return StoreOut(id=store.id, workspace_id=store.workspace_id, code=store.code, name=store.name,
                    country=store.country, subdivision=store.subdivision, timezone=store.timezone,
                    currency=store.currency, is_reference=store.workspace.is_reference,
                    history_start=store.history_start, history_end=store.history_end)


@router.get("/workspaces", response_model=list[WorkspaceOut], response_model_by_alias=True)
def list_workspaces(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[WorkspaceOut]:
    rows = db.execute(select(Workspace, Membership.role).outerjoin(
        Membership, (Membership.workspace_id == Workspace.id) & (Membership.user_id == user.id))
        .where((Membership.user_id == user.id) | Workspace.is_reference)
        .order_by(Workspace.is_reference.desc(), Workspace.name))
    return [WorkspaceOut(id=w.id, name=w.name, is_reference=w.is_reference,
                         role=role.value if role else "REFERENCE") for w, role in rows]


@router.post("/workspaces", response_model=WorkspaceOut, status_code=201, response_model_by_alias=True)
def create_workspace(body: WorkspaceIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> WorkspaceOut:
    workspace = Workspace(name=body.name, created_by=user.id)
    db.add(workspace)
    db.flush()
    db.add(Membership(workspace_id=workspace.id, user_id=user.id, role=Role.OWNER))
    db.commit()
    return WorkspaceOut(id=workspace.id, name=workspace.name, is_reference=False, role=Role.OWNER.value)


@router.get("/workspaces/{workspace_id}/stores", response_model=list[StoreOut], response_model_by_alias=True)
def list_stores(workspace_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[StoreOut]:
    access = workspace_access(db, user, workspace_id)
    return [store_out(s) for s in access.workspace.stores]


@router.post("/workspaces/{workspace_id}/stores", response_model=StoreOut, status_code=201, response_model_by_alias=True)
def create_store(workspace_id: uuid.UUID, body: StoreIn, user: User = Depends(current_user),
                 db: Session = Depends(get_db)) -> StoreOut:
    access = workspace_access(db, user, workspace_id)
    require(access.can_edit, "Only owners and managers can add stores.")
    try:
        zoneinfo.ZoneInfo(body.timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ApiError("INVALID_TIMEZONE", "Choose a valid timezone, for example Africa/Nairobi.") from None
    store = Store(workspace_id=workspace_id, code=body.code, name=body.name, country=body.country.upper(),
                  subdivision=body.subdivision, timezone=body.timezone, currency=body.currency.upper(),
                  model_id=f"own-{workspace_id}")
    db.add(store)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError("STORE_CODE_TAKEN", "Another store in this workspace already uses that code.", 409) from None
    db.refresh(store)
    return store_out(store)


@router.get("/stores/{store_id}", response_model=StoreOut, response_model_by_alias=True)
def get_store(store_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> StoreOut:
    store, _ = store_access(db, user, store_id)
    return store_out(store)
