"""Load the Reference (demo) workspace from the files `python -m mlengine export-reference` writes.

Idempotent: running it again replaces the Reference store's products, sales and future prices.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import FuturePrice, Product, SalesDaily, Store, Workspace
from app.services.history import refresh_store_range

log = logging.getLogger(__name__)
REFERENCE_NAME = "Demo data (M5 CA_1 FOODS)"


def _copy(db: Session, table: str, columns: list[str], rows, chunk: int = 50_000) -> int:
    """COPY rows in text format, a chunk at a time (much faster than one write per row)."""
    raw = db.connection().connection  # psycopg connection inside the session's transaction

    def fmt(value) -> str:
        return "\\N" if value is None else str(value)

    count, buffer = 0, []
    with raw.cursor() as cursor, cursor.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy:
        for row in rows:
            buffer.append("\t".join(fmt(v) for v in row))
            if len(buffer) >= chunk:
                copy.write("\n".join(buffer) + "\n")
                count += len(buffer)
                buffer.clear()
        if buffer:
            copy.write("\n".join(buffer) + "\n")
            count += len(buffer)
    return count


def seed_reference(db: Session, source: Path) -> dict:
    source = Path(source)
    meta = json.loads((source / "store.json").read_text())
    workspace = db.scalar(select(Workspace).where(Workspace.is_reference))
    if workspace is None:
        workspace = Workspace(name=REFERENCE_NAME, is_reference=True)
        db.add(workspace)
        db.flush()
    store = db.scalar(select(Store).where(Store.workspace_id == workspace.id, Store.code == meta["storeId"]))
    if store is None:
        store = Store(workspace_id=workspace.id, code=meta["storeId"], name=meta["name"], country=meta["country"],
                      subdivision=meta.get("subdivision"), timezone=meta["timezone"], currency=meta["currency"])
        db.add(store)
    store.model_id = meta.get("modelId") or get_settings().reference_model_id
    db.flush()

    db.execute(delete(SalesDaily).where(SalesDaily.store_id == store.id))
    db.execute(delete(FuturePrice).where(FuturePrice.store_id == store.id))

    existing = {p.sku: p for p in db.scalars(select(Product).where(Product.workspace_id == workspace.id))}
    products = pq.read_table(source / "products.parquet").to_pylist()
    for row in products:
        product = existing.get(row["product_id"]) or Product(workspace_id=workspace.id, sku=row["product_id"])
        product.name, product.category = row["name"], row.get("category")
        product.unit_of_measure = row.get("unit_of_measure") or "units"
        product.first_price_date = _date(row.get("first_price_date"))
        product.first_sale_date = _date(row.get("first_sale_date"))
        product.is_active = True
        db.add(product)
        existing[product.sku] = product
    db.flush()
    ids = {sku: p.id for sku, p in existing.items()}

    def sales_rows():
        table = pq.read_table(source / "sales_daily.parquet", columns=["product_id", "date", "units_sold", "unit_price"])
        for batch in table.to_batches(max_chunksize=200_000):
            cols = batch.to_pydict()
            for sku, day, units, price in zip(cols["product_id"], cols["date"], cols["units_sold"], cols["unit_price"]):
                price = None if price is None or price != price else price
                yield (store.id, ids[sku], day, units, price)

    def price_rows():
        table = pq.read_table(source / "prices_future.parquet", columns=["product_id", "date", "unit_price"])
        cols = table.to_pydict()
        for sku, day, price in zip(cols["product_id"], cols["date"], cols["unit_price"]):
            if price is not None and price == price:
                yield (store.id, ids[sku], day, price)

    n_sales = _copy(db, "sales_daily", ["store_id", "product_id", "sale_date", "units_sold", "unit_price"], sales_rows())
    n_prices = _copy(db, "future_prices", ["store_id", "product_id", "price_date", "unit_price"], price_rows())
    refresh_store_range(db, store)
    store.history_version += 1
    db.commit()
    summary = {"workspaceId": str(workspace.id), "storeId": str(store.id), "products": len(products),
               "salesRows": n_sales, "futurePriceRows": n_prices,
               "historyStart": str(store.history_start), "historyEnd": str(store.history_end)}
    log.info("seeded reference workspace: %s", summary)
    return summary


def _date(value) -> dt.date | None:
    if value in (None, "", "None", "NaT"):
        return None
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value)[:10])


def reference_store_id(db: Session) -> uuid.UUID | None:
    return db.scalar(select(Store.id).join(Workspace).where(Workspace.is_reference))
