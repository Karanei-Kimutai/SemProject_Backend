"""Public API request/response models (app ↔ backend). Field names are camelCase on the wire."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field
from pydantic.alias_generators import to_camel


class Api(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


# ---- auth ---------------------------------------------------------------------------------

class RegisterIn(Api):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    display_name: str = Field(min_length=1, max_length=120)


class LoginIn(Api):
    email: EmailStr
    password: str = Field(max_length=200)


class RefreshIn(Api):
    refresh_token: str = Field(max_length=200)


class PasswordResetRequestIn(Api):
    email: EmailStr


class PasswordResetConfirmIn(Api):
    email: EmailStr
    code: str = Field(pattern=r"^\d{8}$")
    new_password: str = Field(min_length=10, max_length=200)


class ChangePasswordIn(Api):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=10, max_length=200)


class TokenOut(Api):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int


class WorkspaceAccessOut(Api):
    workspace_id: uuid.UUID
    name: str
    role: str
    is_reference: bool


class MeOut(Api):
    id: uuid.UUID
    email: str
    display_name: str
    is_research: bool
    workspaces: list[WorkspaceAccessOut]


# ---- workspaces, stores, products ---------------------------------------------------------

class WorkspaceIn(Api):
    name: str = Field(min_length=1, max_length=120)


class WorkspaceOut(Api):
    id: uuid.UUID
    name: str
    is_reference: bool
    role: str


class StoreIn(Api):
    code: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=120)
    country: str = Field(min_length=2, max_length=2)
    subdivision: str | None = Field(default=None, max_length=10)
    timezone: str = Field(min_length=1, max_length=64)
    currency: str = Field(min_length=3, max_length=3)


class StoreOut(Api):
    id: uuid.UUID
    workspace_id: uuid.UUID
    code: str
    name: str
    country: str
    subdivision: str | None
    timezone: str
    currency: str
    is_reference: bool
    history_start: dt.date | None
    history_end: dt.date | None


class DemandClassOut(Api):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    class_: str | None = Field(default=None, alias="class")
    label: str | None = None
    adi: float | None = None
    cv2: float | None = None
    zero_share: float | None = None


class ProductIn(Api):
    sku: str = Field(min_length=1, max_length=80, pattern=r"^\S(.*\S)?$")
    name: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=120)
    unit_of_measure: str = Field(default="units", max_length=40)
    default_price: float | None = Field(default=None, ge=0)


class ProductPatch(Api):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=120)
    unit_of_measure: str | None = Field(default=None, max_length=40)
    default_price: float | None = Field(default=None, ge=0)
    is_active: bool | None = None


class ProductOut(Api):
    id: uuid.UUID
    sku: str
    name: str
    category: str | None
    unit_of_measure: str
    default_price: float | None
    is_active: bool
    first_sale_date: dt.date | None
    demand_class: DemandClassOut | None


class Page(Api):
    page: int
    page_size: int
    total: int


class ProductPage(Page):
    items: list[ProductOut]


class HistoryPoint(Api):
    date: dt.date
    units: float | None  # null = not recorded
    price: float | None


class HistoryOut(Api):
    product_id: uuid.UUID
    from_: dt.date = Field(alias="from")
    to: dt.date
    points: list[HistoryPoint]


# ---- readiness and capabilities -----------------------------------------------------------

class ProductReadiness(Api):
    product_id: uuid.UUID
    sku: str
    ready: bool
    reasons: list[str]
    recorded_days_in_lookback: int
    missing_days_in_lookback: int
    latest_date: dt.date | None


class ReadinessOut(Api):
    store_id: uuid.UUID
    as_of_date: dt.date | None
    min_history_days: int
    max_missing_days_in_lookback: int
    stale: bool
    products_ready: int
    products_total: int
    products: list[ProductReadiness]


class AsOfRange(Api):
    default: dt.date | None
    editable: bool
    replay_min: dt.date | None = None
    replay_max: dt.date | None = None
    research_min: dt.date | None = None
    calendar_end: dt.date | None = None


class ModelRef(Api):
    id: str
    version: str


class CapabilitiesOut(Api):
    store_id: uuid.UUID
    is_reference: bool
    ai_available: bool
    ai_unavailable_reason: str | None
    methods: list[str]
    horizons: list[int]
    frequency: str
    as_of: AsOfRange
    min_history_days: int
    max_missing_days_in_lookback: int
    prediction_intervals: bool
    baselines: list[dict]
    active_model: ModelRef | None
    training: dict | None = None  # own-data workspaces: the latest training run


# ---- forecasts ----------------------------------------------------------------------------

class ForecastIn(Api):
    store_id: uuid.UUID
    product_id: uuid.UUID
    as_of_date: dt.date | None = None
    horizon_days: int = Field(ge=1, le=28)
    method: Literal["AI", "BASELINE", "COMPARE"] = "COMPARE"
    model_version: str | None = Field(default=None, max_length=40)  # research users only


class JobOut(Api):
    job_id: uuid.UUID
    forecast_id: uuid.UUID
    status: str
    created_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    error: dict | None
    retryable: bool


class ForecastPointOut(Api):
    date: dt.date
    ai: float | None
    baseline: float | None
    seasonal_naive: float | None
    actual: float | None


class ForecastSummary(Api):
    forecast_id: uuid.UUID
    status: str
    store_id: uuid.UUID
    product_id: uuid.UUID
    sku: str
    product_name: str
    as_of_date: dt.date
    horizon_days: int
    method: str
    is_replay: bool
    model_version: str | None
    created_at: dt.datetime
    actuals_available: bool


class ForecastPage(Page):
    items: list[ForecastSummary]


# ---- store-wide forecast batches ------------------------------------------------------------

class BatchIn(Api):
    horizon_days: int = Field(ge=1, le=28)
    # Leave out to forecast every active product with sales history in the store.
    product_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=2000)


class BatchOut(Api):
    batch_id: uuid.UUID
    store_id: uuid.UUID
    status: Literal["RUNNING", "COMPLETED"]
    method: str
    as_of_date: dt.date
    horizon_days: int
    total: int
    completed: int
    failed: int
    pending: int
    created_at: dt.datetime
    finished_at: dt.datetime | None
    # True once the store's sales history changed after this batch (an import or a deleted import).
    outdated: bool


class BatchItem(Api):
    forecast_id: uuid.UUID
    product_id: uuid.UUID
    sku: str
    product_name: str
    unit_of_measure: str
    category: str | None
    demand_label: str | None
    status: str
    error: dict | None
    ai_unavailable_reason: str | None
    points: list[ForecastPointOut]


class BatchItemPage(Page):
    items: list[BatchItem]


# ---- imports ------------------------------------------------------------------------------

class ImportMappingIn(Api):
    columns: dict[Literal["salesDate", "productId", "productName", "unitsSold", "storeId", "unitPrice", "category",
                          "unitOfMeasure", "storeClosed"], str]
    date_format: str | None = None
    missing_days_are_zero: bool  # the wizard's required question: "no row means zero sales?"


class ImportConfirmIn(Api):
    overlap_policy: Literal["SKIP", "REPLACE"] = "SKIP"


class ImportOut(Api):
    id: uuid.UUID
    store_id: uuid.UUID
    filename: str
    size_bytes: int
    status: str
    created_at: dt.datetime
    validated_at: dt.datetime | None
    imported_at: dt.datetime | None
    columns: list[str]
    detected: dict
    mapping: dict | None
    missing_days_are_zero: bool | None
    summary: dict | None
    result: dict | None


class ImportPage(Page):
    items: list[ImportOut]


class ImportIssueOut(Api):
    severity: str
    code: str
    row_number: int | None
    column: str | None
    value: str | None
    message: str


class ImportIssuePage(Page):
    items: list[ImportIssueOut]


class HistorySummaryOut(Api):
    store_id: uuid.UUID
    history_start: dt.date | None
    history_end: dt.date | None
    products_with_data: int
    days_with_data: int
    records: int
    last_import_at: dt.datetime | None


# ---- own-data training --------------------------------------------------------------------

class TrainingRunOut(Api):
    id: uuid.UUID
    workspace_id: uuid.UUID
    status: str
    outcome: str | None
    trigger: str
    created_at: dt.datetime
    submitted_at: dt.datetime | None
    finished_at: dt.datetime | None
    history_rows: int | None
    model: ModelRef | None
    horizons: list[int] | None
    eligibility: dict | None
    backtest: dict | None
    gate: dict | None
    horizon_accuracy: dict | None
    error: dict | None
    retryable: bool


class TrainingRunPage(Page):
    items: list[TrainingRunOut]


class EligibilityOut(Api):
    workspace_id: uuid.UUID
    eligible: bool
    checks: dict
    rules: dict | None
    stores_in_model: list[str]
    latest_run: TrainingRunOut | None


# ---- team ---------------------------------------------------------------------------------

class MemberOut(Api):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: str
    joined_at: dt.datetime


class MemberPatch(Api):
    role: Literal["OWNER", "MANAGER", "VIEWER"]


class InvitationIn(Api):
    email: EmailStr
    role: Literal["OWNER", "MANAGER", "VIEWER"] = "VIEWER"


class InvitationOut(Api):
    id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str
    email: str
    role: str
    status: str
    invited_by: str | None
    created_at: dt.datetime
    expires_at: dt.datetime
