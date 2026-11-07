"""What the model can do for a store, and its model card (spec §8.8, §8.13)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user, store_access
from app.errors import ApiError
from app.ml_client import MlServiceError, get_ml_client
from app.models import User
from app.schemas import AsOfRange, CapabilitiesOut, ModelRef
from app.services import models_info, training

router = APIRouter(prefix="/v1/stores/{store_id}/models", tags=["models"])


@router.get("/capabilities", response_model=CapabilitiesOut, response_model_by_alias=True)
def capabilities(store_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> CapabilitiesOut:
    store, access = store_access(db, user, store_id)
    run = None if access.is_reference else training.latest_run(db, store.workspace_id)
    versions = models_info.versions_for(store.model_id)
    if run is not None and run.model_version and run.model_version not in {v["version"] for v in versions}:
        models_info.clear_cache()  # a model finished training after the cache was filled
        versions = models_info.versions_for(store.model_id)
    latest = versions[-1] if versions else None
    caps = latest["capabilities"] if latest else {}
    reason = None
    if latest is None:
        if access.is_reference:
            reason = "MODEL_MISSING"
        elif run is not None and run.status.value in ("QUEUED", "TRAINING"):
            reason = "TRAINING"
        elif run is not None and run.outcome in ("GATE_FAILED", "NOT_ELIGIBLE"):
            reason = run.outcome
        else:
            reason = "NOT_TRAINED"
    elif store.history_end is None:
        reason = "NO_HISTORY"
    bounds = models_info.as_of_range(store, versions)
    if not user.is_research:
        bounds.pop("researchMin", None)
    return CapabilitiesOut(
        store_id=store.id, is_reference=access.is_reference, ai_available=reason is None,
        ai_unavailable_reason=reason,
        methods=["AI", "BASELINE", "COMPARE"] if reason is None else ["BASELINE"],
        horizons=caps.get("horizons", [1, 7]), frequency="DAILY", as_of=AsOfRange(**bounds),
        min_history_days=caps.get("minHistoryDays", 21),
        max_missing_days_in_lookback=caps.get("maxMissingDaysInLookback", 3),
        prediction_intervals=False, baselines=models_info.baseline_labels(),
        active_model=ModelRef(id=latest["id"], version=latest["version"]) if latest else None,
        training=({"runId": run.id, "status": run.status.value, "outcome": run.outcome,
                   "finishedAt": run.finished_at} if run else None))


@router.get("/active")
def active_model(store_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    store, _ = store_access(db, user, store_id)
    versions = models_info.versions_for(store.model_id)
    if not versions:
        raise ApiError("AI_NOT_AVAILABLE", "No AI model is active for this store yet.", 404)
    latest = versions[-1]
    try:
        card = get_ml_client().card(latest["id"], latest["version"])
    except MlServiceError as error:
        raise ApiError("ML_SERVICE_UNAVAILABLE", "The forecasting service is unavailable. Try again shortly.", 503) from error
    return {"model": {"id": latest["id"], "version": latest["version"], "trainingEndDate": latest["trainingEndDate"],
                      "featureSchema": latest["featureSchema"]},
            "capabilities": latest["capabilities"], "card": card}
