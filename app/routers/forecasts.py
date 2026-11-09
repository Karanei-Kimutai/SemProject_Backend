"""Forecast jobs and results (spec §8.8–8.11, §12.1)."""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, get_sessionmaker
from app.deps import current_user, product_in_store, require, store_access
from app.errors import not_found
from app.models import ForecastRun, Product, RunStatus, Store, User
from app.schemas import ForecastIn, ForecastPage, ForecastSummary, JobOut
from app.services import forecasting, models_info
from app.services.history import load_history

router = APIRouter(prefix="/v1", tags=["forecasts"])

RETRYABLE = {"ML_SERVICE_UNAVAILABLE", "WORKER_TIMEOUT", "INTERNAL_ERROR"}


def _run_for_user(db: Session, user: User, forecast_id: uuid.UUID) -> tuple[ForecastRun, Store]:
    run = db.get(ForecastRun, forecast_id)
    if run is None or run.deleted_at is not None:
        raise not_found("Forecast")
    try:
        store, _ = store_access(db, user, run.store_id)
    except Exception:
        raise not_found("Forecast") from None
    if store.workspace.is_reference and run.requested_by != user.id:
        raise not_found("Forecast")  # demo-store runs are private to the user who made them
    return run, store


def job_out(run: ForecastRun) -> JobOut:
    error = None
    if run.error_code:
        error = {"code": run.error_code, "message": run.error_message}
    return JobOut(job_id=run.id, forecast_id=run.id, status=run.status.value, created_at=run.created_at,
                  started_at=run.started_at, finished_at=run.finished_at, error=error,
                  retryable=run.status == RunStatus.FAILED and run.error_code in RETRYABLE)


def _process_now(run_id: uuid.UUID) -> None:
    with get_sessionmaker()() as db:
        claimed = forecasting.claim_next(db, run_id)
        if claimed is not None:
            forecasting.process_run(db, claimed)


@router.post("/forecasts", response_model=JobOut, status_code=202, response_model_by_alias=True)
def create_forecast(body: ForecastIn, background: BackgroundTasks, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> JobOut:
    store, access = store_access(db, user, body.store_id)
    require(access.can_forecast, "Viewers can't create forecasts.")
    product = product_in_store(db, store, body.product_id)
    as_of, is_replay = forecasting.validate_request(db, user, store, product, body.as_of_date, body.horizon_days,
                                                    body.method, body.model_version)
    run = forecasting.create_run(db, user, store, product, as_of, body.horizon_days, body.method,
                                 body.model_version, is_replay)
    if get_settings().run_worker_in_process:
        background.add_task(_process_now, run.id)
    return job_out(run)


@router.get("/forecast-jobs/{job_id}", response_model=JobOut, response_model_by_alias=True)
def job_status(job_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> JobOut:
    run, _ = _run_for_user(db, user, job_id)
    return job_out(run)


@router.post("/forecast-jobs/{job_id}/retry", response_model=JobOut, status_code=202, response_model_by_alias=True)
def retry_job(job_id: uuid.UUID, background: BackgroundTasks, user: User = Depends(current_user),
              db: Session = Depends(get_db)) -> JobOut:
    run, _ = _run_for_user(db, user, job_id)
    forecasting.retry(db, run)
    db.refresh(run)
    if get_settings().run_worker_in_process:
        background.add_task(_process_now, run.id)
    return job_out(run)


@router.get("/forecasts/{forecast_id}")
def get_forecast(forecast_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run, store = _run_for_user(db, user, forecast_id)
    product = db.get(Product, run.product_id)
    result = {
        "forecastId": str(run.id), "status": run.status.value,
        "createdAt": run.created_at.isoformat(), "generatedAt": run.finished_at.isoformat() if run.finished_at else None,
        "storeId": str(store.id), "isReference": store.workspace.is_reference,
        "product": {"id": str(product.id), "sku": product.sku, "name": product.name,
                    "unitOfMeasure": product.unit_of_measure, "demandClass": run.demand_class},
        "asOfDate": run.as_of_date.isoformat(), "isReplay": run.is_replay, "method": run.method.value,
        "error": {"code": run.error_code, "message": run.error_message} if run.error_code else None,
    }
    if run.status != RunStatus.COMPLETED:
        return result
    settings = get_settings()
    start = run.as_of_date - dt.timedelta(days=settings.chart_history_days - 1)
    history = load_history(db, store.id, product.id, start, run.as_of_date)
    points = run.points
    result.update({
        "horizon": {"startDate": points[0].point_date.isoformat() if points else None,
                    "endDate": points[-1].point_date.isoformat() if points else None,
                    "days": run.horizon_days, "frequency": "DAILY", "validated": run.horizon_validated},
        "model": ({"id": run.model_id, "version": run.model_version, "featureSchema": run.feature_schema,
                   "trainingEndDate": run.model_training_end.isoformat() if run.model_training_end else None}
                  if run.model_id else None),
        "baselines": models_info.baseline_labels(),
        "aiUnavailableReason": run.ai_unavailable_reason,
        "priceAssumption": run.price_assumption,
        "missingDaysInLookback": run.missing_days_in_lookback,
        "warnings": run.warnings or [],
        "points": [{"date": p.point_date.isoformat(), "ai": p.ai, "baseline": p.naive_last_value,
                    "seasonalNaive": p.seasonal_naive_7, "actual": p.actual} for p in points],
        "history": [{"date": h.sale_date.isoformat(), "actual": float(h.units_sold)} for h in history],
        "predictionIntervals": None,
        "runMetrics": run.run_metrics,
        "productBacktest": run.product_backtest,
        "historyVersion": run.history_version,
        "mlengineVersion": run.mlengine_version,
        "mlRequestId": run.ml_request_id,
    })
    return result


@router.get("/forecasts", response_model=ForecastPage, response_model_by_alias=True)
def list_forecasts(store_id: uuid.UUID = Query(alias="storeId"), product_id: uuid.UUID | None = Query(None, alias="productId"),
                   replay: bool | None = None, status: str | None = None,
                   batch: bool | None = Query(None, description="true: only runs from batches; false: only single forecasts"),
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100, alias="pageSize"),
                   user: User = Depends(current_user), db: Session = Depends(get_db)) -> ForecastPage:
    store, access = store_access(db, user, store_id)
    query = (select(ForecastRun, Product).join(Product, Product.id == ForecastRun.product_id)
             .where(ForecastRun.store_id == store.id, ForecastRun.deleted_at.is_(None)))
    if access.is_reference:
        query = query.where(ForecastRun.requested_by == user.id)
    if product_id:
        query = query.where(ForecastRun.product_id == product_id)
    if replay is not None:
        query = query.where(ForecastRun.is_replay == replay)
    if status:
        query = query.where(ForecastRun.status == RunStatus(status.upper()))
    if batch is not None:
        query = query.where(ForecastRun.batch_id.is_not(None) if batch else ForecastRun.batch_id.is_(None))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.order_by(ForecastRun.created_at.desc()).offset((page - 1) * page_size).limit(page_size))
    items = [ForecastSummary(
        forecast_id=run.id, status=run.status.value, store_id=run.store_id, product_id=run.product_id,
        sku=product.sku, product_name=product.name, as_of_date=run.as_of_date, horizon_days=run.horizon_days,
        method=run.method.value, is_replay=run.is_replay, model_version=run.model_version,
        created_at=run.created_at,
        actuals_available=bool(run.run_metrics) and not run.is_replay) for run, product in rows]
    return ForecastPage(page=page, page_size=page_size, total=total, items=items)


@router.delete("/forecasts/{forecast_id}", status_code=204)
def delete_forecast(forecast_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    run, store = _run_for_user(db, user, forecast_id)
    _, access = store_access(db, user, store.id)
    require(run.requested_by == user.id or access.can_edit, "You can only delete your own forecasts.")
    run.deleted_at = func.now()
    db.commit()


@router.get("/forecasts/{forecast_id}/export")
def export_forecast(forecast_id: uuid.UUID, format: str = Query("csv", pattern="^csv$"),
                    user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    run, store = _run_for_user(db, user, forecast_id)
    product = db.get(Product, run.product_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["store", "sku", "as_of_date", "date", "ai_forecast", "baseline_same_as_last_day",
                     "baseline_same_day_last_week", "actual", "model_version"])
    for p in run.points:
        writer.writerow([store.code, product.sku, run.as_of_date, p.point_date, _fmt(p.ai), _fmt(p.naive_last_value),
                         _fmt(p.seasonal_naive_7), _fmt(p.actual), run.model_version or ""])
    filename = f"forecast_{product.sku}_{run.as_of_date}.csv"
    return Response(buffer.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"
