import datetime as dt
import re

from conftest import auth, signup

from app import email, worker
from app.models import PasswordReset, User


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
