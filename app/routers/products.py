"""Products, sales history and data readiness for a store."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user, product_in_store, require, store_access
from app.errors import ApiError
from app.models import Product, User
from app.schemas import (
    DemandClassOut, HistoryOut, HistoryPoint, ProductIn, ProductOut, ProductPage, ProductPatch, ProductReadiness,
    ReadinessOut,
)
from app.services import models_info
from app.services.history import LOOKBACK_DAYS, MAX_MISSING_DAYS, load_history, product_readiness

router = APIRouter(prefix="/v1/stores/{store_id}", tags=["products"])


def product_out(p: Product) -> ProductOut:
    demand = None
    if p.demand_class:
        demand = DemandClassOut(class_=p.demand_class, label=p.demand_label, adi=p.demand_adi, cv2=p.demand_cv2,
                                zero_share=p.demand_zero_share)
    return ProductOut(id=p.id, sku=p.sku, name=p.name, category=p.category, unit_of_measure=p.unit_of_measure,
                      default_price=float(p.default_price) if p.default_price is not None else None,
                      is_active=p.is_active, first_sale_date=p.first_sale_date, demand_class=demand)


@router.get("/products", response_model=ProductPage, response_model_by_alias=True)
def list_products(store_id: uuid.UUID, q: str | None = Query(default=None, max_length=80),
                  category: str | None = None, demand_class: str | None = Query(default=None, alias="demandClass"),
                  page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200, alias="pageSize"),
                  user: User = Depends(current_user), db: Session = Depends(get_db)) -> ProductPage:
    store, _ = store_access(db, user, store_id)
    query = select(Product).where(Product.workspace_id == store.workspace_id)
    if q:
        cleaned = q.replace("\\", "").replace("%", "").replace("_", "\\_")
        like = f"%{cleaned}%"
        query = query.where(or_(Product.sku.ilike(like), Product.name.ilike(like)))
    if category:
        query = query.where(Product.category == category)
    if demand_class:
        query = query.where(Product.demand_class == demand_class.upper())
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Product.sku).offset((page - 1) * page_size).limit(page_size))
    return ProductPage(page=page, page_size=page_size, total=total, items=[product_out(p) for p in items])


@router.post("/products", response_model=ProductOut, status_code=201, response_model_by_alias=True)
def create_product(store_id: uuid.UUID, body: ProductIn, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> ProductOut:
    store, access = store_access(db, user, store_id)
    require(access.can_edit, "The demo store is read-only." if access.is_reference else
            "Only owners and managers can add products.")
    product = Product(workspace_id=store.workspace_id, **body.model_dump())
    db.add(product)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError("SKU_TAKEN", "Another product already uses that SKU.", 409) from None
    return product_out(product)


@router.get("/products/{product_id}", response_model=ProductOut, response_model_by_alias=True)
def get_product(store_id: uuid.UUID, product_id: uuid.UUID, user: User = Depends(current_user),
                db: Session = Depends(get_db)) -> ProductOut:
    store, _ = store_access(db, user, store_id)
    return product_out(product_in_store(db, store, product_id))


@router.patch("/products/{product_id}", response_model=ProductOut, response_model_by_alias=True)
def update_product(store_id: uuid.UUID, product_id: uuid.UUID, body: ProductPatch,
                   user: User = Depends(current_user), db: Session = Depends(get_db)) -> ProductOut:
    store, access = store_access(db, user, store_id)
    require(access.can_edit, "The demo store is read-only." if access.is_reference else
            "Only owners and managers can edit products.")
    product = product_in_store(db, store, product_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    db.commit()
    return product_out(product)


@router.get("/products/{product_id}/history", response_model=HistoryOut, response_model_by_alias=True)
def history(store_id: uuid.UUID, product_id: uuid.UUID, from_: dt.date | None = Query(default=None, alias="from"),
            to: dt.date | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> HistoryOut:
    store, _ = store_access(db, user, store_id)
    product = product_in_store(db, store, product_id)
    to = to or store.history_end or dt.date.today()
    from_ = from_ or to - dt.timedelta(days=27)
    if from_ > to or (to - from_).days > 730:
        raise ApiError("INVALID_RANGE", "Choose a range of up to two years, with 'from' before 'to'.")
    rows = {r.sale_date: r for r in load_history(db, store.id, product.id, from_, to)}
    points = []
    for offset in range((to - from_).days + 1):
        day = from_ + dt.timedelta(days=offset)
        row = rows.get(day)
        points.append(HistoryPoint(date=day, units=float(row.units_sold) if row else None,
                                   price=float(row.unit_price) if row and row.unit_price is not None else None))
    return HistoryOut(product_id=product.id, from_=from_, to=to, points=points)


@router.get("/readiness", response_model=ReadinessOut, response_model_by_alias=True)
def readiness(store_id: uuid.UUID, as_of_date: dt.date | None = Query(default=None, alias="asOfDate"),
              user: User = Depends(current_user), db: Session = Depends(get_db)) -> ReadinessOut:
    store, _ = store_access(db, user, store_id)
    as_of = as_of_date or store.history_end
    versions = models_info.versions_for(store.model_id) if store.model_id else []
    caps = versions[-1]["capabilities"] if versions else {}
    min_days = caps.get("minHistoryDays", LOOKBACK_DAYS)
    max_missing = caps.get("maxMissingDaysInLookback", MAX_MISSING_DAYS)
    products = {p.id: p for p in db.scalars(select(Product).where(
        Product.workspace_id == store.workspace_id, Product.is_active))}
    checks = product_readiness(db, store, list(products), as_of, min_days, max_missing) if as_of else {}
    items = [ProductReadiness(product_id=pid, sku=products[pid].sku, ready=c.ready, reasons=c.reasons,
                              recorded_days_in_lookback=c.recorded_days, missing_days_in_lookback=c.missing_days,
                              latest_date=c.latest_date) for pid, c in checks.items()]
    items.sort(key=lambda r: (r.ready, r.sku))
    stale = bool(store.history_end and not store.workspace.is_reference
                 and store.history_end < dt.date.today() - dt.timedelta(days=1))
    return ReadinessOut(store_id=store.id, as_of_date=as_of, min_history_days=min_days,
                        max_missing_days_in_lookback=max_missing, stale=stale,
                        products_ready=sum(r.ready for r in items), products_total=len(items), products=items)
