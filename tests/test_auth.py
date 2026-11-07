from conftest import auth, signup


def test_register_login_and_me(client):
    tokens = signup(client)
    assert tokens["tokenType"] == "Bearer" and tokens["expiresIn"] == 900
    me = client.get("/v1/me", headers=auth(tokens)).json()
    assert me["email"] == "user@example.com" and me["isResearch"] is False
    assert client.post("/v1/auth/login", json={"email": "USER@example.com", "password": "long-enough-pw"}).status_code == 200


def test_bad_credentials_and_duplicates(client):
    signup(client)
    wrong = client.post("/v1/auth/login", json={"email": "user@example.com", "password": "nope-nope-nope"})
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "INVALID_CREDENTIALS"
    unknown = client.post("/v1/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"})
    assert unknown.json()["error"]["code"] == "INVALID_CREDENTIALS"
    again = client.post("/v1/auth/register", json={"email": "user@example.com", "password": "long-enough-pw",
                                                    "displayName": "U"})
    assert again.status_code == 409
    short = client.post("/v1/auth/register", json={"email": "x@example.com", "password": "short", "displayName": "U"})
    assert short.status_code == 422 and short.json()["error"]["code"] == "INVALID_REQUEST"


def test_requires_token(client):
    assert client.get("/v1/me").json()["error"]["code"] == "UNAUTHORIZED"
    assert client.get("/v1/me", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_refresh_rotates_and_detects_reuse(client):
    first = signup(client)
    second = client.post("/v1/auth/refresh", json={"refreshToken": first["refreshToken"]}).json()
    assert second["refreshToken"] != first["refreshToken"]
    # Reusing the rotated token ends every session, including the new one.
    reuse = client.post("/v1/auth/refresh", json={"refreshToken": first["refreshToken"]})
    assert reuse.status_code == 401
    assert client.post("/v1/auth/refresh", json={"refreshToken": second["refreshToken"]}).status_code == 401


def test_logout_revokes_refresh_token(client):
    tokens = signup(client)
    assert client.post("/v1/auth/logout", json={"refreshToken": tokens["refreshToken"]}).status_code == 204
    assert client.post("/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]}).status_code == 401
