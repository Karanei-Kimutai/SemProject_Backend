"""CSV sales imports (design spec §8.6–8.7).

Flow: upload → map columns (validates into ``import_rows``) → confirm (merge into ``sales_daily``) → optional delete.

Rules that matter for the model:
* A recorded zero stays 0. A day with no row is *not recorded*, unless the user answered "no row means
  zero sales", in which case each product's missing days from its first day in the file to the file's
  last day are stored as 0. Store-closed days are never zero-filled.
* Duplicate product/store/day rows are summed and reported, never dropped.
* Spikes (> 10× the product's recent median) are flagged, never removed: the model trained on uncapped data.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import math
import re
import statistics
import uuid
import zoneinfo
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import ApiError
from app.models import (
    ImportIssue, ImportRow, ImportStatus, Product, SalesDaily, SalesImport, Store, Workspace,
)
from app.services.history import refresh_store_range

FIELDS = ["salesDate", "productId", "productName", "unitsSold", "storeId", "unitPrice", "category",
          "unitOfMeasure", "storeClosed"]
STORED_NOT_USED = ["revenue", "promotion", "promotionflag", "inventory", "inventoryonhand", "stock", "returns",
                   "returnsquantity"]
_SYNONYMS = {
    "salesDate": ["salesdate", "saledate", "date", "day", "transactiondate", "orderdate", "businessdate"],
    "productId": ["productid", "sku", "itemid", "productcode", "itemcode", "barcode", "item", "articleid"],
    "productName": ["productname", "itemname", "name", "description", "productdescription", "product"],
    "unitsSold": ["unitssold", "units", "quantity", "qty", "quantitysold", "qtysold", "unitsales", "sold"],
    "storeId": ["storeid", "storecode", "store", "branch", "branchid", "location", "outlet"],
    "unitPrice": ["unitprice", "price", "sellprice", "sellingprice", "saleprice"],
    "category": ["category", "department", "dept", "categoryname", "productcategory", "group"],
    "unitOfMeasure": ["unitofmeasure", "uom", "unit", "measure"],
    "storeClosed": ["storeclosed", "closed", "isclosed"],
}
DATE_FORMATS = {"YYYY-MM-DD": "%Y-%m-%d", "DD/MM/YYYY": "%d/%m/%Y", "MM/DD/YYYY": "%m/%d/%Y",
                "YYYY/MM/DD": "%Y/%m/%d", "DD-MM-YYYY": "%d-%m-%Y", "DD.MM.YYYY": "%d.%m.%Y"}
SPIKE_FACTOR = 10
SPIKE_WINDOW = 28
MAX_STORED_ISSUES = 5000
TRUTHY = {"true", "1", "yes", "y", "closed", "t"}


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---- reading ------------------------------------------------------------------------------

def decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ApiError("FILE_NOT_UTF8", "Save the file as CSV UTF-8 and upload it again.") from None


def sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def read_rows(text_content: str, delimiter: str):
    return csv.reader(io.StringIO(text_content, newline=""), delimiter=delimiter)


def suggest_mapping(columns: list[str]) -> dict[str, dict]:
    keys = {_key(c): c for c in columns}
    used: set[str] = set()
    suggestion: dict[str, dict] = {}
    for fld in FIELDS:
        for rank, synonym in enumerate(_SYNONYMS[fld]):
            column = keys.get(synonym)
            if column and column not in used:
                suggestion[fld] = {"column": column, "confidence": "HIGH" if rank < 3 else "CHECK"}
                used.add(column)
                break
    return suggestion


def detect_date_formats(values: list[str]) -> list[str]:
    """Formats that parse the most sampled values (a few typos don't hide the format).

    More than one result means the file is ambiguous (for example 01/02/2026), and the user must choose.
    """
    values = [v.strip() for v in values if v and v.strip()][:1000]
    counts = {}
    for label, fmt in DATE_FORMATS.items():
        ok = 0
        for v in values:
            try:
                dt.datetime.strptime(v, fmt)
                ok += 1
            except ValueError:
                pass
        counts[label] = ok
    best = max(counts.values(), default=0)
    if best == 0 or best < 0.5 * len(values):
        return []
    return [label for label, ok in counts.items() if ok == best]


def inspect_upload(content: bytes) -> dict:
    """Header, delimiter, suggested mapping and a raw preview, for the mapping screen."""
    settings = get_settings()
    if not content.strip():
        raise ApiError("EMPTY_FILE", "The file is empty.")
    if len(content) > settings.import_max_bytes:
        raise ApiError("FILE_TOO_LARGE", f"Files can be up to {settings.import_max_bytes // (1024 * 1024)} MB.", 413)
    body = decode(content)
    delimiter = sniff_delimiter(body[:20000])
    reader = read_rows(body, delimiter)
    header = [h.strip() for h in next(reader, [])]
    if not any(header):
        raise ApiError("NO_HEADER", "The first line must contain column names.")
    if len(set(_key(h) for h in header)) != len(header) or any(not h for h in header):
        raise ApiError("BAD_HEADER", "Every column needs a different, non-empty name.")
    preview, date_values = [], []
    suggestion = suggest_mapping(header)
    date_index = header.index(suggestion["salesDate"]["column"]) if "salesDate" in suggestion else None
    for i, row in enumerate(reader):
        if i < 5:
            preview.append(dict(zip(header, row)))
        if date_index is not None and date_index < len(row):
            date_values.append(row[date_index])
        if i >= 1000:
            break
    formats = detect_date_formats(date_values)
    return {
        "delimiter": delimiter, "columns": header, "preview": preview, "suggestedMapping": suggestion,
        "dateFormats": formats, "suggestedDateFormat": formats[0] if len(formats) == 1 else None,
        "dateFormatAmbiguous": len(formats) > 1,
        "unusedColumns": [h for h in header if _key(h) in STORED_NOT_USED],
    }


# ---- validation ---------------------------------------------------------------------------

@dataclass
class _Daily:
    units: float = 0.0
    price: float | None = None
    name: str | None = None
    category: str | None = None
    uom: str | None = None
    first_row: int | None = None
    rows: int = 0


@dataclass
class _Validation:
    issues: list[ImportIssue] = field(default_factory=list)
    errors_by_code: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    warnings_by_code: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    rejected_rows: set[int] = field(default_factory=set)
    warned_rows: set[int] = field(default_factory=set)

    def add(self, import_id, severity, code, message, row=None, column=None, value=None):
        (self.errors_by_code if severity == "ERROR" else self.warnings_by_code)[code] += 1
        if row is not None:
            (self.rejected_rows if severity == "ERROR" else self.warned_rows).add(row)
        if len(self.issues) < MAX_STORED_ISSUES:
            self.issues.append(ImportIssue(import_id=import_id, severity=severity, code=code, row_number=row,
                                           column=column, value=None if value is None else str(value)[:200],
                                           message=message))


def _number(value: str) -> float | None:
    value = value.strip().replace(" ", "")
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        raise ValueError from None
    if math.isnan(number) or math.isinf(number):
        raise ValueError
    return number


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").upper()[:80]


def validate(db: Session, upload: SalesImport, store: Store, mapping: dict[str, str], date_format: str | None,
             missing_days_are_zero: bool) -> None:
    """Parse the whole file with this mapping into import_rows and import_issues; set status and summary."""
    columns = upload.columns
    unknown = [c for c in mapping.values() if c not in columns]
    if unknown:
        raise ApiError("UNKNOWN_COLUMN", f"These columns aren't in the file: {', '.join(unknown)}.")
    if len(set(mapping.values())) != len(mapping):
        raise ApiError("COLUMN_MAPPED_TWICE", "Each column can be mapped to one field only.")
    missing = [f for f in ("salesDate", "unitsSold") if f not in mapping]
    if "productId" not in mapping and "productName" not in mapping:
        missing.append("productId")
    if missing:
        raise ApiError("REQUIRED_FIELDS_UNMAPPED", "Map the required fields: date, product and units sold.",
                       details={"fields": missing})
    formats = upload.detected.get("dateFormats", [])
    if date_format is None:
        if len(formats) != 1:
            raise ApiError("DATE_FORMAT_REQUIRED", "Choose the date format used in the file.",
                           details={"options": formats or list(DATE_FORMATS)})
        date_format = formats[0]
    if date_format not in DATE_FORMATS:
        raise ApiError("INVALID_REQUEST", "Unknown date format.", details={"options": list(DATE_FORMATS)})
    strptime_format = DATE_FORMATS[date_format]

    db.execute(delete(ImportRow).where(ImportRow.import_id == upload.id))
    db.execute(delete(ImportIssue).where(ImportIssue.import_id == upload.id))

    stores_by_code = {s.code: s for s in db.scalars(select(Store).where(Store.workspace_id == store.workspace_id))}
    today = {s.id: dt.datetime.now(zoneinfo.ZoneInfo(s.timezone)).date() for s in stores_by_code.values()}
    index = {fld: columns.index(col) for fld, col in mapping.items()}
    check = _Validation()
    daily: dict[tuple[uuid.UUID, str, dt.date], _Daily] = {}
    closed: set[tuple[uuid.UUID, dt.date]] = set()
    rows_read = 0
    limit = get_settings().import_max_rows

    reader = read_rows(decode(upload.content), upload.detected["delimiter"])
    next(reader, None)
    for line, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        rows_read += 1
        if rows_read > limit:
            raise ApiError("TOO_MANY_ROWS", f"Files can have up to {limit:,} rows. Split the file and upload each part.")

        def cell(fld: str) -> str:
            i = index.get(fld)
            return row[i].strip() if i is not None and i < len(row) else ""

        def reject(code, message, fld=None):
            check.add(upload.id, "ERROR", code, message, line, mapping.get(fld) if fld else None,
                      cell(fld) if fld else None)

        raw_date = cell("salesDate")
        try:
            day = dt.datetime.strptime(raw_date, strptime_format).date()
        except ValueError:
            reject("INVALID_DATE", f"Row {line}: '{raw_date}' isn't a date in {date_format} format.", "salesDate")
            continue

        target = store
        if "storeId" in mapping and cell("storeId"):
            target = stores_by_code.get(cell("storeId"))
            if target is None:
                reject("UNKNOWN_STORE", f"Row {line}: store '{cell('storeId')}' doesn't exist in this workspace.",
                       "storeId")
                continue
        if day > today[target.id]:
            reject("FUTURE_DATE", f"Row {line}: {day} is in the future.", "salesDate")
            continue
        if "storeClosed" in mapping and cell("storeClosed").lower() in TRUTHY:
            closed.add((target.id, day))
            continue

        sku, name = cell("productId"), cell("productName")
        if not sku and name:
            sku = _slug(name)
        if not sku:
            reject("MISSING_PRODUCT", f"Row {line}: the product is missing.",
                   "productId" if "productId" in mapping else "productName")
            continue
        if len(sku) > 80:
            reject("PRODUCT_ID_TOO_LONG", f"Row {line}: product IDs can be up to 80 characters.", "productId")
            continue

        try:
            units = _number(cell("unitsSold"))
        except ValueError:
            reject("INVALID_UNITS", f"Row {line}: units sold must be a number.", "unitsSold")
            continue
        if units is None:
            reject("MISSING_UNITS", f"Row {line}: units sold is empty. Units are never worked out from revenue.",
                   "unitsSold")
            continue
        if units < 0:
            reject("NEGATIVE_UNITS", f"Row {line}: units sold can't be negative. Leave returns out of this file.",
                   "unitsSold")
            continue

        price = None
        if "unitPrice" in mapping and cell("unitPrice"):
            try:
                price = _number(cell("unitPrice"))
            except ValueError:
                check.add(upload.id, "WARNING", "INVALID_PRICE",
                          f"Row {line}: the price isn't a number, so it was left out.", line, mapping["unitPrice"],
                          cell("unitPrice"))
            if price is not None and price <= 0:
                check.add(upload.id, "WARNING", "NON_POSITIVE_PRICE",
                          f"Row {line}: prices must be above 0, so it was left out.", line, mapping["unitPrice"],
                          cell("unitPrice"))
                price = None

        entry = daily.setdefault((target.id, sku, day), _Daily(first_row=line))
        entry.units += units
        entry.rows += 1
        entry.price = price if price is not None else entry.price
        entry.name = entry.name or name or None
        entry.category = entry.category or cell("category") or None
        entry.uom = entry.uom or cell("unitOfMeasure") or None

    aggregated = 0
    for (sid, sku, day), entry in daily.items():
        if entry.rows > 1:
            aggregated += entry.rows
            check.add(upload.id, "WARNING", "DUPLICATE_ROWS_SUMMED",
                      f"{entry.rows} rows for {sku} on {day} were added together ({entry.units:g} units).",
                      entry.first_row, mapping.get("unitsSold"))

    zero_filled = _zero_fill(daily, closed) if missing_days_are_zero else 0
    _flag_spikes(db, upload.id, daily, check, mapping)

    db.add_all(check.issues)
    db.flush()
    db.bulk_insert_mappings(ImportRow, [
        {"import_id": upload.id, "store_id": sid, "sku": sku, "sale_date": day, "units_sold": e.units,
         "unit_price": e.price, "product_name": e.name, "category": e.category, "unit_of_measure": e.uom,
         "first_row": e.first_row, "source_rows": e.rows, "zero_filled": e.rows == 0}
        for (sid, sku, day), e in daily.items()])

    accepted = rows_read - len(check.rejected_rows)
    known = set(db.scalars(select(Product.sku).where(Product.workspace_id == store.workspace_id)))
    skus = {sku for _, sku, _ in daily}
    dates = [day for _, _, day in daily]
    upload.mapping = {"columns": mapping, "dateFormat": date_format}
    upload.missing_days_are_zero = missing_days_are_zero
    upload.closed_days = [[str(s), str(d)] for s, d in sorted(closed)]
    upload.summary = {
        "rowsRead": rows_read, "rowsAccepted": accepted, "rowsRejected": len(check.rejected_rows),
        "rowsWithWarnings": len(check.warned_rows - check.rejected_rows),
        "rowsSummedAsDuplicates": aggregated, "dailyRecords": len(daily), "zeroFilledDays": zero_filled,
        "closedDays": len(closed),
        "dateRange": {"from": str(min(dates)), "to": str(max(dates))} if dates else None,
        "products": len(skus), "newProducts": len(skus - known),
        "stores": sorted({s.code for s in stores_by_code.values() if s.id in {k[0] for k in daily}}),
        "overlappingDays": overlap_count(db, upload.id, store.workspace_id),
        "errorsByCode": dict(check.errors_by_code), "warningsByCode": dict(check.warnings_by_code),
        "issuesTruncated": len(check.issues) >= MAX_STORED_ISSUES,
        "unusedColumns": upload.detected.get("unusedColumns", []),
    }
    if not daily:
        upload.status = ImportStatus.FAILED
    elif check.rejected_rows or check.warnings_by_code:
        upload.status = ImportStatus.NEEDS_ATTENTION
    else:
        upload.status = ImportStatus.READY
    upload.validated_at = _now()
    db.commit()


def _zero_fill(daily: dict, closed: set) -> int:
    """Each product's missing days, from its first day in the file to the store's last day in the file, become 0."""
    by_product: dict[tuple, list[dt.date]] = defaultdict(list)
    last_day: dict[uuid.UUID, dt.date] = {}
    for sid, sku, day in daily:
        by_product[(sid, sku)].append(day)
        last_day[sid] = max(last_day.get(sid, day), day)
    added = 0
    for (sid, sku), days in by_product.items():
        price = None
        day = min(days)
        while day <= last_day[sid]:
            entry = daily.get((sid, sku, day))
            if entry is not None:
                price = entry.price if entry.price is not None else price
            elif (sid, day) not in closed:
                daily[(sid, sku, day)] = _Daily(units=0.0, price=price, rows=0)
                added += 1
            day += dt.timedelta(days=1)
    return added


def _flag_spikes(db: Session, import_id: uuid.UUID, daily: dict, check: _Validation, mapping: dict) -> None:
    by_product: dict[tuple, list[tuple[dt.date, _Daily]]] = defaultdict(list)
    for (sid, sku, day), entry in daily.items():
        by_product[(sid, sku)].append((day, entry))
    firsts: dict[uuid.UUID, list[dt.date]] = defaultdict(list)
    for (sid, _), items in by_product.items():
        firsts[sid].append(min(d for d, _ in items))
    prior: dict[tuple, list[tuple[dt.date, float]]] = defaultdict(list)
    for sid, days in firsts.items():
        rows = db.execute(select(Product.sku, SalesDaily.sale_date, SalesDaily.units_sold)
                          .join(Product, Product.id == SalesDaily.product_id)
                          .where(SalesDaily.store_id == sid,
                                 SalesDaily.sale_date >= min(days) - dt.timedelta(days=SPIKE_WINDOW * 2),
                                 SalesDaily.sale_date < max(days)))
        for sku, day, units in rows:
            prior[(sid, sku)].append((day, float(units)))
    for key, items in by_product.items():
        items.sort(key=lambda item: item[0])
        first = items[0][0]
        window = [u for d, u in sorted(prior.get(key, [])) if d < first][-SPIKE_WINDOW:]
        for day, entry in items:
            if len(window) >= 7:
                median = statistics.median(window)
                if median > 0 and entry.units > SPIKE_FACTOR * median and entry.rows > 0:
                    check.add(import_id, "WARNING", "SPIKE",
                              f"{key[1]} sold {entry.units:g} on {day}, more than {SPIKE_FACTOR}× its usual "
                              f"{median:g}. Check it's correct; it will be kept as it is.",
                              entry.first_row, mapping.get("unitsSold"), entry.units)
            window.append(entry.units)
            window = window[-SPIKE_WINDOW:]


def overlap_count(db: Session, import_id: uuid.UUID, workspace_id: uuid.UUID) -> int:
    return db.scalar(text("""
        SELECT count(*) FROM import_rows r
        JOIN products p ON p.workspace_id = :ws AND p.sku = r.sku
        JOIN sales_daily s ON s.store_id = r.store_id AND s.product_id = p.id AND s.sale_date = r.sale_date
        WHERE r.import_id = :iid"""), {"ws": workspace_id, "iid": import_id})


# ---- confirm and delete -------------------------------------------------------------------

def confirm(db: Session, upload: SalesImport, workspace: Workspace, overlap_policy: str) -> list[uuid.UUID]:
    """Merge validated rows into sales_daily. Returns the IDs of the stores whose history changed."""
    params = {"iid": upload.id, "ws": workspace.id}
    store_ids = list(db.scalars(select(ImportRow.store_id).where(ImportRow.import_id == upload.id).distinct()))
    db.execute(select(Store).where(Store.id.in_(store_ids)).with_for_update())  # one merge per store at a time

    created = db.execute(text("""
        INSERT INTO products (id, workspace_id, sku, name, category, unit_of_measure, is_active, created_at)
        SELECT gen_random_uuid(), :ws, sku, coalesce(name, sku), category, coalesce(uom, 'units'), true, now()
        FROM (SELECT DISTINCT ON (sku) sku, product_name AS name, category, unit_of_measure AS uom
              FROM import_rows WHERE import_id = :iid ORDER BY sku, first_row NULLS LAST) AS firsts
        ON CONFLICT (workspace_id, sku) DO NOTHING"""), params).rowcount
    db.execute(text("""
        UPDATE products p SET category = r.category
        FROM (SELECT DISTINCT ON (sku) sku, category FROM import_rows
              WHERE import_id = :iid AND category IS NOT NULL ORDER BY sku, first_row) AS r
        WHERE p.workspace_id = :ws AND p.sku = r.sku AND p.category IS NULL"""), params)

    replaced = 0
    if overlap_policy == "REPLACE":
        replaced = db.execute(text("""
            DELETE FROM sales_daily s USING import_rows r, products p
            WHERE r.import_id = :iid AND p.workspace_id = :ws AND p.sku = r.sku
              AND s.store_id = r.store_id AND s.product_id = p.id AND s.sale_date = r.sale_date"""), params).rowcount
    inserted = db.execute(text("""
        INSERT INTO sales_daily (store_id, product_id, sale_date, units_sold, unit_price, import_id)
        SELECT r.store_id, p.id, r.sale_date, r.units_sold, r.unit_price, :iid
        FROM import_rows r JOIN products p ON p.workspace_id = :ws AND p.sku = r.sku
        WHERE r.import_id = :iid
        ON CONFLICT (store_id, product_id, sale_date) DO NOTHING"""), params).rowcount
    staged = db.scalar(select(func.count()).select_from(ImportRow).where(ImportRow.import_id == upload.id))

    db.execute(text("""
        UPDATE products p SET
            first_sale_date = LEAST(p.first_sale_date, a.first_sale),
            first_price_date = LEAST(p.first_price_date, a.first_price)
        FROM (SELECT p2.id, min(s.sale_date) FILTER (WHERE s.units_sold > 0) AS first_sale,
                     min(s.sale_date) FILTER (WHERE s.unit_price IS NOT NULL) AS first_price
              FROM sales_daily s JOIN products p2 ON p2.id = s.product_id
              WHERE s.import_id = :iid GROUP BY p2.id) AS a
        WHERE p.id = a.id"""), params)
    for store in db.scalars(select(Store).where(Store.id.in_(store_ids))):
        refresh_store_range(db, store)
        store.history_version += 1

    db.execute(delete(ImportRow).where(ImportRow.import_id == upload.id))
    upload.status, upload.imported_at, upload.content = ImportStatus.IMPORTED, _now(), None
    upload.result = {"overlapPolicy": overlap_policy, "recordsInserted": inserted, "recordsReplaced": replaced,
                     "overlapsSkipped": staged - inserted if overlap_policy == "SKIP" else 0,
                     "productsCreated": created}
    db.commit()
    return store_ids


def remove(db: Session, upload: SalesImport) -> list[uuid.UUID]:
    """Delete an import. If it was imported, its rows leave the history (days it replaced don't come back)."""
    store_ids: list[uuid.UUID] = []
    if upload.status == ImportStatus.IMPORTED:
        store_ids = list(db.scalars(select(SalesDaily.store_id).where(SalesDaily.import_id == upload.id).distinct()))
        deleted = db.execute(delete(SalesDaily).where(SalesDaily.import_id == upload.id)).rowcount
        for store in db.scalars(select(Store).where(Store.id.in_(store_ids)).with_for_update()):
            refresh_store_range(db, store)
            store.history_version += 1
        upload.result = {**(upload.result or {}), "recordsDeleted": deleted}
    db.execute(delete(ImportRow).where(ImportRow.import_id == upload.id))
    upload.status, upload.deleted_at, upload.content = ImportStatus.DELETED, _now(), None
    db.commit()
    return store_ids


def file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def refresh_demand_classes(db: Session, store: Store, product_ids: list[uuid.UUID] | None = None,
                           chunk: int = 200) -> int:
    """Recompute demand classes from the last 365 days (spec §11.6). Best effort: returns how many were updated."""
    from app.ml_client import MlServiceError, get_ml_client

    if store.history_end is None:
        return 0
    start = store.history_end - dt.timedelta(days=364)
    query = select(Product).where(Product.workspace_id == store.workspace_id, Product.is_active)
    if product_ids is not None:
        query = query.where(Product.id.in_(product_ids))
    products = list(db.scalars(query))
    updated = 0
    for i in range(0, len(products), chunk):
        part = {p.id: p for p in products[i:i + chunk]}
        history: dict[uuid.UUID, list[dict]] = defaultdict(list)
        for pid, day, units in db.execute(
                select(SalesDaily.product_id, SalesDaily.sale_date, SalesDaily.units_sold)
                .where(SalesDaily.store_id == store.id, SalesDaily.product_id.in_(list(part)),
                       SalesDaily.sale_date >= start, SalesDaily.sale_date <= store.history_end)
                .order_by(SalesDaily.sale_date)):
            history[pid].append({"date": day.isoformat(), "units": float(units)})
        series = [{"productId": part[pid].sku, "storeId": store.code, "history": rows}
                  for pid, rows in history.items()]
        if not series:
            continue
        try:
            results = get_ml_client().demand_class(series)
        except MlServiceError:
            return updated
        by_sku = {p.sku: p for p in part.values()}
        for result in results:
            product = by_sku.get(result["productId"])
            if product is None:
                continue
            product.demand_class, product.demand_label = result["class"], result.get("label")
            product.demand_adi, product.demand_cv2 = result.get("adi"), result.get("cv2")
            product.demand_zero_share, product.demand_updated_at = result.get("zeroShare"), _now()
            updated += 1
        db.commit()
    return updated
