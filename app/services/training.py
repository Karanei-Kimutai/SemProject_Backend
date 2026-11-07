"""Own-data model training (spec §9.4): eligibility, training jobs, polling and weekly retraining.

One model per workspace, trained on all of its stores' history (each store/product is a series).
The ML service runs the job and decides the outcome:

* TRAINED: the AI beat "same as last day" on both RMSE and MAE over the last 28 days, so the model
  is registered and AI forecasts become available.
* GATE_FAILED: it didn't. Nothing is registered (an earlier model, if any, stays active). This is a
  normal, honest outcome: the app keeps offering baselines.
* NOT_ELIGIBLE: not enough history yet.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import ApiError
from app.ml_client import MlServiceError, get_ml_client
from app.models import Product, SalesDaily, Store, TrainingRun, TrainingStatus, User, Workspace
from app.services import models_info

log = logging.getLogger(__name__)
ACTIVE = (TrainingStatus.QUEUED, TrainingStatus.TRAINING)
MAX_ATTEMPTS = 3

_eligibility_cache: dict[tuple, dict] = {}
_cache_lock = threading.Lock()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _stores(db: Session, workspace_id: uuid.UUID) -> list[Store]:
    return list(db.scalars(select(Store).where(Store.workspace_id == workspace_id).order_by(Store.created_at)))


def history_versions(db: Session, workspace_id: uuid.UUID) -> dict[str, int]:
    return {str(s.id): s.history_version for s in _stores(db, workspace_id)}


def workspace_history(db: Session, workspace_id: uuid.UUID) -> list[dict]:
    """All recorded days of every store in the workspace, up to the last training_history_days."""
    stores = _stores(db, workspace_id)
    ends = [s.history_end for s in stores if s.history_end]
    if not ends:
        return []
    start = max(ends) - dt.timedelta(days=get_settings().training_history_days - 1)
    rows = db.execute(
        select(Store.code, Product.sku, Product.category, SalesDaily.sale_date, SalesDaily.units_sold,
               SalesDaily.unit_price)
        .join(Store, Store.id == SalesDaily.store_id).join(Product, Product.id == SalesDaily.product_id)
        .where(Store.workspace_id == workspace_id, SalesDaily.sale_date >= start)
        .order_by(Store.code, Product.sku, SalesDaily.sale_date))
    return [{"storeId": code, "productId": sku, "category": category, "date": day.isoformat(),
             "units": float(units), "price": float(price) if price is not None else None}
            for code, sku, category, day, units, price in rows]


def eligibility(db: Session, workspace_id: uuid.UUID) -> dict:
    """The ML service's eligibility checks for this workspace, cached until its history changes."""
    key = (workspace_id, tuple(sorted(history_versions(db, workspace_id).items())))
    with _cache_lock:
        if key in _eligibility_cache:
            return _eligibility_cache[key]
    history = workspace_history(db, workspace_id)
    if not history:
        rules = {"minProducts": 10, "minHistoryDaysPerProduct": 21, "minStoreHistoryDays": 120,
                 "minObservedProductDays": 4500}
        result = {"eligible": False, "rules": rules, "checks": {
            "productsWithEnoughHistory": {"value": 0, "required": rules["minProducts"], "passed": False},
            "storeHistoryDays": {"value": 0, "required": rules["minStoreHistoryDays"], "passed": False},
            "observedProductDays": {"value": 0, "required": rules["minObservedProductDays"], "passed": False}}}
    else:
        try:
            result = get_ml_client().eligibility(history)
        except MlServiceError as error:
            if error.retryable:
                raise ApiError("ML_SERVICE_UNAVAILABLE", "The forecasting service is unavailable. Try again shortly.",
                               503) from error
            raise ApiError(error.code, error.message) from error
    with _cache_lock:
        _eligibility_cache.clear() if len(_eligibility_cache) > 500 else None
        _eligibility_cache[key] = result
    return result


def latest_run(db: Session, workspace_id: uuid.UUID) -> TrainingRun | None:
    return db.scalar(select(TrainingRun).where(TrainingRun.workspace_id == workspace_id)
                     .order_by(TrainingRun.created_at.desc()).limit(1))


def request_training(db: Session, user: User | None, store: Store, trigger: str = "MANUAL") -> tuple[TrainingRun, bool]:
    """Queue a training run, or return the one already in progress. Returns (run, created)."""
    db.execute(select(Workspace).where(Workspace.id == store.workspace_id).with_for_update())
    active = db.scalar(select(TrainingRun).where(TrainingRun.workspace_id == store.workspace_id,
                                                 TrainingRun.status.in_(ACTIVE)))
    if active is not None:
        db.commit()
        return active, False
    run = TrainingRun(workspace_id=store.workspace_id, store_id=store.id, requested_by=user.id if user else None,
                      trigger=trigger, status=TrainingStatus.QUEUED, country=store.country,
                      subdivision=store.subdivision)
    db.add(run)
    db.commit()
    return run, True


# ---- worker -------------------------------------------------------------------------------

def submit_next(db: Session) -> uuid.UUID | None:
    """Send the oldest queued run's history to the ML service."""
    run = db.scalar(select(TrainingRun).where(TrainingRun.status == TrainingStatus.QUEUED)
                    .order_by(TrainingRun.created_at).limit(1).with_for_update(skip_locked=True))
    if run is None:
        db.rollback()
        return None
    run.attempts += 1
    history = workspace_history(db, run.workspace_id)
    run.history_versions = history_versions(db, run.workspace_id)
    run.history_rows = len(history)
    if not history:
        _finish(run, "NOT_ELIGIBLE", {"eligibility": eligibility_placeholder()})
        db.commit()
        return run.id
    body = {"workspaceId": str(run.workspace_id), "country": run.country, "subdivision": run.subdivision,
            "history": history}
    try:
        job = get_ml_client().train(body)
    except MlServiceError as error:
        if error.retryable and run.attempts < MAX_ATTEMPTS:
            run.status = TrainingStatus.QUEUED  # try again on a later pass
        else:
            _fail(run, error.code, error.message)
        db.commit()
        return run.id
    run.status, run.ml_job_id, run.submitted_at, run.polled_at = TrainingStatus.TRAINING, job["jobId"], _now(), None
    db.commit()
    return run.id


def eligibility_placeholder() -> dict:
    return {"eligible": False, "checks": {}}


def poll_running(db: Session, force: bool = False) -> int:
    """Check training jobs at the ML service. Returns how many finished."""
    settings = get_settings()
    now = _now()
    finished = 0
    runs = list(db.scalars(select(TrainingRun).where(TrainingRun.status == TrainingStatus.TRAINING)
                           .with_for_update(skip_locked=True)))
    for run in runs:
        if not force and run.polled_at and (now - run.polled_at).total_seconds() < settings.training_poll_seconds:
            continue
        run.polled_at = now
        try:
            job = get_ml_client().train_status(run.ml_job_id)
        except MlServiceError as error:
            if error.code == "JOB_NOT_FOUND":  # the ML service restarted and lost its in-memory job
                if run.attempts < MAX_ATTEMPTS:
                    run.status, run.ml_job_id = TrainingStatus.QUEUED, None
                else:
                    _fail(run, "TRAINING_LOST", "The training job was lost. Try again.")
            elif run.submitted_at and now - run.submitted_at > dt.timedelta(minutes=settings.training_max_minutes):
                _fail(run, "TRAINING_TIMEOUT", "Training took too long. Try again.")
            continue
        if job["status"] == "COMPLETED":
            result = job.get("result") or {}
            _finish(run, result.get("status", "FAILED"), result)
            finished += 1
        elif job["status"] == "FAILED":
            error = job.get("error") or {}
            _fail(run, error.get("code", "TRAINING_FAILED"), error.get("message", "Training failed."))
            finished += 1
        elif run.submitted_at and now - run.submitted_at > dt.timedelta(minutes=settings.training_max_minutes):
            _fail(run, "TRAINING_TIMEOUT", "Training took too long. Try again.")
    db.commit()
    if finished:
        models_info.clear_cache()
    return finished


def _finish(run: TrainingRun, outcome: str, result: dict) -> None:
    run.status, run.outcome, run.finished_at = TrainingStatus.COMPLETED, outcome, _now()
    run.result = {k: result.get(k) for k in ("eligibility", "backtest", "gate", "recursive", "horizons",
                                             "bestIteration", "historyStartDate", "historyEndDate") if k in result}
    model = result.get("model")
    if outcome == "TRAINED" and model:
        run.model_id, run.model_version = model.get("id"), model.get("version")


def _fail(run: TrainingRun, code: str, message: str) -> None:
    run.status, run.error_code, run.error_message, run.finished_at = TrainingStatus.FAILED, code, message, _now()


def schedule_retraining(db: Session) -> int:
    """Weekly retrain (spec §11.6): workspaces with a trained model whose history changed since."""
    cutoff = _now() - dt.timedelta(days=get_settings().retrain_after_days)
    latest = (select(TrainingRun.workspace_id, func.max(TrainingRun.created_at).label("last"))
              .group_by(TrainingRun.workspace_id).subquery())
    candidates = db.execute(select(TrainingRun).join(
        latest, (latest.c.workspace_id == TrainingRun.workspace_id) & (latest.c.last == TrainingRun.created_at))
        .where(TrainingRun.created_at < cutoff, TrainingRun.status.not_in(ACTIVE))).scalars().all()
    queued = 0
    for previous in candidates:
        ever_trained = db.scalar(select(func.count()).select_from(TrainingRun).where(
            TrainingRun.workspace_id == previous.workspace_id, TrainingRun.outcome == "TRAINED"))
        if not ever_trained or previous.history_versions == history_versions(db, previous.workspace_id):
            continue
        store = db.get(Store, previous.store_id) if previous.store_id else None
        store = store or next(iter(_stores(db, previous.workspace_id)), None)
        if store is None:
            continue
        _, created = request_training(db, None, store, trigger="SCHEDULED")
        queued += created
    return queued


def retry(db: Session, run: TrainingRun) -> None:
    if run.status != TrainingStatus.FAILED:
        raise ApiError("NOT_RETRYABLE", "Only failed training runs can be retried.", 409)
    run.status, run.error_code, run.error_message, run.attempts = TrainingStatus.QUEUED, None, None, 0
    run.ml_job_id, run.submitted_at, run.finished_at = None, None, None
    db.commit()
