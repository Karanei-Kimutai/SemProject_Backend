"""Store-wide forecast batches: forecast every product of a store in one request."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db
from app.deps import current_user, require, store_access
from app.errors import ApiError, not_found
from app.models import ForecastBatch, ForecastRun, Product, RunStatus, Store, User
from app.routers.forecasts import _process_now
from app.schemas import BatchIn, BatchItem, BatchItemPage, BatchOut
from app.services import batches

router = APIRouter(prefix="/v1", tags=["forecast batches"])


def _batch_for_user(db: Session, user: User, batch_id: uuid.UUID) -> tuple[ForecastBatch, Store]:
    batch = db.get(ForecastBatch, batch_id)
    if batch is None:
        raise not_found("Forecast batch")
    try:
        store, _ = store_access(db, user, batch.store_id)
    except ApiError:
        raise not_found("Forecast batch") from None
    if store.workspace.is_reference and batch.requested_by != user.id:
        raise not_found("Forecast batch")  # demo-store forecasts are private to the user who made them
    return batch, store


def batch_out(db: Session, batch: ForecastBatch, store: Store) -> BatchOut:
    by_status = batches.counts(db, batch)
    completed = by_status.get(RunStatus.COMPLETED, 0)
    failed = by_status.get(RunStatus.FAILED, 0)
    pending = batch.product_count - completed - failed
    finished_at = None
    if pending == 0:
        finished_at = db.scalar(select(func.max(ForecastRun.finished_at)).where(ForecastRun.batch_id == batch.id))
    return BatchOut(batch_id=batch.id, store_id=batch.store_id, status="RUNNING" if pending else "COMPLETED",
                    method=batch.method.value, as_of_date=batch.as_of_date, horizon_days=batch.horizon_days,
                    total=batch.product_count, completed=completed, failed=failed, pending=pending,
                    created_at=batch.created_at, finished_at=finished_at,
                    outdated=batch.history_version != store.history_version)


@router.post("/stores/{store_id}/forecast-batches", response_model=BatchOut, status_code=202,
             response_model_by_alias=True)
def create_batch(store_id: uuid.UUID, body: BatchIn, response: Response, background: BackgroundTasks,
                 user: User = Depends(current_user), db: Session = Depends(get_db)) -> BatchOut:
    store, access = store_access(db, user, store_id)
    require(access.can_forecast, "Viewers can't create forecasts.")
    existing = None if body.product_ids else batches.running_batch(db, user, store, body.horizon_days)
    if existing is not None:
        response.status_code = 200  # the same request is already running: report on it instead of starting another
        return batch_out(db, existing, store)
    batch = batches.create_batch(db, user, store, body.horizon_days, body.product_ids)
    if get_settings().run_worker_in_process:
        for run_id in db.scalars(select(ForecastRun.id).where(ForecastRun.batch_id == batch.id)):
            background.add_task(_process_now, run_id)
    return batch_out(db, batch, store)


@router.get("/stores/{store_id}/forecast-batches/latest", response_model=BatchOut, response_model_by_alias=True)
def latest_batch(store_id: uuid.UUID, horizon_days: int | None = Query(None, alias="horizonDays"),
                 user: User = Depends(current_user), db: Session = Depends(get_db)) -> BatchOut:
    store, access = store_access(db, user, store_id)
    query = select(ForecastBatch).where(ForecastBatch.store_id == store.id)
    if access.is_reference:
        query = query.where(ForecastBatch.requested_by == user.id)
    if horizon_days is not None:
        query = query.where(ForecastBatch.horizon_days == horizon_days)
    batch = db.scalar(query.order_by(ForecastBatch.created_at.desc()).limit(1))
    if batch is None:
        raise not_found("Forecast batch")
    return batch_out(db, batch, store)


@router.get("/forecast-batches/{batch_id}", response_model=BatchOut, response_model_by_alias=True)
def get_batch(batch_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> BatchOut:
    batch, store = _batch_for_user(db, user, batch_id)
    return batch_out(db, batch, store)


@router.get("/forecast-batches/{batch_id}/items", response_model=BatchItemPage, response_model_by_alias=True)
def batch_items(batch_id: uuid.UUID, q: str | None = None, status: str | None = None,
                page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200, alias="pageSize"),
                user: User = Depends(current_user), db: Session = Depends(get_db)) -> BatchItemPage:
    batch, _ = _batch_for_user(db, user, batch_id)
    query = (select(ForecastRun, Product).join(Product, Product.id == ForecastRun.product_id)
             .where(ForecastRun.batch_id == batch.id))
    if q:
        like = f"%{q.strip()}%"
        query = query.where(Product.name.ilike(like) | Product.sku.ilike(like))
    if status:
        query = query.where(ForecastRun.status == RunStatus(status.upper()))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.options(selectinload(ForecastRun.points)).order_by(Product.name, Product.sku)
                      .offset((page - 1) * page_size).limit(page_size))
    items = []
    for run, product in rows:
        done = run.status == RunStatus.COMPLETED
        items.append(BatchItem(
            forecast_id=run.id, product_id=product.id, sku=product.sku, product_name=product.name,
            unit_of_measure=product.unit_of_measure, category=product.category,
            demand_label=product.demand_label, status=run.status.value,
            error={"code": run.error_code, "message": run.error_message} if run.error_code else None,
            ai_unavailable_reason=run.ai_unavailable_reason,
            points=[{"date": p.point_date, "ai": p.ai, "baseline": p.naive_last_value,
                     "seasonal_naive": p.seasonal_naive_7, "actual": p.actual} for p in run.points] if done else []))
    return BatchItemPage(page=page, page_size=page_size, total=total, items=items)


@router.post("/forecast-batches/{batch_id}/retry", response_model=BatchOut, status_code=202,
             response_model_by_alias=True)
def retry_batch(batch_id: uuid.UUID, background: BackgroundTasks, user: User = Depends(current_user),
                db: Session = Depends(get_db)) -> BatchOut:
    batch, store = _batch_for_user(db, user, batch_id)
    _, access = store_access(db, user, store.id)
    require(access.can_forecast, "Viewers can't create forecasts.")
    if batches.retry_failed(db, batch) == 0:
        raise ApiError("NOT_RETRYABLE", "None of this batch's failed forecasts can be retried.", 409)
    if get_settings().run_worker_in_process:
        for run_id in db.scalars(select(ForecastRun.id).where(ForecastRun.batch_id == batch.id,
                                                             ForecastRun.status == RunStatus.QUEUED)):
            background.add_task(_process_now, run_id)
    return batch_out(db, batch, store)
