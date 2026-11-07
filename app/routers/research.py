"""Research export (spec §12.1): every forecast point with its actual, for the AI-vs-traditional comparison.

Research users only. Rows follow the same visibility as forecast history: in the demo workspace, the
user's own runs; in their own workspaces, every run.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user, require, store_access
from app.models import ForecastPoint, ForecastRun, Product, RunStatus, User

router = APIRouter(prefix="/v1/research", tags=["research"])

COLUMNS = ["forecast_id", "store", "sku", "demand_class", "as_of_date", "is_replay", "method", "model_id",
           "model_version", "feature_schema", "horizon_days", "day_ahead", "date", "ai", "naive_last_value",
           "seasonal_naive_7", "actual", "ai_abs_error", "naive_abs_error", "seasonal_abs_error",
           "price_assumption", "created_at"]


def _rows(db: Session, user: User, store_id: uuid.UUID, start: dt.date | None, end: dt.date | None):
    store, access = store_access(db, user, store_id)
    require(user.is_research, "The research export is for research users.")
    query = (select(ForecastRun, ForecastPoint, Product)
             .join(ForecastPoint, ForecastPoint.run_id == ForecastRun.id)
             .join(Product, Product.id == ForecastRun.product_id)
             .where(ForecastRun.store_id == store.id, ForecastRun.status == RunStatus.COMPLETED,
                    ForecastRun.deleted_at.is_(None)))
    if access.is_reference:
        query = query.where(ForecastRun.requested_by == user.id)
    if start:
        query = query.where(ForecastRun.as_of_date >= start)
    if end:
        query = query.where(ForecastRun.as_of_date <= end)
    return store, db.execute(query.order_by(ForecastRun.created_at, ForecastPoint.point_date))


def _err(forecast, actual):
    return None if forecast is None or actual is None else abs(forecast - actual)


@router.get("/export")
def export(store_id: uuid.UUID = Query(alias="storeId"), start: dt.date | None = Query(None, alias="from"),
           end: dt.date | None = Query(None, alias="to"), user: User = Depends(current_user),
           db: Session = Depends(get_db)) -> StreamingResponse:
    store, rows = _rows(db, user, store_id, start, end)

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(COLUMNS)
        for run, point, product in rows:
            writer.writerow([
                run.id, store.code, product.sku, (run.demand_class or {}).get("class"), run.as_of_date,
                run.is_replay, run.method.value, run.model_id, run.model_version, run.feature_schema,
                run.horizon_days, (point.point_date - run.as_of_date).days, point.point_date,
                _fmt(point.ai), _fmt(point.naive_last_value), _fmt(point.seasonal_naive_7), _fmt(point.actual),
                _fmt(_err(point.ai, point.actual)), _fmt(_err(point.naive_last_value, point.actual)),
                _fmt(_err(point.seasonal_naive_7, point.actual)), run.price_assumption, run.created_at.isoformat()])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()

    filename = f"research_{store.code}_{dt.date.today()}.csv"
    return StreamingResponse(generate(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/summary")
def summary(store_id: uuid.UUID = Query(alias="storeId"), start: dt.date | None = Query(None, alias="from"),
            end: dt.date | None = Query(None, alias="to"), user: User = Depends(current_user),
            db: Session = Depends(get_db)) -> dict:
    """AI vs baselines on every forecast day whose actual is known, overall, by demand class and by day ahead.

    Only points where the AI, both baselines and the actual all exist are counted, so every method is
    compared on the same days. Each run counts once per forecast day, so products forecast more often
    weigh more; the export has the raw rows for other weightings.
    """
    store, rows = _rows(db, user, store_id, start, end)
    groups: dict[tuple[str, str], list[tuple[float, float, float, float]]] = defaultdict(list)
    runs = set()
    for run, point, _ in rows:
        if None in (point.ai, point.naive_last_value, point.seasonal_naive_7, point.actual):
            continue
        values = (point.actual, point.ai, point.naive_last_value, point.seasonal_naive_7)
        runs.add(run.id)
        groups[("overall", "all")].append(values)
        groups[("demandClass", (run.demand_class or {}).get("class") or "UNKNOWN")].append(values)
        groups[("dayAhead", str((point.point_date - run.as_of_date).days))].append(values)

    def metrics(values):
        n = len(values)
        out = {"n": n}
        for i, name in ((1, "ai"), (2, "naive_last_value"), (3, "seasonal_naive_7")):
            errors = [v[i] - v[0] for v in values]
            total = sum(v[0] for v in values)
            out[name] = {"mae": sum(abs(e) for e in errors) / n, "rmse": math.sqrt(sum(e * e for e in errors) / n),
                         "wape": sum(abs(e) for e in errors) / total if total else None}
        return out

    result: dict = {"storeId": str(store.id), "runs": len(runs), "overall": None, "byDemandClass": {}, "byDayAhead": {},
                    "note": "Lower error means better on these days, not that it will be better tomorrow."}
    for (kind, key), values in sorted(groups.items()):
        if kind == "overall":
            result["overall"] = metrics(values)
        elif kind == "demandClass":
            result["byDemandClass"][key] = metrics(values)
        else:
            result["byDayAhead"][key] = metrics(values)
    return result


def _fmt(value) -> str:
    return "" if value is None else f"{value:.4f}"


