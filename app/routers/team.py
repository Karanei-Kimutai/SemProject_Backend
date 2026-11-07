"""Workspace members and invitations (spec §8.12). Only owners manage the team.

Invitations are accepted in the app: a signed-in user sees invitations addressed to their email
(`GET /v1/me/invitations`) and accepts or declines them. The email is only a notification.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import email
from app.config import get_settings
from app.db import get_db
from app.deps import current_user, require, workspace_access
from app.errors import ApiError, not_found
from app.models import Invitation, Membership, Role, User, Workspace
from app.schemas import InvitationIn, InvitationOut, MemberOut, MemberPatch

router = APIRouter(prefix="/v1", tags=["team"])


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _owner_workspace(db: Session, user: User, workspace_id: uuid.UUID) -> Workspace:
    access = workspace_access(db, user, workspace_id)
    require(not access.is_reference, "The demo workspace has no team.")
    require(access.role == Role.OWNER, "Only owners can manage the team.")
    return access.workspace


def _owners(db: Session, workspace_id: uuid.UUID) -> int:
    return db.scalar(select(func.count()).select_from(Membership).where(
        Membership.workspace_id == workspace_id, Membership.role == Role.OWNER))


def invitation_out(db: Session, invitation: Invitation) -> InvitationOut:
    workspace = db.get(Workspace, invitation.workspace_id)
    inviter = db.get(User, invitation.invited_by) if invitation.invited_by else None
    status = invitation.status
    if status == "PENDING" and invitation.expires_at <= _now():
        status = "EXPIRED"
    return InvitationOut(id=invitation.id, workspace_id=invitation.workspace_id, workspace_name=workspace.name,
                         email=invitation.email, role=invitation.role.value, status=status,
                         invited_by=inviter.display_name if inviter else None, created_at=invitation.created_at,
                         expires_at=invitation.expires_at)


@router.get("/workspaces/{workspace_id}/members", response_model=list[MemberOut], response_model_by_alias=True)
def list_members(workspace_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    access = workspace_access(db, user, workspace_id)
    require(not access.is_reference, "The demo workspace has no team.")
    rows = db.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                      .where(Membership.workspace_id == workspace_id).order_by(Membership.created_at))
    return [MemberOut(user_id=u.id, email=u.email, display_name=u.display_name, role=m.role.value,
                      joined_at=m.created_at) for m, u in rows]


@router.patch("/workspaces/{workspace_id}/members/{user_id}", response_model=MemberOut, response_model_by_alias=True)
def change_role(workspace_id: uuid.UUID, user_id: uuid.UUID, body: MemberPatch, user: User = Depends(current_user),
                db: Session = Depends(get_db)) -> MemberOut:
    _owner_workspace(db, user, workspace_id)
    membership = db.get(Membership, (workspace_id, user_id))
    if membership is None:
        raise not_found("Member")
    new_role = Role(body.role)
    if membership.role == Role.OWNER and new_role != Role.OWNER and _owners(db, workspace_id) == 1:
        raise ApiError("LAST_OWNER", "A workspace needs at least one owner. Make someone else an owner first.", 409)
    membership.role = new_role
    db.commit()
    member = db.get(User, user_id)
    return MemberOut(user_id=member.id, email=member.email, display_name=member.display_name, role=new_role.value,
                     joined_at=membership.created_at)


@router.delete("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(workspace_id: uuid.UUID, user_id: uuid.UUID, user: User = Depends(current_user),
                  db: Session = Depends(get_db)) -> None:
    """Owners remove anyone; anyone can remove themselves (leave)."""
    if user_id != user.id:
        _owner_workspace(db, user, workspace_id)
    else:
        workspace_access(db, user, workspace_id)
    membership = db.get(Membership, (workspace_id, user_id))
    if membership is None:
        raise not_found("Member")
    if membership.role == Role.OWNER and _owners(db, workspace_id) == 1:
        raise ApiError("LAST_OWNER", "A workspace needs at least one owner. Make someone else an owner first.", 409)
    db.delete(membership)
    db.commit()


@router.post("/workspaces/{workspace_id}/invitations", response_model=InvitationOut, status_code=201,
             response_model_by_alias=True)
def invite(workspace_id: uuid.UUID, body: InvitationIn, user: User = Depends(current_user),
           db: Session = Depends(get_db)) -> InvitationOut:
    workspace = _owner_workspace(db, user, workspace_id)
    address = body.email.lower()
    existing_user = db.scalar(select(User).where(User.email == address))
    if existing_user and db.get(Membership, (workspace_id, existing_user.id)):
        raise ApiError("ALREADY_MEMBER", "That person is already in this workspace.", 409)
    for old in db.scalars(select(Invitation).where(Invitation.workspace_id == workspace_id,
                                                   Invitation.email == address, Invitation.status == "PENDING")):
        old.status, old.responded_at = "REVOKED", _now()  # a new invitation replaces the old one
    invitation = Invitation(workspace_id=workspace_id, email=address, role=Role(body.role), invited_by=user.id,
                            expires_at=_now() + dt.timedelta(days=get_settings().invitation_days))
    db.add(invitation)
    db.commit()
    settings = get_settings()
    email.send(address, f"{user.display_name} invited you to {workspace.name}",
               f"{user.display_name} invited you to join \"{workspace.name}\" as {body.role.lower()} in "
               f"{settings.app_name}.\n\nSign in (or create an account) with this email address in the app, then "
               f"open Settings → Invitations to accept. The invitation expires in {settings.invitation_days} days.")
    return invitation_out(db, invitation)


@router.get("/workspaces/{workspace_id}/invitations", response_model=list[InvitationOut], response_model_by_alias=True)
def list_invitations(workspace_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _owner_workspace(db, user, workspace_id)
    invitations = db.scalars(select(Invitation).where(Invitation.workspace_id == workspace_id,
                                                      Invitation.status == "PENDING")
                             .order_by(Invitation.created_at.desc()))
    return [invitation_out(db, i) for i in invitations]


@router.delete("/workspaces/{workspace_id}/invitations/{invitation_id}", status_code=204)
def revoke_invitation(workspace_id: uuid.UUID, invitation_id: uuid.UUID, user: User = Depends(current_user),
                      db: Session = Depends(get_db)) -> None:
    _owner_workspace(db, user, workspace_id)
    invitation = db.get(Invitation, invitation_id)
    if invitation is None or invitation.workspace_id != workspace_id:
        raise not_found("Invitation")
    if invitation.status == "PENDING":
        invitation.status, invitation.responded_at = "REVOKED", _now()
        db.commit()


@router.get("/me/invitations", response_model=list[InvitationOut], response_model_by_alias=True)
def my_invitations(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[InvitationOut]:
    invitations = db.scalars(select(Invitation).where(Invitation.email == user.email, Invitation.status == "PENDING",
                                                      Invitation.expires_at > _now())
                             .order_by(Invitation.created_at.desc()))
    return [invitation_out(db, i) for i in invitations]


def _my_pending(db: Session, user: User, invitation_id: uuid.UUID) -> Invitation:
    invitation = db.get(Invitation, invitation_id)
    if invitation is None or invitation.email != user.email:
        raise not_found("Invitation")
    if invitation.status != "PENDING" or invitation.expires_at <= _now():
        raise ApiError("INVITATION_NOT_PENDING", "This invitation is no longer valid. Ask for a new one.", 409)
    return invitation


@router.post("/invitations/{invitation_id}/accept", response_model=InvitationOut, response_model_by_alias=True)
def accept(invitation_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> InvitationOut:
    invitation = _my_pending(db, user, invitation_id)
    membership = db.get(Membership, (invitation.workspace_id, user.id))
    if membership is None:
        db.add(Membership(workspace_id=invitation.workspace_id, user_id=user.id, role=invitation.role))
    invitation.status, invitation.responded_at = "ACCEPTED", _now()
    db.commit()
    return invitation_out(db, invitation)


@router.post("/invitations/{invitation_id}/decline", response_model=InvitationOut, response_model_by_alias=True)
def decline(invitation_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> InvitationOut:
    invitation = _my_pending(db, user, invitation_id)
    invitation.status, invitation.responded_at = "DECLINED", _now()
    db.commit()
    return invitation_out(db, invitation)
