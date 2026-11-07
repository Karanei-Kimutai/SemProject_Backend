import datetime as dt
import json

from conftest import FIXTURES, auth, signup

from app import worker
from app.models import ForecastRun, SalesDaily, User
from app.services import forecasting


def _create(client, h, reference, **body):
    payload = {"storeId": str(reference["store"]), "productId": str(reference["product"]), "horizonDays": 1,
               "method": "COMPARE", **body}
    return client.post("/v1/forecasts", json=payload, headers=h)


def _run(client, h, reference, **body):
    created = _create(client, h, reference, **body)
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "QUEUED"
    assert worker.run_once()
    return client.get(f"/v1/forecasts/{created.json()['forecastId']}", headers=h).json()


def test_replay_end_to_end(client, fake_ml, reference):
    h = auth(signup(client))
    result = _run(client, h, reference, asOfDate="2016-05-08")
    assert result["status"] == "COMPLETED" and result["isReplay"] is True
    assert result["model"]["version"] == "1.1.0-bt"
    # Real captured ML output (57.1 vs naive 77) with the actual attached by the backend.
    assert result["points"] == [{"date": "2016-05-09", "ai": 57.1206, "baseline": 77.0, "seasonalNaive": 59.0,
                                 "actual": 72.0}]
    assert result["runMetrics"]["ai"]["mae"] == 72.0 - 57.1206
    assert result["runMetrics"]["baseline"]["mae"] == 5.0
    assert result["productBacktest"]["n"] == 28
    assert len(result["history"]) == 28 and result["history"][-1]["date"] == "2016-05-08"


def test_ml_request_matches_contract(client, fake_ml, reference):
    h = auth(signup(client))
    _run(client, h, reference, asOfDate="2016-05-08")
    sent = fake_ml.requests[-1]
    captured = json.loads((FIXTURES / "replay_request.json").read_text())
    assert sent["model"] == captured["model"] and sent["asOfDate"] == captured["asOfDate"]
    assert sent["baselines"] == captured["baselines"] and sent["includeAi"] is True
    series = sent["series"][0]
    assert series["productId"] == "FOODS_3_090" and series["storeId"] == "CA_1"
    assert series["firstPriceDate"] == "2011-01-29"
    assert len(series["history"]) == 365 and series["history"][-1]["date"] == "2016-05-08"
    assert series["futurePrices"] == [{"date": "2016-05-09", "price": 1.6}]
    assert set(series["history"][0]) == set(captured["series"][0]["history"][0])


def test_true_seven_day_forecast(client, fake_ml, reference):
    h = auth(signup(client))
    result = _run(client, h, reference, horizonDays=7)
    assert result["isReplay"] is False and result["model"]["version"] == "1.1.0"
    assert [p["date"] for p in result["points"]][0] == "2016-05-23" and len(result["points"]) == 7
    assert all(p["actual"] is None for p in result["points"]) and result["runMetrics"] is None
    assert result["product"]["demandClass"]["class"] == "SMOOTH"


def test_baseline_only(client, fake_ml, reference):
    h = auth(signup(client))
    result = _run(client, h, reference, method="BASELINE")
    assert fake_ml.requests[-1]["includeAi"] is False and "model" not in fake_ml.requests[-1]
    assert result["points"][0]["ai"] is None and result["points"][0]["baseline"] == 2.0


def test_request_validation(client, fake_ml, reference):
    h = auth(signup(client))
    cases = [({"asOfDate": "2016-03-01"}, "AS_OF_OUT_OF_RANGE"),
             ({"asOfDate": "2016-05-23"}, "AS_OF_OUT_OF_RANGE"),
             ({"horizonDays": 14}, "HORIZON_NOT_SUPPORTED"),
             ({"modelVersion": "1.1.0-dev"}, "FORBIDDEN"),
             ({"productId": str(reference["short"]), "method": "AI"}, "INSUFFICIENT_HISTORY")]
    for body, code in cases:
        response = _create(client, h, reference, **body)
        assert response.json()["error"]["code"] == code, (body, response.text)


def test_research_user_dates_and_versions(client, db, fake_ml, reference):
    h = auth(signup(client))
    db.query(User).update({"is_research": True})
    db.commit()
    caps = client.get(f"/v1/stores/{reference['store']}/models/capabilities", headers=h).json()
    assert caps["asOf"]["researchMin"] == "2016-03-27"
    assert _run(client, h, reference, asOfDate="2016-04-01")["model"]["version"] == "1.1.0-dev"
    leak = _create(client, h, reference, asOfDate="2016-05-08", modelVersion="1.1.0")
    assert leak.json()["error"]["code"] == "NO_MODEL_FOR_AS_OF"


def test_ml_outage_is_retried_then_fails_retryable(client, db, fake_ml, reference):
    h = auth(signup(client))
    fake_ml.fail_with = 503
    job = _create(client, h, reference).json()
    for _ in range(forecasting.MAX_ATTEMPTS):
        assert worker.run_once()
    status = client.get(f"/v1/forecast-jobs/{job['jobId']}", headers=h).json()
    assert status["status"] == "FAILED" and status["retryable"] is True
    assert status["error"]["code"] == "ML_SERVICE_UNAVAILABLE"
    fake_ml.fail_with = None
    assert client.post(f"/v1/forecast-jobs/{job['jobId']}/retry", headers=h).status_code == 202
    assert worker.run_once()
    assert client.get(f"/v1/forecast-jobs/{job['jobId']}", headers=h).json()["status"] == "COMPLETED"


def test_ai_unavailable_for_product(client, fake_ml, reference):
    h = auth(signup(client))
    fake_ml.ai_reason = "PRODUCT_NOT_IN_MODEL"
    compare = _run(client, h, reference)
    assert compare["status"] == "COMPLETED" and compare["aiUnavailableReason"] == "PRODUCT_NOT_IN_MODEL"
    ai_only = _run(client, h, reference, method="AI")
    assert ai_only["status"] == "FAILED" and ai_only["error"]["code"] == "PRODUCT_NOT_IN_MODEL"


def test_reference_runs_are_private_per_user(client, fake_ml, reference):
    alice, bob = auth(signup(client, "a@example.com")), auth(signup(client, "b@example.com"))
    result = _run(client, alice, reference)
    assert client.get(f"/v1/forecasts/{result['forecastId']}", headers=bob).status_code == 404
    listing = client.get("/v1/forecasts", params={"storeId": str(reference["store"])}, headers=bob).json()
    assert listing["total"] == 0
    assert client.get("/v1/forecasts", params={"storeId": str(reference["store"])}, headers=alice).json()["total"] == 1


def test_actuals_arrive_later(client, db, fake_ml, reference):
    h = auth(signup(client))
    result = _run(client, h, reference)
    db.add(SalesDaily(store_id=reference["store"], product_id=reference["product"], sale_date=dt.date(2016, 5, 23),
                      units_sold=4))
    db.commit()
    assert forecasting.attach_new_actuals(db) == 1
    again = client.get(f"/v1/forecasts/{result['forecastId']}", headers=h).json()
    assert again["points"][0]["actual"] == 4.0 and again["runMetrics"]["ai"]["mae"] == 2.5
    listing = client.get("/v1/forecasts", params={"storeId": str(reference["store"])}, headers=h).json()
    assert listing["items"][0]["actualsAvailable"] is True


def test_export_and_delete(client, fake_ml, reference):
    h = auth(signup(client))
    result = _run(client, h, reference, horizonDays=7)
    csv = client.get(f"/v1/forecasts/{result['forecastId']}/export", headers=h)
    lines = csv.text.strip().splitlines()
    assert csv.headers["content-type"].startswith("text/csv") and len(lines) == 8
    assert lines[1].startswith("CA_1,FOODS_3_090,2016-05-22,2016-05-23,1.5000,2.0000,3.0000,,1.1.0")
    assert client.delete(f"/v1/forecasts/{result['forecastId']}", headers=h).status_code == 204
    assert client.get(f"/v1/forecasts/{result['forecastId']}", headers=h).status_code == 404


def test_stuck_runs_are_requeued(db, fake_ml, reference, client):
    h = auth(signup(client))
    job = _create(client, h, reference).json()
    run_id = forecasting.claim_next(db)
    run = db.get(ForecastRun, run_id)
    run.started_at = run.started_at - forecasting.STUCK_AFTER * 2
    db.commit()
    assert forecasting.requeue_stuck(db) == 1
    assert client.get(f"/v1/forecast-jobs/{job['jobId']}", headers=h).json()["status"] == "QUEUED"
