# How a forecasting backend works, and how this one does it

This document explains, in plain language, what the backend of a demand forecasting product usually does, and then how this backend does each part. It is written for someone new to backend development or machine learning who needs to understand, explain or extend the project. The precise API contract is in [`api.md`](api.md); the design reference is [`architecture.md`](architecture.md).

The companion document for the whole project, including how the model itself is trained and evaluated, is `docs/HOW_FORECASTING_WORKS.md` in the Android app repository.

Contents:

1. [The short version](#1-the-short-version)
2. [What a forecasting backend usually does](#2-what-a-forecasting-backend-usually-does)
3. [How this backend does it](#3-how-this-backend-does-it)
4. [Walkthrough: uploading sales](#4-walkthrough-uploading-sales)
5. [Walkthrough: forecasting every product](#5-walkthrough-forecasting-every-product)
6. [Walkthrough: training a shop's own model](#6-walkthrough-training-a-shops-own-model)
7. [Where this backend differs from larger systems](#7-where-this-backend-differs-from-larger-systems)
8. [Where to find things in the code](#8-where-to-find-things-in-the-code)
9. [Glossary](#9-glossary)

---

## 1. The short version

- **The app talks only to this backend.** The backend owns everything a business stores: accounts, shops, products and daily sales. It is the only part that talks to the machine learning (ML) service.
- **The ML service only does maths.** It holds the trained models and turns "here are this product's last weeks of sales" into "here is the forecast". It keeps no business data and is not reachable from the internet.
- **Forecasts are jobs, not instant answers.** A forecast request is saved as a job and answered with "accepted" straight away. A separate worker process does the work, and the app checks back until it's done. A whole shop can be forecast in one request (a batch).
- **Sales arrive as CSV uploads.** Each upload goes through checks (column matching, date formats, bad rows, overlaps with earlier uploads) before anything is saved.
- **Each shop can train its own model.** Once it has enough history, the backend sends that history to the ML service to train a model. The model is used only if it beats the simple forecast, and it is retrained weekly when new sales arrive.
- **Every number the app shows comes from here.** The app does no forecasting maths.

```text
Android app ──HTTPS──▶ Caddy ──▶ API (FastAPI) ──┬──▶ PostgreSQL
                                   ▲            │
                       worker ─────┘            └──▶ ML service (mlengine, private network only)
```

---

## 2. What a forecasting backend usually does

### 2.1 Keep the API and the model apart

Most ML products split into two services:

- **The application backend:** accounts, permissions, data storage, business rules, the public API.
- **The model service:** loads trained models and makes predictions. It is often written by different people, uses heavier libraries and needs different hardware.

They talk over a small internal API. This lets each be changed, tested and deployed on its own, and keeps the model service off the public internet.

### 2.2 Accounts, sessions and permissions

- **Passwords** are never stored as text. They are hashed with a slow, salted algorithm (Argon2 or bcrypt), so a leaked database doesn't reveal them.
- **Sessions** commonly use two tokens:
  - a short-lived **access token** (minutes) sent with every request;
  - a long-lived **refresh token** (weeks) used only to get new access tokens, and revocable.
- **Multi-tenancy:** many businesses share one system, so every request must be checked against "may this user see this shop?". Data is grouped by tenant (here, a *workspace*), and users have **roles** (owner, manager, viewer).

### 2.3 Getting the data in

Forecasts are only as good as the sales data. Ingestion usually means:

- **Accepting files** in the formats businesses already have (CSV exports from a till or spreadsheet).
- **Mapping columns** ("Qty", "Units", "Sold" all mean units sold) and **parsing dates** (is 01/02/2026 the 1st of February or the 2nd of January?).
- **Validating** every row and reporting problems by line number instead of failing the whole file.
- **Staging:** holding the cleaned rows separately until the user confirms, so a bad upload never touches real data.
- **Handling overlaps:** if a new file covers days already stored, ask whether to keep or replace them.
- **Idempotency:** if the phone retries an upload after a dropped connection, the server recognises the repeat instead of storing the file twice.

One rule matters a lot for forecasting: **"no record" is not the same as "zero sales"**. A missing day might be a closed shop, a broken till or a day nobody exported. Treating it as zero teaches the model that the product didn't sell.

### 2.4 Storing time series

Sales are stored as one row per store, product and day. This shape makes it easy to:

- load "the last N days" for a product quickly;
- replace or delete one upload's days;
- track a **history version** that changes whenever the data changes, so anything computed from the old data can be flagged as out of date.

### 2.5 Long work runs in the background

A forecast can take seconds; training can take minutes. A web request shouldn't wait that long: phones lose connections, and servers limit request times. The usual pattern:

1. The request is checked and saved as a **job**. The answer is `202 Accepted` with a job ID.
2. A **worker** process picks up jobs one by one and does the work.
3. The client **polls** ("is job 123 done?") or is notified.

The job queue can be a dedicated tool (Redis with Celery, RabbitMQ, a cloud queue) or a database table. Whatever is used, it should:

- survive restarts;
- never give one job to two workers;
- **retry** temporary failures (the model service was down) but not permanent ones (the product has no history);
- recover jobs left half-done by a crashed worker.

### 2.6 Serving models and keeping them honest

The backend must know:

- which model a shop uses;
- which **version** made each forecast;
- that the model is allowed to answer the question.

The last point is about **leakage**. A forecast made "as of" a past date must come from a model trained only on data up to that date. Otherwise it has seen the answer, and comparing it with actual sales is meaningless.

Each forecast is usually stored with its inputs' version, the model version and the baselines, so it can be explained and audited later.

### 2.7 Training per customer

When each customer gets their own model, the backend:

- checks **eligibility** (enough data to train and test fairly);
- sends the history to the training service as a background job and tracks it;
- applies a **gate:** the new model is used only if it beats the simple baseline on held-out days;
- **retrains** on a schedule as new data arrives.

### 2.8 Scheduled housekeeping

Background maintenance usually covers:
- requeuing stuck jobs;
- attaching actual sales to old forecasts, so accuracy can be measured;
- scheduling retraining;
- cleaning up expired records.

### 2.9 A written contract, tests and deployment

- **The API contract** is documented and machine-readable (OpenAPI), so the mobile client can be built against it.
- **Errors** have one consistent shape with stable codes the app can translate into plain messages.
- **Tests** run against a real database and a fake model service, so they are fast and repeatable.
- **Deployment** puts the services behind HTTPS, keeps secrets out of the code and backs up the database.

---

## 3. How this backend does it

Each part of section 2, as built here.

### 3.1 API and ML service apart (section 2.1)

| Part | What it is |
|---|---|
| API | FastAPI (Python 3.12), SQLAlchemy 2, Alembic migrations, PostgreSQL 16 |
| Worker | The same code, run as `python -m app.worker` |
| ML service | `mlengine`'s FastAPI service, from the `SemProject_MLEngine` repository |
| Reverse proxy | Caddy, for automatic HTTPS in production; it is the only exposed service |

The API calls the ML service through `app/ml_client.py`. Every call carries a shared secret (`X-Internal-Token`), and the ML service is only on the internal Docker network.

The ML service is **stateless for forecasting**: the backend sends each product's recent history and prices with every request. It keeps only model files (the Walmart model and each shop's trained models).

### 3.2 Accounts, sessions and permissions (section 2.2)

- **Passwords:** Argon2id. Signing in with an unknown email is still checked against a dummy hash, so the response time doesn't reveal which emails have accounts. Sign-in endpoints are rate-limited.
- **Tokens:**
  - access tokens are 15-minute JWTs;
  - refresh tokens are random 48-byte values (only their SHA-256 is stored), valid for 30 days and replaced on every use. Reusing an old one revokes all of that user's sessions, which is the standard defence against a stolen token.
- **Workspaces and stores:** a business is a *workspace* containing one or more *stores*. A user's *membership* gives them a role:
  - **Owner:** everything, including the team;
  - **Manager:** upload, train and forecast;
  - **Viewer:** read only.
- **Access checks:** every lookup of a store, product or forecast goes through one function, `deps.store_access`. Something a user may not see returns `404 Not Found`, not `403`, so its existence isn't revealed.
- **The demo workspace** (the Walmart "sample shop") is shared and read-only. Forecasts made there are private to the user who made them.
- **Account extras:**
  - **password reset:** 8-digit single-use codes, sent by email (in development, printed in the API logs);
  - **team invitations**, accepted inside the app by the invited email;
  - **password change.** A reset or change signs out every other session.

### 3.3 Getting the data in (section 2.3)

The import pipeline (`app/services/imports.py`, walked through in section 4):

- **Upload:** a CSV of up to 10 MB. The delimiter is detected, and a column mapping is suggested from common names. The date format is guessed from the values; if two formats fit equally well, the user must choose.
- **Staging:** parsed rows go to `import_rows`, one per store, product and day, with duplicates summed. Problems go to `import_issues` with their line numbers.
- **Zero or missing:** the owner answers whether days without a row mean "sold nothing". Closed days are excluded.
- **Spikes:** values more than 10 times the product's recent median are flagged as warnings, never removed.
- **Overlaps:** if the file covers days already stored, the owner chooses to keep the old values (`SKIP`) or replace them (`REPLACE`).
- **Idempotency:** the app sends an idempotency key with each upload. A repeat with the same key returns the original import; the same key with a different file is refused (`IDEMPOTENCY_KEY_REUSED`).
- **Delete:** an import can be removed later, taking its days with it.

### 3.4 Storing time series (section 2.4)

- `sales_daily` has one row per (store, product, date), with units sold, the price if known, and the import it came from. **A missing row means "not recorded", never zero.**
- Each store has `history_start`, `history_end` and a `history_version` that goes up with every confirmed or deleted import.
- **Readiness:** a product can be forecast once it has 21 days of history ending at the latest day, with at most 3 missing days in those 21. The Readiness endpoint reports this per product.

### 3.5 Long work in the background (section 2.5)

- **The queue is a Postgres table.** The job and the forecast are one row in `forecast_runs` (`jobId == forecastId`). There is no Redis or Celery to run. Jobs survive restarts.
- **Claiming:** the worker takes the next job with `SELECT … FOR UPDATE SKIP LOCKED`. Several workers can run without ever taking the same job. Single-product forecasts are claimed before batch runs, so one person's forecast isn't stuck behind a whole shop.
- **States:** `QUEUED → PREPARING_DATA → RUNNING_MODEL → FINALIZING → COMPLETED` or `FAILED`. The app shows these honestly.
- **Retries:**
  - an ML service outage is retried up to 3 attempts, then fails as retryable, and the app offers Retry;
  - a problem with the request itself (for example, not enough history) fails at once, with a code.
- **Polling:** the app polls `GET /v1/forecast-jobs/{id}` (or a batch) every few seconds.
- **Batches:** `POST /v1/stores/{sid}/forecast-batches` creates one ordinary job per product, grouped under a `forecast_batches` row. Progress is counted from the jobs' states. Asking again while a batch is still running returns the same batch instead of starting another.
- **For quick local runs,** `RUN_WORKER_IN_PROCESS=true` runs jobs inside the API process.

### 3.6 Serving models honestly (section 2.6)

- Each store has a `model_id`:
  - `m5-ca1-foods` for the demo store, which is the trained Walmart model;
  - `own-<workspaceId>` for a business, which exists only after the shop trains successfully.
- **The leakage rule** is applied twice, by the backend and again by the ML service: a model may serve a forecast as of date *a* only if its training data ended on or before *a*. The Walmart model has three versions trained to different dates for exactly this reason.
- **Business stores** always forecast from their latest day of history. Only the demo store can make forecasts from past dates ("replays").
- **Every forecast stores:**
  - its points: AI, "same as last day", "same weekday last week", and actual sales once known;
  - the model ID and version;
  - the history version used;
  - the ML engine version and request ID;
  - the price assumption;
  - any reason the AI wasn't used for that product.
- **Methods:**
  - `AI`: the model only;
  - `BASELINE`: the simple forecasts only;
  - `COMPARE`: both, which is what the business app uses.

  The baselines are always computed, even when the AI can't run for a product.
- **Capabilities:** `GET /v1/stores/{sid}/models/capabilities` tells the app what this store can do right now:
  - available methods and lengths (1 or 7 days);
  - whether AI is available, and if not, why (`NOT_TRAINED`, `TRAINING`, `NOT_ELIGIBLE`, `GATE_FAILED`, `NO_HISTORY`, `MODEL_MISSING`);
  - the date rules.

  The app builds its screens from this, so nothing is hardcoded.

### 3.7 Training per shop (section 2.7)

Walked through in section 6. In short:
- **Eligibility:** at least 10 products with 21+ days, 120 days of store history and 4,500 product-days. It is checked by the ML service and cached until the history changes.
- **Training:** a background job on the ML service, sent the shop's last 730 days.
- **Gate:** the shop's model is used only if it beats "same as last day" on both RMSE and MAE.
- **Retraining:** weekly, once a shop has a trained model and new sales have arrived.

### 3.8 Scheduled housekeeping (section 2.8)

The worker runs a maintenance pass every hour (`worker.maintenance`). It:
- requeues forecasts stuck in progress for more than 5 minutes, for example after a worker crash;
- fills in actual sales on past forecasts whose days have since been uploaded, so the app can show "Actuals now available";
- queues retraining for shops whose last training is more than 7 days old and whose history has changed.

### 3.9 Contract, tests and deployment (section 2.9)

- **Contract:**
  - [`api.md`](api.md) is the guide for the app developer;
  - [`openapi.json`](openapi.json) is generated from the code by `python scripts/export_openapi.py`, and a test fails if it is out of date.
- **Errors** always have the shape `{"error": {"code", "message", "details", "requestId"}}`, and the app maps each code to a plain message. Every response carries a request ID that also appears in the logs.
- **Tests:**
  - 67 tests run against a real Postgres database and a fake ML service built with `httpx.MockTransport`;
  - for the demo replay, the fake returns a response captured from the real ML service, and the tests check that requests sent to the ML service match the captured contract.
- **Deployment:** Docker Compose with four services (`db`, `ml`, `api`, `worker`) plus Caddy in production. Secrets come from `.env`. Database migrations run when the API container starts. See [`deployment.md`](deployment.md).
- **Local setup on Windows:** `scripts/setup-local.ps1` (see `SETUP.md` in the app repository).

---

## 4. Walkthrough: uploading sales

1. **Upload.** The app sends the CSV to `POST /v1/stores/{sid}/imports` with an idempotency key. The backend stores the file and returns:
   - the detected delimiter, columns and date format;
   - a suggested column mapping;
   - preview rows.
2. **Map.** The owner confirms the mapping and whether missing days mean zero (`PUT /v1/imports/{id}/mapping`). Every row is then:
   - parsed;
   - checked for bad or future dates, missing, negative or non-numeric units, bad prices and duplicates;
   - written to the staging table.

   Problems are recorded with their line numbers. This step can be repeated until the result looks right.
3. **Review.** `GET …/validation` and `GET …/errors` return the counts, the rejected rows and warnings such as spikes, and whether any days overlap earlier uploads.
4. **Confirm.** `POST …/confirm` with the overlap choice. In one database transaction, the backend:
   - adds new products;
   - inserts the days (replacing or keeping overlaps);
   - updates the store's history range;
   - increases `history_version`.

   It then attaches late actual sales to earlier forecasts and refreshes each product's demand class (whether it sells steadily or only on some days). The demand-class refresh is best effort: if the ML service is down, the import still succeeds.
5. **Afterwards:**
   - the readiness report shows which products can now be forecast;
   - earlier batches are flagged `outdated`;
   - the AI model screen shows progress toward eligibility.

---

## 5. Walkthrough: forecasting every product

1. **Request.** The app sends `POST /v1/stores/{sid}/forecast-batches {"horizonDays": 7}`.
2. **Checks, before anything is queued.** The user must be allowed to forecast, the store must have history, and the length must be supported. Any failure returns a typed `422` the app can explain, such as `NO_PRODUCTS` or `INSUFFICIENT_HISTORY`.
3. **Method.** `COMPARE` if the store has a model, otherwise `BASELINE`. The as-of date is the store's last day of history.
4. **Queue.** One `forecast_runs` row per product with sales history, all `QUEUED`, grouped by a `forecast_batches` row. The response is `202` with the batch's progress.
5. **For each job, the worker:**
   1. **Prepares the data:** up to 365 days of the product's history, known prices, and the first date it had a price.
   2. **Calls the model:** `POST /internal/v1/forecast` on the ML service, which applies the leakage rule again, checks the product's readiness, and returns the AI forecast (when it can) and both baselines for each day.
   3. **Finalises:** stores the daily points and the model details, and caches the product's demand class.
6. **Progress.** The app polls `GET /v1/forecast-batches/{id}` for "40 of 100 done". Once nothing is pending, it pages through `GET …/items`, one row per product with its daily points.
7. **Showing it.** The app shows each product's expected units, using the AI only when the shop's model beat the simple forecast, and opens any product as an ordinary forecast (`GET /v1/forecasts/{id}`).
8. **Failures.** Products that failed for a temporary reason can be retried together (`POST …/retry`). Products that failed for a lasting reason keep their error code.

A single-product forecast (`POST /v1/forecasts`) follows the same steps for one product.

---

## 6. Walkthrough: training a shop's own model

1. **Eligibility.** `GET /v1/stores/{sid}/models/eligibility` asks the ML service whether the workspace's history is enough. The app shows each check as "value of required", for example "64 of 120 days".
2. **Request.** An owner or manager taps Train: `POST …/models/train`. The backend checks eligibility again, locks the workspace (only one training run at a time) and creates a `QUEUED` training run.
3. **Submit.** The worker sends the workspace's recorded history (all stores, the last 730 days) and the store's country (for public holidays) to the ML service's `/train`. The run becomes `TRAINING` with the ML job ID.
4. **Poll.** About every 5 seconds the worker checks the ML job. If the ML service restarted and lost the job, it is resubmitted (up to 3 attempts). After 30 minutes the run fails with `TRAINING_TIMEOUT`.
5. **Outcome.**
   - `TRAINED`: the new model beat "same as last day" on both RMSE and MAE over the last 28 days. The ML service registers it as `own-<workspaceId>`. From the next forecast on, capabilities report AI as available and `COMPARE` forecasts include it.
   - `GATE_FAILED`: it didn't beat the baseline. Nothing is registered, and the shop keeps the simple forecast. The app shows the numbers and explains.
   - `NOT_ELIGIBLE`: the history wasn't enough after all.
6. **Retrain.** Every week the maintenance pass retrains shops that have a trained model and new sales since their last run.

---

## 7. Where this backend differs from larger systems

The design is sound for a single-server project. Larger systems often do the following:

| Larger systems often… | This backend | Why |
|---|---|---|
| Use a dedicated queue (Redis/Celery, RabbitMQ, cloud queues) | A Postgres table with `SKIP LOCKED` | One less service to run; jobs survive restarts; enough for this load |
| Push results to the phone (notifications, websockets) | The app polls every few seconds | Simpler and works through any network |
| Run many API servers behind a load balancer | One API process | Sign-in rate limits are kept in memory, which is only correct with one process |
| Store files in object storage (S3) | CSVs stored in Postgres until imported (≤ 10 MB) | No extra service |
| Run a model registry service (MLflow etc.) | The ML engine's own folder of versioned model files | Small number of models |
| Verify email addresses at sign-up | Not done | Invitations still require signing in with the exact invited address |
| Monitor forecast accuracy on dashboards | Actual sales are attached to past forecasts; no dashboard | Out of scope |
| Accept data from till systems automatically | CSV upload only | Businesses can export CSVs from almost anything |

---

## 8. Where to find things in the code

| Topic | Code |
|---|---|
| App setup, routers, request IDs | `app/main.py` |
| Settings (from `.env`) | `app/config.py` |
| Tables | `app/models.py`; migrations in `migrations/versions/` |
| Request and response shapes | `app/schemas.py` |
| Passwords and tokens | `app/security.py`, `app/routers/auth.py` |
| Who may see what | `app/deps.py` |
| Error shape | `app/errors.py` |
| Calls to the ML service | `app/ml_client.py` |
| Sales history and readiness | `app/services/history.py` |
| CSV imports | `app/services/imports.py`, `app/routers/imports.py` |
| Single forecasts and the job lifecycle | `app/services/forecasting.py`, `app/routers/forecasts.py` |
| Store-wide batches | `app/services/batches.py`, `app/routers/batches.py` |
| Which models exist, the leakage rule | `app/services/models_info.py`, `app/routers/models_api.py` |
| Own-data training and weekly retraining | `app/services/training.py`, `app/routers/training.py` |
| The worker and hourly maintenance | `app/worker.py` |
| Loading the Walmart demo store | `app/seed.py`, `app/cli.py` |
| Team, invitations, roles | `app/routers/team.py` |
| Research summary and export (research app only) | `app/routers/research.py` |
| Tests | `tests/` (`conftest.py` sets up the database and the fake ML service) |

---

## 9. Glossary

| Term | Meaning |
|---|---|
| `202 Accepted` | "Your request was accepted and will be done in the background" |
| As-of date | The last day of known sales; a forecast starts the day after |
| Baseline | A simple forecast that needs no training: "same as last day" or "same weekday last week" |
| Batch | One request that forecasts every product of a store |
| Capabilities | What a store can do right now: methods, lengths, AI availability and why |
| Gate | The check that a newly trained model beats the baseline before it is used |
| History version | A counter on each store that goes up whenever its sales data changes |
| Idempotency key | A value the client sends so a repeated request isn't processed twice |
| JWT | A signed token proving who the user is, checked on every request |
| Leakage rule | A model may only forecast from dates after its training data ended |
| Migration | A versioned change to the database's tables (Alembic) |
| ML service | The internal service that makes forecasts and trains models (`mlengine`) |
| Readiness | Whether a product has enough recent history (21 days, at most 3 missing) to forecast |
| Refresh token | A long-lived secret used only to get new access tokens; revocable |
| Replay | A forecast made from a past date in the demo store, compared with what really sold |
| `SKIP LOCKED` | A Postgres feature letting several workers take different jobs without waiting on each other |
| Staging table | Where uploaded rows wait, checked, until the user confirms |
| Worker | The background process that runs forecast and training jobs |
| Workspace | One business (tenant), containing its stores, products and team |
