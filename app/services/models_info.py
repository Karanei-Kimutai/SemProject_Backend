"""What the ML service can do for a store: model versions, horizons and as-of ranges (spec §8.8, §12.1)."""

from __future__ import annotations

import datetime as dt
import threading
import time

from app.errors import ApiError
from app.ml_client import MlServiceError, get_ml_client
from app.models import Store

_CACHE_SECONDS = 60
_lock = threading.Lock()
_cache: tuple[float, list[dict]] | None = None


def ml_models(force: bool = False) -> list[dict]:
    global _cache
    with _lock:
        if not force and _cache and time.monotonic() - _cache[0] < _CACHE_SECONDS:
            return _cache[1]
    try:
        models = get_ml_client().models()
    except MlServiceError as error:
        raise ApiError("ML_SERVICE_UNAVAILABLE", "The forecasting service is unavailable. Try again shortly.", 503) from error
    with _lock:
        _cache = (time.monotonic(), models)
    return models


def clear_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def versions_for(model_id: str | None) -> list[dict]:
    """All versions of one model family, oldest training end first."""
    if not model_id:
        return []
    versions = [m for m in ml_models() if m["id"] == model_id]
    return sorted(versions, key=lambda m: m["trainingEndDate"])


def resolve_local(model_id: str | None, as_of: dt.date) -> dict | None:
    """The leakage rule, applied to the cached model list: latest version trained on or before as_of."""
    eligible = [m for m in versions_for(model_id) if dt.date.fromisoformat(m["trainingEndDate"]) <= as_of]
    return eligible[-1] if eligible else None


def as_of_range(store: Store, versions: list[dict]) -> dict:
    default = store.history_end
    if not store.workspace.is_reference or not versions or default is None:
        return {"default": default, "editable": False}
    ends = [dt.date.fromisoformat(v["trainingEndDate"]) for v in versions]
    latest = versions[-1]
    calendar_end = latest["capabilities"].get("calendarEndDate")
    # Replays: dates with known actuals, served by the version trained just before the backtest window.
    replay_min = ends[-2] if len(ends) >= 2 else ends[-1]
    return {
        "default": default,
        "editable": True,
        "replayMin": replay_min,
        "replayMax": default - dt.timedelta(days=1),
        "researchMin": ends[0],
        "calendarEnd": dt.date.fromisoformat(calendar_end) if calendar_end else None,
    }


def baseline_labels() -> list[dict]:
    return [{"id": "naive_last_value", "label": "Same as last day"},
            {"id": "seasonal_naive_7", "label": "Same day last week"}]
