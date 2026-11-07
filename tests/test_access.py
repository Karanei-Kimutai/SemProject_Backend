from conftest import auth, signup

from app.models import Membership, Role, User


def test_reference_is_visible_and_read_only(client, reference):
    h = auth(signup(client))
    workspaces = client.get("/v1/workspaces", headers=h).json()
    assert [(w["isReference"], w["role"]) for w in workspaces] == [(True, "REFERENCE")]
    sid, pid = reference["store"], reference["product"]
    assert client.get(f"/v1/stores/{sid}/products", headers=h).json()["total"] == 2
    patch = client.patch(f"/v1/stores/{sid}/products/{pid}", json={"name": "x"}, headers=h)
    assert patch.status_code == 403
    create = client.post(f"/v1/stores/{sid}/products", json={"sku": "NEW", "name": "New"}, headers=h)
    assert create.status_code == 403


def test_own_workspace_is_private(client):
    owner = auth(signup(client, "owner@example.com"))
    other = auth(signup(client, "other@example.com"))
    ws = client.post("/v1/workspaces", json={"name": "Duka"}, headers=owner).json()
    assert ws["role"] == "OWNER"
    store = client.post(f"/v1/workspaces/{ws['id']}/stores", json={
        "code": "MAIN", "name": "Main", "country": "KE", "timezone": "Africa/Nairobi", "currency": "KES"},
        headers=owner)
    assert store.status_code == 201, store.text
    sid = store.json()["id"]
    assert client.get(f"/v1/stores/{sid}", headers=other).status_code == 404
    assert client.get(f"/v1/workspaces/{ws['id']}/stores", headers=other).status_code == 404
    product = client.post(f"/v1/stores/{sid}/products", json={"sku": "MILK-500", "name": "Milk 500ml"}, headers=owner)
    assert product.status_code == 201
    dup = client.post(f"/v1/stores/{sid}/products", json={"sku": "MILK-500", "name": "Again"}, headers=owner)
    assert dup.status_code == 409


def test_viewer_cannot_forecast_or_edit(client, db, reference):
    owner = auth(signup(client, "owner@example.com"))
    viewer_tokens = signup(client, "viewer@example.com")
    ws = client.post("/v1/workspaces", json={"name": "Duka"}, headers=owner).json()
    store = client.post(f"/v1/workspaces/{ws['id']}/stores", json={
        "code": "MAIN", "name": "Main", "country": "KE", "timezone": "Africa/Nairobi", "currency": "KES"},
        headers=owner).json()
    product = client.post(f"/v1/stores/{store['id']}/products", json={"sku": "A", "name": "A"}, headers=owner).json()
    viewer = db.query(User).filter_by(email="viewer@example.com").one()
    db.add(Membership(workspace_id=ws["id"], user_id=viewer.id, role=Role.VIEWER))
    db.commit()
    h = auth(viewer_tokens)
    assert client.get(f"/v1/stores/{store['id']}/products", headers=h).status_code == 200
    assert client.patch(f"/v1/stores/{store['id']}/products/{product['id']}", json={"name": "B"},
                        headers=h).status_code == 403
    forecast = client.post("/v1/forecasts", json={"storeId": store["id"], "productId": product["id"],
                                                  "horizonDays": 1}, headers=h)
    assert forecast.status_code == 403


def test_bad_timezone_rejected(client):
    h = auth(signup(client))
    ws = client.post("/v1/workspaces", json={"name": "Duka"}, headers=h).json()
    bad = client.post(f"/v1/workspaces/{ws['id']}/stores", json={
        "code": "MAIN", "name": "Main", "country": "KE", "timezone": "Mars/Base", "currency": "KES"}, headers=h)
    assert bad.json()["error"]["code"] == "INVALID_TIMEZONE"
