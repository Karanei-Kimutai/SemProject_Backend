"""CSV sales imports (spec §8.6, §12.1)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Header, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import current_user, require, store_access
from app.errors import ApiError, not_found
from app.models import ImportIssue, ImportRow, ImportStatus, SalesDaily, SalesImport, Store, User
from app.schemas import (
    HistorySummaryOut, ImportConfirmIn, ImportIssueOut, ImportIssuePage, ImportMappingIn, ImportOut, ImportPage,
)
from app.services import forecasting, imports

router = APIRouter(prefix="/v1", tags=["imports"])
EDITABLE = (ImportStatus.UPLOADED, ImportStatus.NEEDS_ATTENTION, ImportStatus.READY, ImportStatus.FAILED)


def import_out(upload: SalesImport) -> ImportOut:
    return ImportOut(id=upload.id, store_id=upload.store_id, filename=upload.filename, size_bytes=upload.size_bytes,
                     status=upload.status.value, created_at=upload.created_at, validated_at=upload.validated_at,
                     imported_at=upload.imported_at, columns=upload.columns, detected=upload.detected or {},
                     mapping=upload.mapping, missing_days_are_zero=upload.missing_days_are_zero,
                     summary=upload.summary, result=upload.result)


def _editable_store(db: Session, user: User, store_id: uuid.UUID) -> Store:
    store, access = store_access(db, user, store_id)
    require(not access.is_reference, "The demo store is read-only.")
    require(access.can_edit, "Only owners and managers can import sales.")
    return store


def _import_for_user(db: Session, user: User, import_id: uuid.UUID, edit: bool = False) -> tuple[SalesImport, Store]:
    upload = db.get(SalesImport, import_id)
    if upload is None or upload.status == ImportStatus.DELETED:
        raise not_found("Import")
    try:
        store, access = store_access(db, user, upload.store_id)
    except ApiError:
        raise not_found("Import") from None
    if edit:
        require(access.can_edit, "Only owners and managers can change imports.")
    return upload, store


@router.post("/stores/{store_id}/imports", response_model=ImportOut, status_code=201, response_model_by_alias=True)
async def upload_import(store_id: uuid.UUID, file: UploadFile = File(...),
                        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100),
                        user: User = Depends(current_user), db: Session = Depends(get_db)) -> ImportOut:
    store = _editable_store(db, user, store_id)
    limit = get_settings().import_max_bytes
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise ApiError("FILE_TOO_LARGE", f"Files can be up to {limit // (1024 * 1024)} MB.", 413)
    digest = imports.file_hash(content)
    if idempotency_key:
        existing = db.scalar(select(SalesImport).where(SalesImport.store_id == store.id,
                                                      SalesImport.uploaded_by == user.id,
                                                      SalesImport.idempotency_key == idempotency_key))
        if existing is not None:
            if existing.sha256 != digest:
                raise ApiError("IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was used for a different file.", 409)
            return import_out(existing)
    detected = imports.inspect_upload(content)
    columns = detected.pop("columns")
    upload = SalesImport(store_id=store.id, uploaded_by=user.id, idempotency_key=idempotency_key,
                         filename=(file.filename or "upload.csv")[:255], size_bytes=len(content), sha256=digest,
                         content=content, columns=columns, detected=detected, status=ImportStatus.UPLOADED)
    db.add(upload)
    db.commit()
    return import_out(upload)


@router.get("/stores/{store_id}/imports", response_model=ImportPage, response_model_by_alias=True)
def list_imports(store_id: uuid.UUID, status: str | None = None, page: int = Query(1, ge=1),
                 page_size: int = Query(20, ge=1, le=100, alias="pageSize"),
                 user: User = Depends(current_user), db: Session = Depends(get_db)) -> ImportPage:
    store, _ = store_access(db, user, store_id)
    query = select(SalesImport).where(SalesImport.store_id == store.id, SalesImport.status != ImportStatus.DELETED)
    if status:
        try:
            query = query.where(SalesImport.status == ImportStatus(status.upper()))
        except ValueError:
            raise ApiError("INVALID_REQUEST", "Unknown import status.") from None
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(SalesImport.created_at.desc()).offset((page - 1) * page_size).limit(page_size))
    return ImportPage(page=page, page_size=page_size, total=total, items=[import_out(i) for i in items])


@router.get("/imports/{import_id}", response_model=ImportOut, response_model_by_alias=True)
def get_import(import_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> ImportOut:
    upload, _ = _import_for_user(db, user, import_id)
    return import_out(upload)


@router.put("/imports/{import_id}/mapping", response_model=ImportOut, response_model_by_alias=True)
def set_mapping(import_id: uuid.UUID, body: ImportMappingIn, user: User = Depends(current_user),
                db: Session = Depends(get_db)) -> ImportOut:
    upload, store = _import_for_user(db, user, import_id, edit=True)
    if upload.status not in EDITABLE or upload.content is None:
        raise ApiError("IMPORT_LOCKED", "This import was already imported. Upload the file again to change it.", 409)
    imports.validate(db, upload, store, body.columns, body.date_format, body.missing_days_are_zero)
    return import_out(upload)


@router.get("/imports/{import_id}/validation")
def validation(import_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Summary plus the first normalised daily records, for the preview step."""
    upload, store = _import_for_user(db, user, import_id)
    if upload.summary is None:
        raise ApiError("NOT_VALIDATED", "Map the columns first.", 409)
    if upload.status in (ImportStatus.READY, ImportStatus.NEEDS_ATTENTION):
        upload.summary = {**upload.summary,
                          "overlappingDays": imports.overlap_count(db, upload.id, store.workspace_id)}
        db.commit()
    codes = {s.id: s.code for s in db.scalars(select(Store).where(Store.workspace_id == store.workspace_id))}
    rows = db.scalars(select(ImportRow).where(ImportRow.import_id == upload.id)
                      .order_by(ImportRow.zero_filled, ImportRow.first_row).limit(20))
    return {"importId": str(upload.id), "status": upload.status.value, "summary": upload.summary,
            "preview": [{"storeId": codes.get(r.store_id), "productId": r.sku, "productName": r.product_name,
                         "date": r.sale_date.isoformat(), "unitsSold": float(r.units_sold),
                         "unitPrice": float(r.unit_price) if r.unit_price is not None else None,
                         "category": r.category, "sourceRows": r.source_rows, "zeroFilled": r.zero_filled}
                        for r in rows]}


@router.get("/imports/{import_id}/errors", response_model=ImportIssuePage, response_model_by_alias=True)
def import_errors(import_id: uuid.UUID, severity: str | None = Query(None, pattern="^(ERROR|WARNING)$"),
                  page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500, alias="pageSize"),
                  user: User = Depends(current_user), db: Session = Depends(get_db)) -> ImportIssuePage:
    upload, _ = _import_for_user(db, user, import_id)
    query = select(ImportIssue).where(ImportIssue.import_id == upload.id)
    if severity:
        query = query.where(ImportIssue.severity == severity)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(ImportIssue.row_number.nulls_last(), ImportIssue.id)
                       .offset((page - 1) * page_size).limit(page_size))
    return ImportIssuePage(page=page, page_size=page_size, total=total,
                           items=[ImportIssueOut.model_validate(i) for i in items])


def _after_history_change(db: Session, store_ids: list[uuid.UUID]) -> dict:
    refreshed = 0
    for sid in store_ids:
        forecasting.attach_new_actuals(db, sid)
        refreshed += imports.refresh_demand_classes(db, db.get(Store, sid))
    return {"demandClassesUpdated": refreshed}


@router.post("/imports/{import_id}/confirm", response_model=ImportOut, response_model_by_alias=True)
def confirm_import(import_id: uuid.UUID, body: ImportConfirmIn, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> ImportOut:
    upload, store = _import_for_user(db, user, import_id, edit=True)
    if upload.status not in (ImportStatus.READY, ImportStatus.NEEDS_ATTENTION):
        raise ApiError("IMPORT_NOT_READY", "Map and validate the file before importing it.", 409,
                       details={"status": upload.status.value})
    store_ids = imports.confirm(db, upload, store.workspace, body.overlap_policy)
    upload.result = {**upload.result, **_after_history_change(db, store_ids)}
    db.commit()
    return import_out(upload)


@router.delete("/imports/{import_id}", response_model=ImportOut, response_model_by_alias=True)
def delete_import(import_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> ImportOut:
    upload, _ = _import_for_user(db, user, import_id, edit=True)
    store_ids = imports.remove(db, upload)
    if store_ids:
        imports.refresh_demand_classes(db, db.get(Store, store_ids[0]))
    return import_out(upload)


@router.get("/stores/{store_id}/history/summary", response_model=HistorySummaryOut, response_model_by_alias=True)
def history_summary(store_id: uuid.UUID, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> HistorySummaryOut:
    store, _ = store_access(db, user, store_id)
    products, days, records = db.execute(
        select(func.count(SalesDaily.product_id.distinct()), func.count(SalesDaily.sale_date.distinct()), func.count())
        .where(SalesDaily.store_id == store.id)).one()
    last_import = db.scalar(select(func.max(SalesImport.imported_at)).where(SalesImport.store_id == store.id,
                                                                            SalesImport.status == ImportStatus.IMPORTED))
    return HistorySummaryOut(store_id=store.id, history_start=store.history_start, history_end=store.history_end,
                             products_with_data=products, days_with_data=days, records=records,
                             last_import_at=last_import)

