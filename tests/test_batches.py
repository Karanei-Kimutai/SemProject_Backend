import uuid

from conftest import auth, signup

from app import worker
from app.models import ForecastRun, Store
from app.services import forecasting


def _start(client, h, reference, **body):
    return client.post(f"/v1/stores/{reference['store']}/forecast-batches", json={"horizonDays": 7, **body}, headers=h)


def _drain():
    while worker.run_once():
        pass


def test_batch_forecasts_every_product_with_history(client, fake_ml, reference):
    h = auth(signup(client))
    created = _start(client, h, reference)
    assert created.status_code == 202, created.text
    batch = created.json()
    assert batch["status"] == "RUNNING" and batch["total"] == 2 and batch["pending"] == 2
    assert batch["method"] == "COMPARE" and batch["asOfDate"] == "2016-05-22" and batch["outdated"] is False

    _drain()
    done = client.get(f"/v1/forecast-batches/{batch['batchId']}", headers=h).json()
    assert done["status"] == "COMPLETED" and done["completed"] == 2 and done["failed"] == 0
    assert done["finishedAt"] is not None

    items = client.get(f"/v1/forecast-batches/{batch['batchId']}/items", headers=h).json()
    assert items["total"] == 2 and [i["sku"] for i in items["items"]] == ["FOODS_3_090", "FOODS_NEW"]
    first = items["items"][0]
    assert first["status"] == "COMPLETED" and len(first["points"]) == 7
    assert first["points"][0] == {"date": "2016-05-23", "ai": 1.5, "baseline": 2.0, "seasonalNaive": 3.0,
                                  "actual": None}
    # Each item is an ordinary forecast that opens on its own.
    single = client.get(f"/v1/forecasts/{first['forecastId']}", headers=h).json()
    assert single["status"] == "COMPLETED" and single["productBacktest"] is None

    latest = client.get(f"/v1/stores/{reference['store']}/forecast-batches/latest", headers=h).json()
    assert latest["batchId"] == batch["batchId"]


def test_items_search_and_paging(client, fake_ml, reference):
    h = auth(signup(client))
    batch = _start(client, h, reference).json()
    _drain()
    url = f"/v1/forecast-batches/{batch['batchId']}/items"
    assert [i["sku"] for i in client.get(url, params={"q": "new"}, headers=h).json()["items"]] == ["FOODS_NEW"]
    page = client.get(url, params={"pageSize": 1, "page": 2}, headers=h).json()
    assert page["total"] == 2 and [i["sku"] for i in page["items"]] == ["FOODS_NEW"]


def test_a_running_batch_is_returned_instead_of_duplicated(client, fake_ml, reference):
    h = auth(signup(client))
    first = _start(client, h, reference).json()
    again = _start(client, h, reference)
    assert again.status_code == 200 and again.json()["batchId"] == first["batchId"]
    other_length = _start(client, h, reference, horizonDays=1)
    assert other_length.status_code == 202 and other_length.json()["batchId"] != first["batchId"]


def test_chosen_products_only(client, fake_ml, reference):
    h = auth(signup(client))
    batch = _start(client, h, reference, productIds=[str(reference["short"])]).json()
    assert batch["total"] == 1
    assert _start(client, h, reference, productIds=[str(uuid.uuid4())]).status_code == 404


def test_single_forecasts_go_before_batch_runs(client, db, fake_ml, reference):
    h = auth(signup(client))
    _start(client, h, reference)
    single = client.post("/v1/forecasts", json={"storeId": str(reference["store"]), "productId": str(reference["product"]),
                                                "horizonDays": 1}, headers=h).json()
    claimed = forecasting.claim_next(db)
    assert claimed == uuid.UUID(single["jobId"])


def test_history_list_can_leave_out_batch_runs(client, fake_ml, reference):
    h = auth(signup(client))
    _start(client, h, reference)
    client.post("/v1/forecasts", json={"storeId": str(reference["store"]), "productId": str(reference["product"]),
                                       "horizonDays": 1}, headers=h)
    params = {"storeId": str(reference["store"])}
    assert client.get("/v1/forecasts", params=params, headers=h).json()["total"] == 3
    assert client.get("/v1/forecasts", params={**params, "batch": "false"}, headers=h).json()["total"] == 1
    assert client.get("/v1/forecasts", params={**params, "batch": "true"}, headers=h).json()["total"] == 2


def test_failed_runs_can_be_retried(client, fake_ml, reference):
    h = auth(signup(client))
    fake_ml.fail_with = 503
    batch = _start(client, h, reference).json()
    _drain()
    failed = client.get(f"/v1/forecast-batches/{batch['batchId']}", headers=h).json()
    assert failed["status"] == "COMPLETED" and failed["failed"] == 2
    fake_ml.fail_with = None
    retried = client.post(f"/v1/forecast-batches/{batch['batchId']}/retry", headers=h)
    assert retried.status_code == 202 and retried.json()["pending"] == 2
    _drain()
    assert client.get(f"/v1/forecast-batches/{batch['batchId']}", headers=h).json()["completed"] == 2
    again = client.post(f"/v1/forecast-batches/{batch['batchId']}/retry", headers=h)
    assert again.status_code == 409 and again.json()["error"]["code"] == "NOT_RETRYABLE"


def test_new_sales_mark_the_batch_outdated(client, db, fake_ml, reference):
    h = auth(signup(client))
    batch = _start(client, h, reference).json()
    _drain()
    store = db.get(Store, reference["store"])
    store.history_version += 1
    db.commit()
    assert client.get(f"/v1/forecast-batches/{batch['batchId']}", headers=h).json()["outdated"] is True


def test_demo_batches_are_private_per_user(client, fake_ml, reference):
    alice, bob = auth(signup(client, "a@example.com")), auth(signup(client, "b@example.com"))
    batch = _start(client, alice, reference).json()
    assert client.get(f"/v1/forecast-batches/{batch['batchId']}", headers=bob).status_code == 404
    assert client.get(f"/v1/stores/{reference['store']}/forecast-batches/latest", headers=bob).status_code == 404


def test_a_store_without_sales_has_nothing_to_forecast(client, fake_ml):
    h = auth(signup(client))
    workspace = client.post("/v1/workspaces", json={"name": "Shop"}, headers=h).json()
    store = client.post(f"/v1/workspaces/{workspace['id']}/stores",
                        json={"code": "S1", "name": "Shop", "country": "KE", "timezone": "Africa/Nairobi",
                              "currency": "KES"}, headers=h).json()
    response = client.post(f"/v1/stores/{store['id']}/forecast-batches", json={"horizonDays": 7}, headers=h)
    assert response.status_code == 422 and response.json()["error"]["code"] == "NO_PRODUCTS"
    missing = client.get(f"/v1/stores/{store['id']}/forecast-batches/latest", headers=h)
    assert missing.status_code == 404


def test_batch_runs_are_ordinary_runs(db, client, fake_ml, reference):
    h = auth(signup(client))
    batch = _start(client, h, reference).json()
    runs = db.query(ForecastRun).filter(ForecastRun.batch_id == uuid.UUID(batch["batchId"])).all()
    assert len(runs) == 2 and all(r.horizon_days == 7 and r.is_replay is False for r in runs)
