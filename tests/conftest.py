"""Test setup: a real Postgres database (TEST_DATABASE_URL) and a fake ML service.

The fake ML service answers like mlengine's /internal/v1 API. For the captured replay request
(FOODS_3_090, as-of 2016-05-08) it returns the real captured response from
tests/fixtures/ml/replay_response.json.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

os.environ.setdefault("TEST_DATABASE_URL", "postgresql+psycopg://dev@127.0.0.1:55432/forecast_test")
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["JWT_SECRET"] = "test-secret-that-is-at-least-32-characters"
os.environ["RUN_WORKER_IN_PROCESS"] = "false"
os.environ["ML_SERVICE_URL"] = "http://ml.test"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import Base, get_engine, get_sessionmaker  # noqa: E402
from app.main import app  # noqa: E402
from app.ml_client import MlClient, set_ml_client  # noqa: E402
from app.models import FuturePrice, Product, SalesDaily, Store, Workspace  # noqa: E402
from app.services import models_info  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "ml"
HISTORY_END = dt.date(2016, 5, 22)
HISTORY_DAYS = 400

CAPS = {"horizons": [1, 7], "experimentalMaxHorizon": 28, "frequency": "DAILY", "minHistoryDays": 21,
        "maxMissingDaysInLookback": 3, "predictionIntervals": False,
        "baselines": ["naive_last_value", "seasonal_naive_7"], "calendarEndDate": "2016-06-19"}
VERSIONS = [("1.1.0-dev", "2016-03-27"), ("1.1.0-bt", "2016-04-24"), ("1.1.0", "2016-05-22")]


class FakeMl:
    def __init__(self):
        self.requests: list[dict] = []
        self.fail_with: int | None = None  # HTTP status to return for /forecast
        self.ai_reason: str | None = None

    def models(self) -> list[dict]:
        return [{"id": "m5-ca1-foods", "version": v, "status": "ACTIVE", "featureSchema": "m5-v1",
                 "trainingEndDate": end, "capabilities": CAPS, "createdAt": "2026-11-07T00:00:00Z"}
                for v, end in VERSIONS]

    def resolve(self, as_of: dt.date) -> dict:
        eligible = [m for m in self.models() if dt.date.fromisoformat(m["trainingEndDate"]) <= as_of]
        return eligible[-1]

    def forecast(self, body: dict) -> httpx.Response:
        self.requests.append(body)
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"error": {"code": "BOOM", "message": "failed"}})
        series = body["series"][0]
        as_of = dt.date.fromisoformat(body["asOfDate"])
        if series["productId"] == "FOODS_3_090" and body["asOfDate"] == "2016-05-08" and body["horizonDays"] == 1:
            return httpx.Response(200, json=json.loads((FIXTURES / "replay_response.json").read_text()))
        model = None
        if body["includeAi"]:
            model = self.resolve(as_of)
            if "version" in body["model"]:
                model = next(m for m in self.models() if m["version"] == body["model"]["version"])
                if dt.date.fromisoformat(model["trainingEndDate"]) > as_of:
                    return httpx.Response(422, json={"error": {"code": "NO_MODEL_FOR_AS_OF", "message": "no"}})
        days = [as_of + dt.timedelta(days=k + 1) for k in range(body["horizonDays"])]
        ai_on = body["includeAi"] and not self.ai_reason
        return httpx.Response(200, json={
            "requestId": body.get("requestId"), "asOfDate": body["asOfDate"],
            "horizon": {"startDate": str(days[0]), "endDate": str(days[-1]), "days": len(days), "validated": True},
            "model": ({"id": model["id"], "version": model["version"], "featureSchema": "m5-v1",
                       "trainingEndDate": model["trainingEndDate"]} if model else None),
            "mlengineVersion": "0.1.0", "baselineLabels": {},
            "series": [{"productId": series["productId"], "storeId": series["storeId"],
                        "points": [{"date": str(d), "ai": 1.5 if ai_on else None,
                                    "baselines": {"naive_last_value": 2.0, "seasonal_naive_7": 3.0}} for d in days],
                        "aiUnavailableReason": None if ai_on else (self.ai_reason or "AI_NOT_REQUESTED"),
                        "missingDaysInLookback": 0, "priceAssumption": "KNOWN" if ai_on else "NOT_USED",
                        "demandClass": {"class": "SMOOTH", "label": "Steady seller", "adi": 1.0, "cv2": 0.2,
                                        "zeroShare": 0.0, "observedDays": 365, "saleDays": 365},
                        "warnings": []}]})

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Internal-Token") == "test-ml-token"
        path = request.url.path.removeprefix("/internal/v1")
        if path == "/models":
            return httpx.Response(200, json={"models": self.models()})
        if path.endswith("/card"):
            return httpx.Response(200, json={"title": "Demand model", "model": path.split("/")[3]})
        if path.endswith("/backtest"):
            return httpx.Response(200, json={"product": {"productId": request.url.params["productId"], "n": 28,
                                                         "ai": {"mae": 1.0, "rmse": 1.2, "wape": 0.2},
                                                         "naive_last_value": {"mae": 1.5, "rmse": 2.0, "wape": 0.3}}})
        if path == "/forecast":
            return self.forecast(json.loads(request.content))
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": path}})


@pytest.fixture(scope="session", autouse=True)
def schema():
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def clean(schema):
    with get_engine().begin() as connection:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        connection.execute(text(f"TRUNCATE {tables} CASCADE"))
    models_info.clear_cache()
    yield


@pytest.fixture
def fake_ml():
    fake = FakeMl()
    set_ml_client(MlClient("http://ml.test", "test-ml-token", 5, transport=httpx.MockTransport(fake.handler)))
    models_info.clear_cache()
    yield fake
    set_ml_client(None)


@pytest.fixture
def db():
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def client(fake_ml):
    return TestClient(app)


@pytest.fixture
def reference(db):
    """A small Reference workspace: CA_1 with FOODS_3_090 (full history) and FOODS_NEW (10 days)."""
    workspace = Workspace(name="Demo", is_reference=True)
    db.add(workspace)
    db.flush()
    store = Store(workspace_id=workspace.id, code="CA_1", name="Demo store", country="US", subdivision="CA",
                  timezone="America/Los_Angeles", currency="USD", model_id="m5-ca1-foods")
    db.add(store)
    full = Product(workspace_id=workspace.id, sku="FOODS_3_090", name="FOODS_3_090", category="FOODS_3",
                   first_price_date=dt.date(2011, 1, 29))
    short = Product(workspace_id=workspace.id, sku="FOODS_NEW", name="FOODS_NEW", category="FOODS_3")
    db.add_all([full, short])
    db.flush()
    for k in range(HISTORY_DAYS):
        day = HISTORY_END - dt.timedelta(days=k)
        units = 72 if day == dt.date(2016, 5, 9) else 10 + k % 7
        db.add(SalesDaily(store_id=store.id, product_id=full.id, sale_date=day, units_sold=units, unit_price=1.6))
        if k < 10:
            db.add(SalesDaily(store_id=store.id, product_id=short.id, sale_date=day, units_sold=1, unit_price=2.0))
    for k in range(1, 29):
        db.add(FuturePrice(store_id=store.id, product_id=full.id, price_date=HISTORY_END + dt.timedelta(days=k),
                           unit_price=1.6))
    store.history_start, store.history_end = HISTORY_END - dt.timedelta(days=HISTORY_DAYS - 1), HISTORY_END
    db.commit()
    return {"workspace": workspace.id, "store": store.id, "product": full.id, "short": short.id}


def signup(client: TestClient, email: str = "user@example.com", password: str = "long-enough-pw") -> dict:
    response = client.post("/v1/auth/register", json={"email": email, "password": password, "displayName": "U"})
    assert response.status_code == 201, response.text
    return response.json()


def auth(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['accessToken']}"}
