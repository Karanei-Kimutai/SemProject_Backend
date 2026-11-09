"""Database tables.

Conventions: UUID primary keys; ``created_at`` in UTC; business dates are plain ``DATE`` values
interpreted in the store's timezone. A row in ``sales_daily`` means that day was *recorded*; a
missing row means "not recorded" and is never treated as zero sales.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    Boolean, Date, DateTime, Enum, Float, ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Role(str, enum.Enum):
    OWNER = "OWNER"
    MANAGER = "MANAGER"
    VIEWER = "VIEWER"


class ForecastMethod(str, enum.Enum):
    AI = "AI"
    BASELINE = "BASELINE"
    COMPARE = "COMPARE"


class RunStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    PREPARING_DATA = "PREPARING_DATA"
    RUNNING_MODEL = "RUNNING_MODEL"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _enum(cls):
    return Enum(cls, name=cls.__name__.lower(), native_enum=False, length=20)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # stored lower-cased
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(120))
    is_research: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # SHA-256 of the opaque token
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120))
    is_reference: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    stores: Mapped[list[Store]] = relationship(back_populates="workspace", order_by="Store.name")


class Membership(Base):
    __tablename__ = "memberships"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[Role] = mapped_column(_enum(Role))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Store(Base):
    __tablename__ = "stores"
    __table_args__ = (UniqueConstraint("workspace_id", "code"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(40))  # the storeId sent to the ML service
    name: Mapped[str] = mapped_column(String(120))
    country: Mapped[str] = mapped_column(String(2))
    subdivision: Mapped[str | None] = mapped_column(String(10))
    timezone: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3))
    model_id: Mapped[str | None] = mapped_column(String(80))  # ML model family serving this store
    history_version: Mapped[int] = mapped_column(Integer, default=0)
    history_start: Mapped[dt.date | None] = mapped_column(Date)
    history_end: Mapped[dt.date | None] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    workspace: Mapped[Workspace] = relationship(back_populates="stores")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("workspace_id", "sku"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(String(80))  # the productId sent to the ML service
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(120))
    unit_of_measure: Mapped[str] = mapped_column(String(40), default="units")
    default_price: Mapped[float | None] = mapped_column(Numeric(12, 4))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    first_price_date: Mapped[dt.date | None] = mapped_column(Date)
    first_sale_date: Mapped[dt.date | None] = mapped_column(Date)
    demand_class: Mapped[str | None] = mapped_column(String(20))
    demand_label: Mapped[str | None] = mapped_column(String(80))
    demand_adi: Mapped[float | None] = mapped_column(Float)
    demand_cv2: Mapped[float | None] = mapped_column(Float)
    demand_zero_share: Mapped[float | None] = mapped_column(Float)
    demand_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SalesDaily(Base):
    __tablename__ = "sales_daily"

    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), primary_key=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    sale_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    units_sold: Mapped[float] = mapped_column(Numeric(12, 3))
    unit_price: Mapped[float | None] = mapped_column(Numeric(12, 4))
    import_id: Mapped[uuid.UUID | None] = mapped_column(index=True)


class FuturePrice(Base):
    """Known prices for days after the last recorded sale (Reference workspace)."""

    __tablename__ = "future_prices"

    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), primary_key=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    price_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 4))


class ForecastBatch(Base):
    """Forecasts for many products of one store at once. Each product is an ordinary ForecastRun."""

    __tablename__ = "forecast_batches"
    __table_args__ = (Index("ix_forecast_batches_store", "store_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    method: Mapped[ForecastMethod] = mapped_column(_enum(ForecastMethod))
    as_of_date: Mapped[dt.date] = mapped_column(Date)
    horizon_days: Mapped[int] = mapped_column(Integer)
    history_version: Mapped[int] = mapped_column(Integer, default=0)
    product_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ForecastRun(Base):
    """A forecast request and, once completed, its result. The job ID and forecast ID are the same."""

    __tablename__ = "forecast_runs"
    __table_args__ = (
        Index("ix_forecast_runs_queue", "status", "created_at"),
        Index("ix_forecast_runs_store_product", "store_id", "product_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"))
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    # Set when the run is one product of a store-wide batch.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("forecast_batches.id", ondelete="CASCADE"),
                                                       index=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    method: Mapped[ForecastMethod] = mapped_column(_enum(ForecastMethod))
    as_of_date: Mapped[dt.date] = mapped_column(Date)
    horizon_days: Mapped[int] = mapped_column(Integer)
    requested_model_version: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus), default=RunStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)
    is_replay: Mapped[bool] = mapped_column(Boolean, default=False)
    history_version: Mapped[int] = mapped_column(Integer, default=0)
    # Filled from the ML service response
    model_id: Mapped[str | None] = mapped_column(String(80))
    model_version: Mapped[str | None] = mapped_column(String(40))
    feature_schema: Mapped[str | None] = mapped_column(String(40))
    model_training_end: Mapped[dt.date | None] = mapped_column(Date)
    mlengine_version: Mapped[str | None] = mapped_column(String(20))
    ml_request_id: Mapped[str | None] = mapped_column(String(80))
    horizon_validated: Mapped[bool | None] = mapped_column(Boolean)
    price_assumption: Mapped[str | None] = mapped_column(String(20))
    missing_days_in_lookback: Mapped[int | None] = mapped_column(Integer)
    ai_unavailable_reason: Mapped[str | None] = mapped_column(String(40))
    demand_class: Mapped[dict | None] = mapped_column(JSONB)
    warnings: Mapped[list | None] = mapped_column(JSONB)
    run_metrics: Mapped[dict | None] = mapped_column(JSONB)
    product_backtest: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    points: Mapped[list[ForecastPoint]] = relationship(
        order_by="ForecastPoint.point_date", cascade="all, delete-orphan"
    )


class ForecastPoint(Base):
    __tablename__ = "forecast_points"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("forecast_runs.id", ondelete="CASCADE"), primary_key=True)
    point_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    ai: Mapped[float | None] = mapped_column(Float)
    naive_last_value: Mapped[float | None] = mapped_column(Float)
    seasonal_naive_7: Mapped[float | None] = mapped_column(Float)
    actual: Mapped[float | None] = mapped_column(Float)


class ImportStatus(str, enum.Enum):
    UPLOADED = "UPLOADED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"  # validated; some rows rejected, the rest can still be imported
    READY = "READY"
    IMPORTED = "IMPORTED"
    FAILED = "FAILED"  # nothing importable (unreadable file, or every row rejected)
    DELETED = "DELETED"


class SalesImport(Base):
    """One uploaded CSV file (an import batch). Its rows merge into ``sales_daily`` when confirmed."""

    __tablename__ = "imports"
    __table_args__ = (UniqueConstraint("store_id", "uploaded_by", "idempotency_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes | None] = mapped_column(LargeBinary)  # dropped once imported or deleted
    status: Mapped[ImportStatus] = mapped_column(_enum(ImportStatus), default=ImportStatus.UPLOADED)
    columns: Mapped[list] = mapped_column(JSONB, default=list)
    detected: Mapped[dict] = mapped_column(JSONB, default=dict)  # delimiter, suggested mapping, date formats
    mapping: Mapped[dict | None] = mapped_column(JSONB)
    missing_days_are_zero: Mapped[bool | None] = mapped_column(Boolean)
    summary: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    closed_days: Mapped[list | None] = mapped_column(JSONB)  # [[store_id, date], …]
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    validated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ImportRow(Base):
    """A validated, normalised row: one product, store and day (duplicates already summed)."""

    __tablename__ = "import_rows"

    import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"), primary_key=True)
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), primary_key=True)
    sku: Mapped[str] = mapped_column(String(80), primary_key=True)
    sale_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    units_sold: Mapped[float] = mapped_column(Numeric(12, 3))
    unit_price: Mapped[float | None] = mapped_column(Numeric(12, 4))
    product_name: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(120))
    unit_of_measure: Mapped[str | None] = mapped_column(String(40))
    first_row: Mapped[int | None] = mapped_column(Integer)  # CSV line number; None for zero-filled days
    source_rows: Mapped[int] = mapped_column(Integer, default=1)
    zero_filled: Mapped[bool] = mapped_column(Boolean, default=False)


class ImportIssue(Base):
    __tablename__ = "import_issues"
    __table_args__ = (Index("ix_import_issues_import", "import_id", "severity", "row_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"))
    severity: Mapped[str] = mapped_column(String(10))  # ERROR (row rejected) or WARNING (kept, flagged)
    code: Mapped[str] = mapped_column(String(40))
    row_number: Mapped[int | None] = mapped_column(Integer)
    column: Mapped[str | None] = mapped_column(String(120))
    value: Mapped[str | None] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)


class TrainingStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    TRAINING = "TRAINING"  # submitted to the ML service; the worker polls it
    COMPLETED = "COMPLETED"  # see outcome
    FAILED = "FAILED"


class TrainingRun(Base):
    """One own-data training job for a workspace (spec §9.4). The model is shared by the workspace's stores."""

    __tablename__ = "training_runs"
    __table_args__ = (Index("ix_training_runs_status", "status", "created_at"),
                      Index("ix_training_runs_workspace", "workspace_id", "created_at"))

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    store_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    trigger: Mapped[str] = mapped_column(String(20), default="MANUAL")  # MANUAL or SCHEDULED
    status: Mapped[TrainingStatus] = mapped_column(_enum(TrainingStatus), default=TrainingStatus.QUEUED)
    outcome: Mapped[str | None] = mapped_column(String(20))  # TRAINED, GATE_FAILED, NOT_ELIGIBLE
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    ml_job_id: Mapped[str | None] = mapped_column(String(80))
    country: Mapped[str | None] = mapped_column(String(2))
    subdivision: Mapped[str | None] = mapped_column(String(10))
    history_versions: Mapped[dict | None] = mapped_column(JSONB)  # {store_id: history_version} sent
    history_rows: Mapped[int | None] = mapped_column(Integer)
    model_id: Mapped[str | None] = mapped_column(String(80))
    model_version: Mapped[str | None] = mapped_column(String(40))
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    polled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Invitation(Base):
    """An invitation to join a workspace. Accepted by a signed-in user whose email matches."""

    __tablename__ = "invitations"
    __table_args__ = (Index("ix_invitations_email", "email", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))  # lower-cased
    role: Mapped[Role] = mapped_column(_enum(Role))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING, ACCEPTED, DECLINED, REVOKED
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    responded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordReset(Base):
    """A one-time reset code (only its hash is stored). Limited lifetime and attempts."""

    __tablename__ = "password_resets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
