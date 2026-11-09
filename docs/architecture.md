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
├── routers/           auth (incl. password reset), workspaces (and stores), team, products (history, readiness),
│                      models_api, forecasts, batches, imports, training, research
├── services/
│   ├── history.py     history loading, known prices, readiness rules
│   ├── models_info.py ML model list (cached 60 s), the leakage rule, as-of ranges
│   ├── forecasting.py request validation, job claiming, the ML call, storing results, retries, actuals
│   ├── batches.py     store-wide forecasts: choosing products, one run per product, progress, retry
│   ├── imports.py     CSV reading, mapping suggestions, validation, zero-fill, spikes, merge, delete, demand classes
│   └── training.py    own-data eligibility (cached), training runs, ML job submission and polling, weekly retrain
├── email.py           console / smtp / memory email backends
├── ratelimit.py       fixed-window limits for the auth endpoints
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
| `forecast_runs` | status, method, as_of_date, horizon_days, model_* metadata, run_metrics, product_backtest, batch_id | The job and the forecast are one row (`jobId == forecastId`). `batch_id` is set for runs created by a store-wide batch |
| `forecast_batches` | store, requested_by, method, as_of_date, horizon_days, history_version, product_count | Groups one run per product; progress is counted from the runs' states |
| `forecast_points` | PK (run, date), ai, naive_last_value, seasonal_naive_7, actual | |
| `imports` | store, status, file content (until imported), columns, detected, mapping, missing_days_are_zero, summary, result | One CSV upload; `(store, user, idempotency_key)` is unique |
| `import_rows` | PK (import, store, sku, date), units, price, source_rows, zero_filled | Validated, normalised staging rows; removed after confirm |
| `import_issues` | import, severity, code, row_number, column, message | Up to 5,000 per import |
| `invitations` | workspace, email, role, status, expires_at | Accepted in-app by the matching signed-in user |
| `password_resets` | user, code_hash, attempts, expires_at, used_at | Single-use reset codes |
| `training_runs` | workspace, status, outcome, trigger, ml_job_id, history_versions, model_version, result | One own-data training job; at most one active per workspace |

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

## Store-wide forecasts (batches)

1. `POST /v1/stores/{sid}/forecast-batches` runs the same checks as a single forecast, once for the store, then picks every active product with sales history (or the `productIds` given; at most 2,000).
2. The method follows the store: `COMPARE` when it has a registered model, otherwise `BASELINE`. The as-of date is the last day of history.
3. One `forecast_batches` row and one ordinary `QUEUED` run per product are created in one transaction. If the same user already has an unfinished batch for this store and length, that batch is returned instead.
4. The worker claims single forecasts before batch runs, so someone waiting on one product isn't stuck behind a whole store. Batch runs skip the demo store's per-product backtest (opening the product fetches it).
5. Progress, items and retry are read from the runs. Each run is an ordinary forecast, so retries, stuck-run recovery and the per-forecast endpoints work unchanged. `history_version` lets the app say "new sales were added after this forecast".

## Imports

1. **Upload.** Store the file (bytea, ≤ 10 MB), detect the delimiter, suggest a mapping from column-name synonyms, and detect the date format from the sampled values. The format that parses the most values wins, and a tie means the user must choose.
2. **Mapping.** Parse every row into `import_rows`, one per (store, sku, day), summing duplicates. Apply the zero-vs-missing answer (closed days excluded) and flag spikes against the product's previous 28 recorded values (including history). Write `import_issues` and the summary. This can be repeated until the import is confirmed.
3. **Confirm.** One transaction, holding the affected store rows locked: insert new products; `REPLACE` deletes the overlapping days; insert with `ON CONFLICT DO NOTHING` (that is how `SKIP` works); update first sale and price dates and the store range; bump `history_version`. Then attach late actuals and refresh demand classes through `/internal/v1/demand-class`. That last step is best effort: if the ML service is down, the import still succeeds.
4. **Delete** removes `sales_daily` rows with that `import_id`.

## Own-data training

1. **Request.** `POST …/models/train` checks eligibility (ML `/eligibility`, cached by the stores' `history_version`). It then locks the workspace row and creates a `QUEUED` run, unless one is already active.
2. **Submit.** The worker (forecasts first) sends the workspace's history to ML `/train`: all stores, the last 730 days, recorded days only, so gaps stay gaps. The run becomes `TRAINING` with the ML job ID, and the history versions sent are recorded.
3. **Poll.** About every 5 s the worker checks ML `/train/{jobId}`:
   - `COMPLETED` → the run is `COMPLETED`, with outcome `TRAINED`, `GATE_FAILED` or `NOT_ELIGIBLE`, and the model cache is cleared.
   - `JOB_NOT_FOUND` (the ML service restarted; its jobs live in memory) → resubmit, up to 3 attempts.
   - More than 30 minutes → `TRAINING_TIMEOUT`.
4. **Serve.** Stores already point at `own-<workspaceId>`, so once the ML registry has a version, capabilities and forecasts use it. The leakage rule still applies: a model trained to day L serves as-of dates ≥ L.
5. **Retrain.** Hourly maintenance queues a `SCHEDULED` run for workspaces whose latest run is more than 7 days old, that have a trained model, and whose history changed since.

## Security

- Passwords: Argon2id. An unknown email is checked against a dummy hash, so response timing doesn't reveal which emails have accounts.
- Access tokens: 15 minutes, HS256, issuer checked, `type: access`.
- Refresh tokens: random 48 bytes; only the SHA-256 is stored; rotated on every use; reuse revokes every session for that user.
- Authorisation: every store, product and forecast lookup goes through `deps.store_access`. Resources the user can't see return `404`, not `403`, so their existence isn't revealed. Demo-workspace forecasts are private to the user who made them.
- The ML service is reachable only on the internal Docker network and requires `X-Internal-Token`.
- Rate limits on the sign-in endpoints (`app/ratelimit.py`) are in memory, which is right for the single API process in Docker Compose. With several API processes, move the counters to Postgres or Redis.
- Password reset: 8-digit single-use codes, stored as SHA-256 (with the user ID), valid for 30 minutes, at most 5 attempts. Requesting one never reveals whether the account exists. A reset or password change revokes every refresh token.
- Invitations are accepted inside the app by the signed-in user whose email matches, so there are no bearer links to leak.
- Email (`app/email.py`) is best effort, so a mail outage never fails a request. The `console` backend logs messages (development), and `smtp` sends them.
- Not done: email verification on sign-up. Anyone can register any address, but being invited still requires signing in with that exact address.

## Build plan

| Slice | Scope | Status |
| --- | --- | --- |
| 1 | Auth, workspaces/stores/products, demo seeding, history, readiness, capabilities, model card, forecast jobs + worker, history list, CSV export | **Done** (26 tests; checked end to end against the real ML service) |
| 2 | CSV imports: upload (`Idempotency-Key`), column mapping with suggestions, the "no row means zero sales?" answer, validation report, confirm/merge with overlap policy, delete batch; demand-class refresh and late actuals after import; `history_version` bump | **Done** (40 tests in total; checked end to end in Docker against the real ML service) |
| 3 | Own-data training: eligibility via `/internal/v1/eligibility`, `POST /v1/stores/{sid}/models/train` → ML `/train` job, polling, gate result on the model screen, weekly retrain | **Done** (47 tests in total; checked end to end in Docker: the sample shop trains in 23 s and passes the gate) |
| 4 | Team invitations and member management, research summary and export, rate limiting, password reset and change, email (console/SMTP) | **Done** (56 tests in total) |
| 5 | Store-wide forecasts for the business version of the app: `forecast_batches` (migration `0005`), create, progress, items, retry, latest; single forecasts claimed first; `GET /v1/forecasts?batch=` | **Done** (67 tests in total; approved by the project owner and merged into `main`) |
