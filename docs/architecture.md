# Backend Architecture

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Stack | Python 3.12 + FastAPI + SQLAlchemy 2 + PostgreSQL 16 | Same language as `mlengine`; OpenAPI generated from the code for the Android client |
| Sign-in | Our own: email + password, Argon2id hashes, 15-min HS256 JWT access tokens, 30-day opaque refresh tokens | No third-party dependency; refresh tokens can be revoked |
| Job queue | A `forecast_runs` table polled by a worker (`SELECT … FOR UPDATE SKIP LOCKED`) | No Redis or Celery to run; jobs survive restarts; workers scale out safely |
| Hosting | One Linux server, Docker Compose, Caddy for automatic HTTPS | Cheap and simple for a semester project; only Caddy is exposed |
| Repository | Separate from the ML engine | The backend and the ML engine are deployed and versioned on their own and talk only over HTTP |

## Modules

```text
app/
├── main.py            FastAPI app, request-ID middleware, routers, /healthz
├── config.py          Settings from environment variables (.env)
├── db.py              Engine and session
├── models.py          Tables (see below)
├── schemas.py         Public request/response models (camelCase)
├── security.py        Password hashing, JWTs, refresh tokens
├── deps.py            current_user, workspace/store access rules
├── errors.py          ApiError and the single error shape
├── ml_client.py       httpx client for the ML service (X-Internal-Token)
├── routers/           auth, workspaces (and stores), products (history, readiness), models_api, forecasts
├── services/
│   ├── history.py     history loading, known prices, readiness rules
│   ├── models_info.py ML model list (cached 60 s), the leakage rule, as-of ranges
│   └── forecasting.py request validation, job claiming, the ML call, storing results, retries, actuals
├── worker.py          python -m app.worker
├── seed.py            Load the Reference workspace from `mlengine export-reference`
└── cli.py             seed-reference, create-user
migrations/            Alembic
```

## Data model

| Table | Key columns | Notes |
| --- | --- | --- |
| `users` | email (unique, lower-case), password_hash, is_research | |
| `refresh_tokens` | token_hash (SHA-256), expires_at, revoked_at | Rotated on each refresh; reuse revokes all of the user's tokens |
| `workspaces` | is_reference | Exactly one Reference workspace (the M5 demo) |
| `memberships` | (workspace, user), role | OWNER, MANAGER, VIEWER |
| `stores` | code (sent to ML as `storeId`), country, timezone, model_id, history_start/end, history_version | `model_id` is `m5-ca1-foods` for the demo and `own-<workspaceId>` otherwise |
| `products` | sku (sent to ML as `productId`), category, first_price_date, demand_* | Demand class cached from the last forecast |
| `sales_daily` | PK (store, product, date), units_sold, unit_price, import_id | **A missing row means the day wasn't recorded, never zero** |
| `future_prices` | PK (store, product, date), unit_price | Known prices after the last day of history (demo) |
| `forecast_runs` | status, method, as_of_date, horizon_days, model_* metadata, run_metrics, product_backtest | The job and the forecast are one row (`jobId == forecastId`) |
| `forecast_points` | PK (run, date), ai, naive_last_value, seasonal_naive_7, actual | |

## Forecast job lifecycle

1. `POST /v1/forecasts` runs these checks, and returns a typed `422` before anything is queued if one fails:
   - **Role:** the user may forecast in this workspace.
   - **As-of date:**
     - business stores: always the last day of history;
     - demo: from `replayMin` (research users: `researchMin`) to the last day of history, and the horizon must stay inside the M5 calendar.
   - **Model** (AI and COMPARE only):
     - a version exists for this store;
     - the leakage rule holds: that version's training ended on or before the as-of date;
     - the horizon is in its `capabilities.horizons`.
   - **Readiness** (AI only): 21 days of history and at most 3 gaps. COMPARE still runs and falls back to baselines.
2. A `forecast_runs` row is created as `QUEUED`, and the response is `202`.
3. The worker claims the oldest queued row and moves it to `PREPARING_DATA`. It loads:
   - 365 days of history up to the as-of date;
   - known prices for the forecast days, from the day's `sales_daily.unit_price` in replays and from `future_prices` beyond history (only prices are read, never the future units);
   - the first price date.
4. `RUNNING_MODEL`: `POST /internal/v1/forecast` to the ML service. The ML service applies the leakage rule again.
5. `FINALIZING`:
   - store the points and the model metadata;
   - attach actual sales for the forecast days (replays);
   - compute the run's MAE;
   - fetch the stored per-product backtest (demo);
   - cache the product's demand class.
6. `COMPLETED`, or `FAILED` with a code:
   - **Outages** (`ML_SERVICE_UNAVAILABLE`) are retried up to 3 attempts, then fail as retryable.
   - **Request-level ML errors** fail straight away.
   - **Stuck runs** (in progress for more than 5 minutes, e.g. after a worker crash) are requeued by the hourly maintenance pass.
   - **Late actuals:** the same pass fills in actual sales that arrived after a forecast was made ("Actuals now available").

## Security

- Passwords: Argon2id. An unknown email is checked against a dummy hash, so response timing doesn't reveal which emails have accounts.
- Access tokens: 15 minutes, HS256, issuer checked, `type: access`.
- Refresh tokens: random 48 bytes; only the SHA-256 is stored; rotated on every use; reuse revokes every session for that user.
- Authorisation: every store, product and forecast lookup goes through `deps.store_access`. Resources the user can't see return `404`, not `403`, so their existence isn't revealed. Demo-workspace forecasts are private to the user who made them.
- The ML service is reachable only on the internal Docker network and requires `X-Internal-Token`.
- Still to add before a public launch: rate limiting on `/v1/auth/*` (at Caddy or with a middleware), email verification and password reset.

## Build plan

| Slice | Scope | Status |
| --- | --- | --- |
| 1 | Auth, workspaces/stores/products, demo seeding, history, readiness, capabilities, model card, forecast jobs + worker, history list, CSV export | **Done** (26 tests; checked end to end against the real ML service) |
| 2 | CSV imports: upload (`Idempotency-Key`), column mapping, the "no row means zero sales?" answer, validation report, confirm/merge with overlap policy, delete batch; demand-class refresh after import; `history_version` bump | Next |
| 3 | Own-data training: eligibility via `/internal/v1/eligibility`, `POST /v1/stores/{sid}/models/train` → ML `/train` job, polling, gate result on the model screen, weekly retrain | After 2 |
| 4 | Team invitations, research export, rate limiting, password reset | Later |
