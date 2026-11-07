"""Forecast jobs: validation at request time, and the worker step that calls the ML service.

Lifecycle (design spec §9.5): QUEUED → PREPARING_DATA → RUNNING_MODEL → FINALIZING → COMPLETED/FAILED.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import ApiError
from app.ml_client import MlServiceError, get_ml_client
from app.models import ForecastMethod, ForecastPoint, ForecastRun, Product, RunStatus, SalesDaily, Store, User
from app.services import models_info
from app.services.history import load_future_prices, load_history, product_readiness

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
STUCK_AFTER = dt.timedelta(minutes=5)
IN_PROGRESS = (RunStatus.PREPARING_DATA, RunStatus.RUNNING_MODEL, RunStatus.FINALIZING)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---- request-time checks ------------------------------------------------------------------

def validate_request(db: Session, user: User, store: Store, product: Product, as_of: dt.date | None,
                     horizon: int, method: str, model_version: str | None) -> tuple[dt.date, bool]:
    """Raise a typed 422 the app can explain, or return (as_of, is_replay)."""
    if store.history_end is None:
        raise ApiError("INSUFFICIENT_HISTORY", "This store has no sales history yet.")
    is_reference = store.workspace.is_reference
    as_of = as_of or store.history_end
    versions = models_info.versions_for(store.model_id)
    uses_ai = method != ForecastMethod.BASELINE.value

    if model_version is not None and not user.is_research:
        raise ApiError("FORBIDDEN", "Only research users can choose a model version.", 403)

    if not is_reference and as_of != store.history_end:
        raise ApiError("AS_OF_OUT_OF_RANGE", "Forecasts for your store always start from the latest day of history.",
                       details={"asOfDate": str(store.history_end)})
    if is_reference:
        bounds = models_info.as_of_range(store, versions)
        low = bounds.get("researchMin") if user.is_research else bounds.get("replayMin")
        if low is None or as_of < low or as_of > bounds["default"]:
            raise ApiError("AS_OF_OUT_OF_RANGE", "Choose an as-of date inside the allowed range.",
                           details={"min": str(low) if low else None, "max": str(bounds["default"])})
        calendar_end = bounds.get("calendarEnd")
        if calendar_end and as_of + dt.timedelta(days=horizon) > calendar_end:
            raise ApiError("AS_OF_OUT_OF_RANGE", "The forecast would run past the end of the demo calendar.",
                           details={"calendarEnd": str(calendar_end)})

    if uses_ai:
        if not versions:
            raise ApiError("AI_NOT_AVAILABLE", "AI forecasting isn't available for this store yet. Use the baseline.")
        chosen = (next((v for v in versions if v["version"] == model_version), None) if model_version
                  else models_info.resolve_local(store.model_id, as_of))
        if model_version and chosen is None:
            raise ApiError("MODEL_NOT_FOUND", f"Model version {model_version} doesn't exist.", 404)
        if chosen is None or dt.date.fromisoformat(chosen["trainingEndDate"]) > as_of:
            raise ApiError("NO_MODEL_FOR_AS_OF", "No model version was trained only on data before this date.")
        if horizon not in chosen["capabilities"]["horizons"]:
            raise ApiError("HORIZON_NOT_SUPPORTED", "This model doesn't support that forecast length.",
                           details={"horizons": chosen["capabilities"]["horizons"]})
        caps = chosen["capabilities"]
        check = product_readiness(db, store, [product.id], as_of, caps["minHistoryDays"],
                                  caps["maxMissingDaysInLookback"])[product.id]
        blocking = [r for r in check.reasons if r != "STALE_HISTORY"]
        if blocking and method == ForecastMethod.AI.value:
            messages = {"INSUFFICIENT_HISTORY": "This product needs at least 21 days of sales history.",
                        "TOO_MANY_GAPS": "Too many unrecorded days recently."}
            raise ApiError(blocking[0], messages[blocking[0]],
                           details={"recordedDays": check.recorded_days, "missingDays": check.missing_days,
                                    "requiredDays": caps["minHistoryDays"]})
    elif horizon not in (1, 7):
        raise ApiError("HORIZON_NOT_SUPPORTED", "Baselines support 1 or 7 days.", details={"horizons": [1, 7]})

    is_replay = as_of < store.history_end
    return as_of, is_replay


def create_run(db: Session, user: User, store: Store, product: Product, as_of: dt.date, horizon: int,
               method: str, model_version: str | None, is_replay: bool) -> ForecastRun:
    run = ForecastRun(store_id=store.id, product_id=product.id, requested_by=user.id,
                      method=ForecastMethod(method), as_of_date=as_of, horizon_days=horizon,
                      requested_model_version=model_version, is_replay=is_replay,
                      history_version=store.history_version, status=RunStatus.QUEUED)
    db.add(run)
    db.commit()
    return run


# ---- worker -------------------------------------------------------------------------------

def claim_next(db: Session, run_id: uuid.UUID | None = None) -> uuid.UUID | None:
    """Claim the oldest queued run (or a specific one). SKIP LOCKED lets several workers run safely."""
    query = select(ForecastRun).where(ForecastRun.status == RunStatus.QUEUED)
    if run_id is not None:
        query = query.where(ForecastRun.id == run_id)
    run = db.scalar(query.order_by(ForecastRun.created_at).limit(1).with_for_update(skip_locked=True))
    if run is None:
        db.rollback()
        return None
    run.status, run.started_at, run.attempts = RunStatus.PREPARING_DATA, _now(), run.attempts + 1
    db.commit()
    return run.id


def requeue_stuck(db: Session) -> int:
    """Runs left in progress by a crashed worker go back to the queue (or fail after MAX_ATTEMPTS)."""
    cutoff = _now() - STUCK_AFTER
    stuck = list(db.scalars(select(ForecastRun).where(ForecastRun.status.in_(IN_PROGRESS),
                                                      ForecastRun.started_at < cutoff)))
    for run in stuck:
        if run.attempts >= MAX_ATTEMPTS:
            _fail(run, "WORKER_TIMEOUT", "The forecast took too long. Try again.")
        else:
            run.status = RunStatus.QUEUED
    db.commit()
    return len(stuck)


def _fail(run: ForecastRun, code: str, message: str) -> None:
    run.status, run.error_code, run.error_message, run.finished_at = RunStatus.FAILED, code, message, _now()


def _set_status(db: Session, run: ForecastRun, status: RunStatus) -> None:
    run.status = status
    db.commit()


def _num(value) -> float | None:
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) else value


def build_ml_request(run: ForecastRun, store: Store, product: Product, history: list[SalesDaily],
                     future_prices: list, request_id: str) -> dict:
    series = {
        "productId": product.sku,
        "storeId": store.code,
        "category": product.category,
        "firstPriceDate": product.first_price_date.isoformat() if product.first_price_date else None,
        "history": [{"date": row.sale_date.isoformat(), "units": float(row.units_sold),
                     "price": _num(row.unit_price)} for row in history],
    }
    if future_prices:
        series["futurePrices"] = [{"date": d.isoformat(), "price": price} for d, price in future_prices]
    uses_ai = run.method != ForecastMethod.BASELINE
    body = {
        "requestId": request_id,
        "includeAi": uses_ai,
        "asOfDate": run.as_of_date.isoformat(),
        "horizonDays": run.horizon_days,
        "baselines": ["naive_last_value", "seasonal_naive_7"],
        "series": [series],
    }
    if uses_ai:
        body["model"] = {"id": store.model_id, **({"version": run.requested_model_version}
                                                  if run.requested_model_version else {})}
    return body


def _mae(pairs: list[tuple[float, float]]) -> float | None:
    return sum(abs(a - f) for a, f in pairs) / len(pairs) if pairs else None


def process_run(db: Session, run_id: uuid.UUID) -> None:
    settings = get_settings()
    run = db.get(ForecastRun, run_id)
    store, product = db.get(Store, run.store_id), db.get(Product, run.product_id)
    try:
        start = run.as_of_date - dt.timedelta(days=settings.history_days_for_forecast - 1)
        history = load_history(db, store.id, product.id, start, run.as_of_date)
        horizon_end = run.as_of_date + dt.timedelta(days=run.horizon_days)
        future = load_future_prices(db, store.id, product.id, run.as_of_date + dt.timedelta(days=1), horizon_end)
        if not history:
            _fail(run, "INSUFFICIENT_HISTORY", "This product has no sales history before the as-of date.")
            db.commit()
            return
        request_id = f"fc_{run.id.hex}"
        body = build_ml_request(run, store, product, history, future, request_id)

        _set_status(db, run, RunStatus.RUNNING_MODEL)
        client = get_ml_client()
        response = client.forecast(body, request_id)

        _set_status(db, run, RunStatus.FINALIZING)
        _store_result(db, run, store, product, response, client)
        run.status, run.finished_at = RunStatus.COMPLETED, _now()
        db.commit()
    except MlServiceError as error:
        db.rollback()
        run = db.get(ForecastRun, run_id)
        if error.retryable and run.attempts < MAX_ATTEMPTS:
            run.status = RunStatus.QUEUED
        else:
            _fail(run, error.code, error.message)
        db.commit()
    except Exception:  # noqa: BLE001 - a job must never stay in progress
        log.exception("forecast run %s failed", run_id)
        db.rollback()
        run = db.get(ForecastRun, run_id)
        _fail(run, "INTERNAL_ERROR", "Something went wrong while forecasting.")
        db.commit()


def _store_result(db: Session, run: ForecastRun, store: Store, product: Product, response: dict, client) -> None:
    series = response["series"][0]
    model = response.get("model") or {}
    run.model_id, run.model_version = model.get("id"), model.get("version")
    run.feature_schema = model.get("featureSchema")
    run.model_training_end = dt.date.fromisoformat(model["trainingEndDate"]) if model.get("trainingEndDate") else None
    run.mlengine_version = response.get("mlengineVersion")
    run.ml_request_id = response.get("requestId")
    run.horizon_validated = response.get("horizon", {}).get("validated")
    run.price_assumption = series.get("priceAssumption")
    run.missing_days_in_lookback = series.get("missingDaysInLookback")
    run.ai_unavailable_reason = series.get("aiUnavailableReason")
    run.demand_class = series.get("demandClass")
    run.warnings = series.get("warnings") or []

    if run.method == ForecastMethod.AI and run.ai_unavailable_reason:
        raise MlServiceError(run.ai_unavailable_reason, "The AI forecast isn't available for this product.")

    points = {dt.date.fromisoformat(p["date"]): p for p in series["points"]}
    actuals = {}
    if points:
        rows = db.execute(select(SalesDaily.sale_date, SalesDaily.units_sold).where(
            SalesDaily.store_id == store.id, SalesDaily.product_id == product.id,
            SalesDaily.sale_date >= min(points), SalesDaily.sale_date <= max(points)))
        actuals = {d: float(u) for d, u in rows}
    run.points = [
        ForecastPoint(point_date=d, ai=_num(p.get("ai")),
                      naive_last_value=_num(p["baselines"].get("naive_last_value")),
                      seasonal_naive_7=_num(p["baselines"].get("seasonal_naive_7")),
                      actual=actuals.get(d))
        for d, p in sorted(points.items())
    ]
    run.run_metrics = run_metrics(run.points)

    if run.model_id and store.workspace.is_reference:
        latest = models_info.versions_for(run.model_id)
        if latest:
            run.product_backtest = client.product_backtest(run.model_id, latest[-1]["version"], product.sku)

    demand = run.demand_class or {}
    if demand.get("class"):
        product.demand_class, product.demand_label = demand["class"], demand.get("label")
        product.demand_adi, product.demand_cv2 = demand.get("adi"), demand.get("cv2")
        product.demand_zero_share, product.demand_updated_at = demand.get("zeroShare"), _now()


def run_metrics(points: list[ForecastPoint]) -> dict | None:
    """Errors on forecast days whose actuals are known (replays, or past forecasts after an import)."""
    known = [p for p in points if p.actual is not None]
    if not known:
        return None
    ai = [(p.actual, p.ai) for p in known if p.ai is not None]
    base = [(p.actual, p.naive_last_value) for p in known if p.naive_last_value is not None]
    return {"window": f"{known[0].point_date}/{known[-1].point_date}", "n": len(known),
            "ai": {"mae": _mae(ai)} if ai else None, "baseline": {"mae": _mae(base)} if base else None}


def attach_new_actuals(db: Session, store_id: uuid.UUID | None = None) -> int:
    """Nightly job (spec §11.6): fill in actuals that arrived after a forecast was made."""
    query = (select(ForecastPoint, SalesDaily.units_sold)
             .join(ForecastRun, ForecastRun.id == ForecastPoint.run_id)
             .join(SalesDaily, (SalesDaily.store_id == ForecastRun.store_id)
                   & (SalesDaily.product_id == ForecastRun.product_id)
                   & (SalesDaily.sale_date == ForecastPoint.point_date))
             .where(ForecastPoint.actual.is_(None), ForecastRun.status == RunStatus.COMPLETED))
    if store_id:
        query = query.where(ForecastRun.store_id == store_id)
    touched = set()
    for point, units in db.execute(query):
        point.actual = float(units)
        touched.add(point.run_id)
    for run_id in touched:
        run = db.get(ForecastRun, run_id)
        run.run_metrics = run_metrics(run.points)
    db.commit()
    return len(touched)


def retry(db: Session, run: ForecastRun) -> None:
    if run.status != RunStatus.FAILED:
        raise ApiError("NOT_RETRYABLE", "Only failed forecasts can be retried.", 409)
    db.execute(update(ForecastRun).where(ForecastRun.id == run.id).values(
        status=RunStatus.QUEUED, error_code=None, error_message=None, attempts=0,
        started_at=None, finished_at=None))
    db.commit()
