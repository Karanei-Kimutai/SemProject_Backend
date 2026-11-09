"""Store-wide forecasts: one request that forecasts many products.

A batch creates one ordinary ForecastRun per product, so the worker, retries, stuck-run recovery and
the per-forecast endpoints all work unchanged. The batch only groups them and reports progress.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import Session

from app.errors import ApiError, not_found
from app.models import ForecastBatch, ForecastMethod, ForecastRun, Product, RunStatus, SalesDaily, Store, User
from app.services import forecasting, models_info

MAX_PRODUCTS = 2000
RETRYABLE = {"ML_SERVICE_UNAVAILABLE", "WORKER_TIMEOUT", "INTERNAL_ERROR"}
_UNFINISHED = (RunStatus.QUEUED, *forecasting.IN_PROGRESS)


def _products_with_history(db: Session, store: Store) -> list[Product]:
    has_sales = exists().where(SalesDaily.store_id == store.id, SalesDaily.product_id == Product.id)
    return list(db.scalars(select(Product).where(Product.workspace_id == store.workspace_id, Product.is_active,
                                                 has_sales).order_by(Product.name)))


def _chosen_products(db: Session, store: Store, product_ids: list[uuid.UUID]) -> list[Product]:
    wanted = set(product_ids)
    found = list(db.scalars(select(Product).where(Product.id.in_(wanted), Product.workspace_id == store.workspace_id)))
    if len(found) != len(wanted):
        raise not_found("Product")
    return sorted(found, key=lambda p: p.name)


def running_batch(db: Session, user: User, store: Store, horizon: int) -> ForecastBatch | None:
    """A batch by this user for this store and length that still has unfinished runs."""
    unfinished = exists().where(ForecastRun.batch_id == ForecastBatch.id, ForecastRun.status.in_(_UNFINISHED))
    return db.scalar(select(ForecastBatch).where(
        ForecastBatch.store_id == store.id, ForecastBatch.requested_by == user.id,
        ForecastBatch.horizon_days == horizon, ForecastBatch.history_version == store.history_version,
        unfinished).order_by(ForecastBatch.created_at.desc()).limit(1))


def create_batch(db: Session, user: User, store: Store, horizon: int,
                 product_ids: list[uuid.UUID] | None) -> ForecastBatch:
    """Raise a typed 422 the app can explain, or queue one run per product."""
    products = _chosen_products(db, store, product_ids) if product_ids else _products_with_history(db, store)
    if not products:
        raise ApiError("NO_PRODUCTS", "There are no products with sales history to forecast. Upload sales first.")
    if len(products) > MAX_PRODUCTS:
        raise ApiError("BATCH_TOO_LARGE", f"A batch can forecast at most {MAX_PRODUCTS} products.",
                       details={"max": MAX_PRODUCTS, "requested": len(products)})

    # AI with the baselines alongside when the store has a model; otherwise the baselines only.
    method = ForecastMethod.COMPARE if models_info.versions_for(store.model_id) else ForecastMethod.BASELINE
    # Store-level checks (history, horizon, model) are the same for every product.
    as_of, is_replay = forecasting.validate_request(db, user, store, products[0], None, horizon, method.value, None)

    batch = ForecastBatch(store_id=store.id, requested_by=user.id, method=method, as_of_date=as_of,
                          horizon_days=horizon, history_version=store.history_version, product_count=len(products))
    db.add(batch)
    db.flush()
    db.add_all(ForecastRun(store_id=store.id, product_id=p.id, batch_id=batch.id, requested_by=user.id,
                           method=method, as_of_date=as_of, horizon_days=horizon, is_replay=is_replay,
                           history_version=store.history_version, status=RunStatus.QUEUED) for p in products)
    db.commit()
    return batch


def counts(db: Session, batch: ForecastBatch) -> dict[RunStatus, int]:
    rows = db.execute(select(ForecastRun.status, func.count()).where(ForecastRun.batch_id == batch.id)
                      .group_by(ForecastRun.status))
    return {status: n for status, n in rows}


def retry_failed(db: Session, batch: ForecastBatch) -> int:
    """Queue again the runs that failed for a temporary reason. Product-level failures stay failed."""
    result = db.execute(update(ForecastRun).where(
        ForecastRun.batch_id == batch.id, ForecastRun.status == RunStatus.FAILED,
        ForecastRun.error_code.in_(RETRYABLE)).values(
        status=RunStatus.QUEUED, error_code=None, error_message=None, attempts=0, started_at=None, finished_at=None))
    db.commit()
    return result.rowcount
