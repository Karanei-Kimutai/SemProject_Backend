import datetime as dt

from conftest import auth, signup
from test_imports import confirm, csv_file, days_ago, import_file, shop  # noqa: F401  (fixture)

from app import worker
from app.models import Membership, Role, TrainingRun, User
from app.services import training


def load_history(client, shop, products: int, days: int, start_ago: int = 1):
    lines = [f"{days_ago(start_ago + k)},P{p:03d},Product {p},Cat{p % 3},{(k * (p + 1)) % 5},{10 + p}"
             for p in range(products) for k in range(days)]
    return confirm(client, shop, import_file(client, shop, csv_file(lines))["id"])


def train(client, shop):
    return client.post(f"/v1/stores/{shop['store']}/models/train", headers=shop["h"])


def test_not_eligible(client, fake_ml, shop):
    empty = client.get(f"/v1/stores/{shop['store']}/models/eligibility", headers=shop["h"]).json()
    assert empty["eligible"] is False and empty["checks"]["storeHistoryDays"]["value"] == 0
    load_history(client, shop, products=12, days=60)
    check = client.get(f"/v1/stores/{shop['store']}/models/eligibility", headers=shop["h"]).json()
    assert check["checks"]["storeHistoryDays"] == {"value": 60, "required": 120, "passed": False}
    assert check["checks"]["productsWithEnoughHistory"]["passed"] is True
    response = train(client, shop)
    assert response.status_code == 422 and response.json()["error"]["code"] == "NOT_ELIGIBLE"
    caps = client.get(f"/v1/stores/{shop['store']}/models/capabilities", headers=shop["h"]).json()
    assert caps["aiUnavailableReason"] == "NOT_TRAINED" and caps["training"] is None


def test_train_then_ai_forecast(client, fake_ml, shop):
    load_history(client, shop, products=40, days=130)
    started = train(client, shop)
    assert started.status_code == 202, started.text
    run = started.json()
    assert run["status"] == "QUEUED" and run["trigger"] == "MANUAL"
    again = train(client, shop)
    assert again.status_code == 200 and again.json()["id"] == run["id"]  # one run at a time
    caps = client.get(f"/v1/stores/{shop['store']}/models/capabilities", headers=shop["h"]).json()
    assert caps["aiUnavailableReason"] == "TRAINING" and caps["methods"] == ["BASELINE"]

    assert worker.run_once()  # submits to the ML service; the fake finishes at once, polled in the same pass
    sent = fake_ml.train_requests[-1]
    assert sent["workspaceId"] == shop["ws"] and sent["country"] == "KE" and len(sent["history"]) == 40 * 130
    assert set(sent["history"][0]) == {"storeId", "productId", "category", "date", "units", "price"}
    assert sent["history"][0]["storeId"] == "NAIROBI-01"

    done = client.get(f"/v1/training-runs/{run['id']}", headers=shop["h"]).json()
    assert (done["status"], done["outcome"]) == ("COMPLETED", "TRAINED")
    assert done["model"] == {"id": f"own-{shop['ws']}", "version": "1.0.0"} and done["horizons"] == [1, 7]
    assert done["gate"]["passed"] is True and done["horizonAccuracy"]["validated"] is True
    assert done["backtest"]["ai"]["rmse"] < done["backtest"]["naive_last_value"]["rmse"]

    caps = client.get(f"/v1/stores/{shop['store']}/models/capabilities", headers=shop["h"]).json()
    assert caps["aiAvailable"] is True and caps["methods"] == ["AI", "BASELINE", "COMPARE"]
    assert caps["activeModel"] == {"id": f"own-{shop['ws']}", "version": "1.0.0"}
    assert caps["training"]["outcome"] == "TRAINED"

    pid = client.get(f"/v1/stores/{shop['store']}/products", headers=shop["h"]).json()["items"][0]["id"]
    job = client.post("/v1/forecasts", json={"storeId": shop["store"], "productId": pid, "horizonDays": 7,
                                             "method": "COMPARE"}, headers=shop["h"])
    assert job.status_code == 202, job.text
    assert worker.run_once()
    request = fake_ml.requests[-1]
    assert request["model"] == {"id": f"own-{shop['ws']}"} and request["series"][0]["category"] == "Cat0"
    forecast = client.get(f"/v1/forecasts/{job.json()['forecastId']}", headers=shop["h"]).json()
    assert forecast["status"] == "COMPLETED" and forecast["model"]["version"] == "1.0.0"
    assert forecast["productBacktest"] is None  # stored per-product backtests exist only for the demo model
    listing = client.get(f"/v1/stores/{shop['store']}/models/training-runs", headers=shop["h"]).json()
    assert listing["total"] == 1


def test_gate_failed_keeps_baselines(client, fake_ml, shop):
    load_history(client, shop, products=40, days=130)
    fake_ml.train_outcome = "GATE_FAILED"
    run = train(client, shop).json()
    worker.run_once()
    done = client.get(f"/v1/training-runs/{run['id']}", headers=shop["h"]).json()
    assert (done["outcome"], done["model"], done["gate"]["passed"]) == ("GATE_FAILED", None, False)
    caps = client.get(f"/v1/stores/{shop['store']}/models/capabilities", headers=shop["h"]).json()
    assert caps["aiUnavailableReason"] == "GATE_FAILED" and caps["methods"] == ["BASELINE"]


def test_ml_restart_loses_job_then_resubmits(client, db, fake_ml, shop):
    load_history(client, shop, products=40, days=130)
    run = train(client, shop).json()
    original = fake_ml.train
    def lose_job(body):
        response = original(body)
        fake_ml.train_jobs.pop(response.json()["jobId"])
        return response
    fake_ml.train = lose_job
    worker.run_once()
    stored = db.get(TrainingRun, run["id"])
    db.refresh(stored)
    assert stored.status.value == "QUEUED" and stored.ml_job_id is None
    fake_ml.train = original
    worker.run_once()
    assert client.get(f"/v1/training-runs/{run['id']}", headers=shop["h"]).json()["outcome"] == "TRAINED"
    assert len(fake_ml.train_requests) == 2


def test_ml_down_then_retry(client, fake_ml, shop):
    load_history(client, shop, products=40, days=130)
    run = train(client, shop).json()
    original = fake_ml.handler
    import httpx
    def down(request):
        if request.url.path.endswith("/train"):
            return httpx.Response(503, json={})
        return original(request)
    from app.ml_client import MlClient, set_ml_client
    set_ml_client(MlClient("http://ml.test", "test-ml-token", 5, transport=httpx.MockTransport(down)))
    for _ in range(training.MAX_ATTEMPTS):
        worker.run_once()
    failed = client.get(f"/v1/training-runs/{run['id']}", headers=shop["h"]).json()
    assert failed["status"] == "FAILED" and failed["retryable"] is True
    assert failed["error"]["code"] == "ML_SERVICE_UNAVAILABLE"
    set_ml_client(MlClient("http://ml.test", "test-ml-token", 5, transport=httpx.MockTransport(original)))
    assert client.post(f"/v1/training-runs/{run['id']}/retry", headers=shop["h"]).status_code == 202
    worker.run_once()
    assert client.get(f"/v1/training-runs/{run['id']}", headers=shop["h"]).json()["outcome"] == "TRAINED"


def test_permissions(client, db, fake_ml, shop, reference):
    demo = client.post(f"/v1/stores/{reference['store']}/models/train", headers=shop["h"])
    assert demo.status_code == 409 and demo.json()["error"]["code"] == "REFERENCE_MODEL_FIXED"
    viewer_tokens = signup(client, "viewer@example.com")
    viewer = db.query(User).filter_by(email="viewer@example.com").one()
    db.add(Membership(workspace_id=shop["ws"], user_id=viewer.id, role=Role.VIEWER))
    db.commit()
    h = auth(viewer_tokens)
    assert client.get(f"/v1/stores/{shop['store']}/models/eligibility", headers=h).status_code == 200
    assert client.post(f"/v1/stores/{shop['store']}/models/train", headers=h).status_code == 403
    stranger = auth(signup(client, "stranger@example.com"))
    assert client.get(f"/v1/stores/{shop['store']}/models/eligibility", headers=stranger).status_code == 404


def test_weekly_retrain_only_when_history_changed(client, db, fake_ml, shop):
    load_history(client, shop, products=40, days=130, start_ago=2)
    run = train(client, shop).json()
    worker.run_once()
    stored = db.get(TrainingRun, run["id"])
    stored.created_at = stored.created_at - dt.timedelta(days=8)
    db.commit()
    assert training.schedule_retraining(db) == 0  # nothing new since training
    confirm(client, shop, import_file(client, shop, csv_file([f"{days_ago(1)},P000,Product 0,Cat0,3,10"]))["id"])
    assert training.schedule_retraining(db) == 1
    assert training.schedule_retraining(db) == 0  # already queued
    worker.run_once()
    runs = client.get(f"/v1/stores/{shop['store']}/models/training-runs", headers=shop["h"]).json()["items"]
    assert [(r["trigger"], r["outcome"]) for r in runs] == [("SCHEDULED", "TRAINED"), ("MANUAL", "TRAINED")]
    caps = client.get(f"/v1/stores/{shop['store']}/models/capabilities", headers=shop["h"]).json()
    assert caps["activeModel"]["version"] == "2.0.0"
