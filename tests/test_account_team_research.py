import datetime as dt
import re

from conftest import auth, signup

from app import email, worker
from app.models import Invitation, PasswordReset, User


def _code() -> str:
    return re.search(r"code is (\d{8})", email.outbox[-1].get_content()).group(1)


# ---- password reset and change ------------------------------------------------------------

def test_password_reset_flow(client):
    old = signup(client, "ann@example.com", "first-password")
    assert client.post("/v1/auth/password-reset/request", json={"email": "ANN@example.com"}).status_code == 202
    assert email.outbox[-1]["To"] == "ann@example.com"
    code = _code()
    wrong = client.post("/v1/auth/password-reset/confirm", json={
        "email": "ann@example.com", "code": f"{(int(code) + 1) % 10**8:08d}", "newPassword": "second-password"})
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "INVALID_RESET_CODE"
    done = client.post("/v1/auth/password-reset/confirm", json={
        "email": "ann@example.com", "code": code, "newPassword": "second-password"})
    assert done.status_code == 200 and "accessToken" in done.json()
    # Old sessions are signed out, the old password stops working, the code works once.
    assert client.post("/v1/auth/refresh", json={"refreshToken": old["refreshToken"]}).status_code == 401
    assert client.post("/v1/auth/login", json={"email": "ann@example.com", "password": "first-password"}).status_code == 401
    assert client.post("/v1/auth/login", json={"email": "ann@example.com", "password": "second-password"}).status_code == 200
    reuse = client.post("/v1/auth/password-reset/confirm", json={
        "email": "ann@example.com", "code": code, "newPassword": "third-password"})
    assert reuse.status_code == 400


def test_reset_does_not_reveal_accounts_and_locks_after_attempts(client, db):
    assert client.post("/v1/auth/password-reset/request", json={"email": "ghost@example.com"}).json() == {
        "status": "SENT_IF_ACCOUNT_EXISTS"}
    assert email.outbox == []
    signup(client, "ann@example.com", "first-password")
    client.post("/v1/auth/password-reset/request", json={"email": "ann@example.com"})
    code = _code()
    for _ in range(5):
        client.post("/v1/auth/password-reset/confirm", json={"email": "ann@example.com", "code": "00000000"
                    if code != "00000000" else "11111111", "newPassword": "second-password"})
    locked = client.post("/v1/auth/password-reset/confirm", json={
        "email": "ann@example.com", "code": code, "newPassword": "second-password"})
    assert locked.status_code == 400  # even the right code, after 5 wrong attempts


def test_reset_code_expires_and_only_newest_works(client, db):
    signup(client, "ann@example.com", "first-password")
    client.post("/v1/auth/password-reset/request", json={"email": "ann@example.com"})
    first = _code()
    client.post("/v1/auth/password-reset/request", json={"email": "ann@example.com"})
    second = _code()
    body = {"email": "ann@example.com", "newPassword": "second-password"}
    if first != second:
        assert client.post("/v1/auth/password-reset/confirm", json={**body, "code": first}).status_code == 400
    db.query(PasswordReset).update({"expires_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)})
    db.commit()
    assert client.post("/v1/auth/password-reset/confirm", json={**body, "code": second}).status_code == 400


def test_change_password(client):
    tokens = signup(client, "ann@example.com", "first-password")
    bad = client.post("/v1/auth/change-password", json={"currentPassword": "nope-nope-nope",
                                                         "newPassword": "second-password"}, headers=auth(tokens))
    assert bad.status_code == 401
    ok = client.post("/v1/auth/change-password", json={"currentPassword": "first-password",
                                                        "newPassword": "second-password"}, headers=auth(tokens))
    assert ok.status_code == 200
    assert client.post("/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]}).status_code == 401


def test_rate_limits(client):
    signup(client, "ann@example.com", "first-password")
    codes = [client.post("/v1/auth/login", json={"email": "ann@example.com", "password": "wrong-password"}).status_code
             for _ in range(6)]
    assert codes == [401] * 5 + [429]
    limited = client.post("/v1/auth/login", json={"email": "ann@example.com", "password": "first-password"})
    assert limited.json()["error"]["code"] == "RATE_LIMITED" and int(limited.headers["Retry-After"]) > 0
    # Other accounts aren't blocked by this one's limit.
    signup(client, "bob@example.com", "first-password")
    assert client.post("/v1/auth/login", json={"email": "bob@example.com", "password": "first-password"}).status_code == 200


# ---- team -----------------------------------------------------------------------------------

def _workspace(client, h):
    return client.post("/v1/workspaces", json={"name": "Duka"}, headers=h).json()["id"]


def test_invite_accept_and_manage(client, db):
    owner = auth(signup(client, "owner@example.com"))
    ws = _workspace(client, owner)
    invitation = client.post(f"/v1/workspaces/{ws}/invitations", json={"email": "Bob@Example.com", "role": "MANAGER"},
                             headers=owner)
    assert invitation.status_code == 201 and invitation.json()["status"] == "PENDING"
    assert email.outbox[-1]["To"] == "bob@example.com" and "Duka" in email.outbox[-1]["Subject"]
    assert len(client.get(f"/v1/workspaces/{ws}/invitations", headers=owner).json()) == 1

    bob = auth(signup(client, "bob@example.com"))  # signs up after being invited
    mine = client.get("/v1/me/invitations", headers=bob).json()
    assert [(i["workspaceName"], i["role"], i["invitedBy"]) for i in mine] == [("Duka", "MANAGER", "U")]
    accepted = client.post(f"/v1/invitations/{mine[0]['id']}/accept", headers=bob)
    assert accepted.json()["status"] == "ACCEPTED"
    assert client.post(f"/v1/invitations/{mine[0]['id']}/accept", headers=bob).status_code == 409
    roles = {m["email"]: m["role"] for m in client.get(f"/v1/workspaces/{ws}/members", headers=bob).json()}
    assert roles == {"owner@example.com": "OWNER", "bob@example.com": "MANAGER"}
    assert any(w["workspaceId"] == ws for w in client.get("/v1/me", headers=bob).json()["workspaces"])

    # Managers can't manage the team; owners can change roles and remove members.
    assert client.post(f"/v1/workspaces/{ws}/invitations", json={"email": "c@example.com"}, headers=bob).status_code == 403
    bob_id = next(m["userId"] for m in client.get(f"/v1/workspaces/{ws}/members", headers=owner).json()
                  if m["email"] == "bob@example.com")
    patched = client.patch(f"/v1/workspaces/{ws}/members/{bob_id}", json={"role": "VIEWER"}, headers=owner)
    assert patched.json()["role"] == "VIEWER"
    again = client.post(f"/v1/workspaces/{ws}/invitations", json={"email": "bob@example.com"}, headers=owner)
    assert again.json()["error"]["code"] == "ALREADY_MEMBER"
    assert client.delete(f"/v1/workspaces/{ws}/members/{bob_id}", headers=owner).status_code == 204
    assert client.get(f"/v1/workspaces/{ws}/stores", headers=bob).status_code == 404


def test_last_owner_is_protected_and_members_can_leave(client):
    owner_tokens = signup(client, "owner@example.com")
    owner = auth(owner_tokens)
    ws = _workspace(client, owner)
    owner_id = client.get("/v1/me", headers=owner).json()["id"]
    assert client.delete(f"/v1/workspaces/{ws}/members/{owner_id}", headers=owner).json()["error"]["code"] == "LAST_OWNER"
    assert client.patch(f"/v1/workspaces/{ws}/members/{owner_id}", json={"role": "VIEWER"},
                        headers=owner).json()["error"]["code"] == "LAST_OWNER"
    client.post(f"/v1/workspaces/{ws}/invitations", json={"email": "bob@example.com", "role": "VIEWER"}, headers=owner)
    bob = auth(signup(client, "bob@example.com"))
    client.post(f"/v1/invitations/{client.get('/v1/me/invitations', headers=bob).json()[0]['id']}/accept", headers=bob)
    bob_id = client.get("/v1/me", headers=bob).json()["id"]
    assert client.delete(f"/v1/workspaces/{ws}/members/{bob_id}", headers=bob).status_code == 204  # leave


def test_decline_revoke_expire(client, db):
    owner = auth(signup(client, "owner@example.com"))
    ws = _workspace(client, owner)
    bob = auth(signup(client, "bob@example.com"))
    first = client.post(f"/v1/workspaces/{ws}/invitations", json={"email": "bob@example.com"}, headers=owner).json()
    assert client.post(f"/v1/invitations/{first['id']}/decline", headers=bob).json()["status"] == "DECLINED"
    second = client.post(f"/v1/workspaces/{ws}/invitations", json={"email": "bob@example.com"}, headers=owner).json()
    assert client.delete(f"/v1/workspaces/{ws}/invitations/{second['id']}", headers=owner).status_code == 204
    assert client.get("/v1/me/invitations", headers=bob).json() == []
    third = client.post(f"/v1/workspaces/{ws}/invitations", json={"email": "bob@example.com"}, headers=owner).json()
    db.query(Invitation).filter_by(id=third["id"]).update(
        {"expires_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)})
    db.commit()
    assert client.get("/v1/me/invitations", headers=bob).json() == []
    assert client.post(f"/v1/invitations/{third['id']}/accept", headers=bob).status_code == 409
    # Someone else can't accept an invitation addressed to Bob.
    fourth = client.post(f"/v1/workspaces/{ws}/invitations", json={"email": "bob@example.com"}, headers=owner).json()
    eve = auth(signup(client, "eve@example.com"))
    assert client.post(f"/v1/invitations/{fourth['id']}/accept", headers=eve).status_code == 404


# ---- research export --------------------------------------------------------------------------

def test_research_export_and_summary(client, db, fake_ml, reference):
    tokens = signup(client, "researcher@example.com")
    h = auth(tokens)
    store, product = str(reference["store"]), str(reference["product"])
    assert client.get("/v1/research/summary", params={"storeId": store}, headers=h).status_code == 403
    db.query(User).update({"is_research": True})
    db.commit()
    for as_of, horizon in [("2016-05-08", 1), ("2016-05-01", 7), ("2016-05-22", 1)]:
        client.post("/v1/forecasts", json={"storeId": store, "productId": product, "asOfDate": as_of,
                                           "horizonDays": horizon, "method": "COMPARE"}, headers=h)
        assert worker.run_once()
    summary = client.get("/v1/research/summary", params={"storeId": store}, headers=h).json()
    # The true forecast (2016-05-22) has no actuals yet, so 1 + 7 points from the two replays count.
    assert summary["runs"] == 2 and summary["overall"]["n"] == 8
    assert summary["byDayAhead"]["1"]["n"] == 2 and set(summary["byDayAhead"]) == {str(d) for d in range(1, 8)}
    assert summary["overall"]["ai"]["mae"] >= 0 and "naive_last_value" in summary["overall"]
    export = client.get("/v1/research/export", params={"storeId": store}, headers=h)
    lines = export.text.strip().splitlines()
    assert lines[0].startswith("forecast_id,store,sku,demand_class,as_of_date") and len(lines) == 1 + 1 + 7 + 1
    replay = next(line for line in lines if ",2016-05-08," in line).split(",")
    assert replay[13:19] == ["57.1206", "77.0000", "59.0000", "72.0000", "14.8794", "5.0000"]
