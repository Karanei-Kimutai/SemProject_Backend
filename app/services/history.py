"""Reading sales history and checking data readiness (design spec §8.7.4)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import FuturePrice, Product, SalesDaily, Store

LOOKBACK_DAYS = 21
MAX_MISSING_DAYS = 3


def load_history(db: Session, store_id: uuid.UUID, product_id: uuid.UUID, start: dt.date, end: dt.date) -> list[SalesDaily]:
    return list(db.scalars(
        select(SalesDaily)
        .where(SalesDaily.store_id == store_id, SalesDaily.product_id == product_id,
               SalesDaily.sale_date >= start, SalesDaily.sale_date <= end)
        .order_by(SalesDaily.sale_date)))


def load_future_prices(db: Session, store_id: uuid.UUID, product_id: uuid.UUID, start: dt.date,
                       end: dt.date) -> list[tuple[dt.date, float]]:
    """Known prices for the forecast days, as (date, price).

    Prices are planned in advance, so they may be sent for forecast days. For replays the day's price
    sits in ``sales_daily`` (only the price is read, never the units); beyond the history it comes
    from ``future_prices``. A day with neither falls back to the last known price in the ML service.
    """
    prices = {d: float(p) for d, p in db.execute(
        select(FuturePrice.price_date, FuturePrice.unit_price)
        .where(FuturePrice.store_id == store_id, FuturePrice.product_id == product_id,
               FuturePrice.price_date >= start, FuturePrice.price_date <= end))}
    prices.update({d: float(p) for d, p in db.execute(
        select(SalesDaily.sale_date, SalesDaily.unit_price)
        .where(SalesDaily.store_id == store_id, SalesDaily.product_id == product_id,
               SalesDaily.sale_date >= start, SalesDaily.sale_date <= end, SalesDaily.unit_price.is_not(None)))})
    return sorted(prices.items())


@dataclass
class Readiness:
    ready: bool
    reasons: list[str]
    recorded_days: int
    missing_days: int
    latest_date: dt.date | None


def readiness_for(first_date: dt.date | None, latest_date: dt.date | None, recorded_in_lookback: int,
                  as_of: dt.date, min_history: int = LOOKBACK_DAYS, max_missing: int = MAX_MISSING_DAYS) -> Readiness:
    """Mirror the ML service's per-series checks so the app can explain problems before a job runs."""
    missing = min_history - recorded_in_lookback
    reasons = []
    if first_date is None or first_date > as_of or (as_of - first_date).days + 1 < min_history:
        reasons.append("INSUFFICIENT_HISTORY")
    elif missing > max_missing:
        reasons.append("TOO_MANY_GAPS")
    if latest_date is not None and latest_date < as_of:
        reasons.append("STALE_HISTORY")
    return Readiness(not reasons, reasons, recorded_in_lookback, max(missing, 0), latest_date)


def product_readiness(db: Session, store: Store, product_ids: list[uuid.UUID] | None, as_of: dt.date,
                      min_history: int = LOOKBACK_DAYS, max_missing: int = MAX_MISSING_DAYS) -> dict[uuid.UUID, Readiness]:
    lookback_start = as_of - dt.timedelta(days=min_history - 1)
    stats = (
        select(SalesDaily.product_id,
               func.min(SalesDaily.sale_date),
               func.max(SalesDaily.sale_date),
               func.count().filter(SalesDaily.sale_date >= lookback_start))
        .where(SalesDaily.store_id == store.id, SalesDaily.sale_date <= as_of)
        .group_by(SalesDaily.product_id))
    if product_ids is not None:
        stats = stats.where(SalesDaily.product_id.in_(product_ids))
    found = {pid: readiness_for(first, latest, recorded, as_of, min_history, max_missing)
             for pid, first, latest, recorded in db.execute(stats)}
    ids = product_ids if product_ids is not None else list(
        db.scalars(select(Product.id).where(Product.workspace_id == store.workspace_id, Product.is_active)))
    empty = Readiness(False, ["INSUFFICIENT_HISTORY"], 0, min_history, None)
    return {pid: found.get(pid, empty) for pid in ids}


def refresh_store_range(db: Session, store: Store) -> None:
    first, last = db.execute(select(func.min(SalesDaily.sale_date), func.max(SalesDaily.sale_date))
                             .where(SalesDaily.store_id == store.id)).one()
    store.history_start, store.history_end = first, last
