"""Own-data model training (spec §9.4, §12.1 `POST /v1/stores/{sid}/models/train`)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user, require, store_access, workspace_access
from app.errors import ApiError, not_found
from app.models import Store, TrainingRun, TrainingStatus, User
from app.schemas import EligibilityOut, ModelRef, TrainingRunOut, TrainingRunPage
from app.services import training

router = APIRouter(prefix="/v1", tags=["training"])
RETRYABLE = {"ML_SERVICE_UNAVAILABLE", "TRAINING_LOST", "TRAINING_TIMEOUT"}


def run_out(run: TrainingRun) -> TrainingRunOut:
    result = run.result or {}
    recursive = result.get("recursive") or None
    accuracy = None
    if recursive:
        accuracy = {"validated": recursive.get("validated"),
                    "byDay": [{"day": d["day"], "aiRmse": d["ai"]["rmse"], "aiMae": d["ai"]["mae"],
                               "naiveRmse": d["naive_last_value"]["rmse"], "naiveMae": d["naive_last_value"]["mae"]}
                              for d in recursive.get("byDay", [])]}
    return TrainingRunOut(
        id=run.id, workspace_id=run.workspace_id, status=run.status.value, outcome=run.outcome, trigger=run.trigger,
        created_at=run.created_at, submitted_at=run.submitted_at, finished_at=run.finished_at,
        history_rows=run.history_rows,
        model=ModelRef(id=run.model_id, version=run.model_version) if run.model_version else None,
        horizons=result.get("horizons"), eligibility=result.get("eligibility"), backtest=result.get("backtest"),
        gate=result.get("gate"), horizon_accuracy=accuracy,
        error={"code": run.error_code, "message": run.error_message} if run.error_code else None,
        retryable=run.status == TrainingStatus.FAILED and run.error_code in RETRYABLE)


def _own_store(db: Session, user: User, store_id: uuid.UUID, edit: bool = False) -> Store:
    store, access = store_access(db, user, store_id)
    if access.is_reference:
        raise ApiError("REFERENCE_MODEL_FIXED", "The demo store uses the fixed Reference model; it isn't retrained.",
                       409)
    if edit:
        require(access.can_edit, "Only owners and managers can train models.")
    return store


@router.get("/stores/{store_id}/models/eligibility", response_model=EligibilityOut, response_model_by_alias=True)
def get_eligibility(store_id: uuid.UUID, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> EligibilityOut:
    store = _own_store(db, user, store_id)
    result = training.eligibility(db, store.workspace_id)
    latest = training.latest_run(db, store.workspace_id)
    codes = [s.code for s in db.scalars(select(Store).where(Store.workspace_id == store.workspace_id)
                                        .order_by(Store.code))]
    return EligibilityOut(workspace_id=store.workspace_id, eligible=result["eligible"], checks=result["checks"],
                          rules=result.get("rules"), stores_in_model=codes,
                          latest_run=run_out(latest) if latest else None)


@router.post("/stores/{store_id}/models/train", response_model=TrainingRunOut, status_code=202,
             response_model_by_alias=True)
def start_training(store_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    store = _own_store(db, user, store_id, edit=True)
    check = training.eligibility(db, store.workspace_id)
    if not check["eligible"]:
        raise ApiError("NOT_ELIGIBLE", "This workspace doesn't have enough sales history to train its own model yet.",
                       details={"checks": check["checks"]})
    run, created = training.request_training(db, user, store)
    body = run_out(run).model_dump(mode="json", by_alias=True)
    return JSONResponse(body, status_code=202 if created else 200)


@router.get("/stores/{store_id}/models/training-runs", response_model=TrainingRunPage, response_model_by_alias=True)
def list_runs(store_id: uuid.UUID, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100,
                                                                                         alias="pageSize"),
              user: User = Depends(current_user), db: Session = Depends(get_db)) -> TrainingRunPage:
    store = _own_store(db, user, store_id)
    query = select(TrainingRun).where(TrainingRun.workspace_id == store.workspace_id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    runs = db.scalars(query.order_by(TrainingRun.created_at.desc()).offset((page - 1) * page_size).limit(page_size))
    return TrainingRunPage(page=page, page_size=page_size, total=total, items=[run_out(r) for r in runs])


def _run_for_user(db: Session, user: User, run_id: uuid.UUID) -> TrainingRun:
    run = db.get(TrainingRun, run_id)
    if run is None:
        raise not_found("Training run")
    try:
        workspace_access(db, user, run.workspace_id)
    except ApiError:
        raise not_found("Training run") from None
    return run


@router.get("/training-runs/{run_id}", response_model=TrainingRunOut, response_model_by_alias=True)
def get_run(run_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> TrainingRunOut:
    return run_out(_run_for_user(db, user, run_id))


@router.post("/training-runs/{run_id}/retry", response_model=TrainingRunOut, status_code=202,
             response_model_by_alias=True)
def retry_run(run_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> TrainingRunOut:
    run = _run_for_user(db, user, run_id)
    require(workspace_access(db, user, run.workspace_id).can_edit, "Only owners and managers can train models.")
    training.retry(db, run)
    return run_out(run)
