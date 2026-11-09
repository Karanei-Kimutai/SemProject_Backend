# Retail Demand Forecasting: Backend API

This is the backend for the Retail Demand Forecasting mobile app. The app talks only to this API. The API stores businesses, products and sales history, runs forecast jobs, and calls the private ML service ([`SemProject_MLEngine`](../SemProject_MLEngine), the `mlengine` package) for every forecast.

```text
Android app ──HTTPS──▶ Caddy ──▶ API (FastAPI) ──┬──▶ PostgreSQL
                                   ▲            │
                       worker ─────┘            └──▶ ML service (mlengine, private network only)
```

**Status (9 November 2026): all four slices are done, plus store-wide forecasts.** Working end to end:
- sign-in with JWT access tokens and rotating refresh tokens;
- workspaces, stores and products, with role checks;
- sales history, data readiness and model capabilities;
- forecast jobs through a Postgres-backed worker, including demo replays with actual sales and stored backtests;
- store-wide forecasts: one request forecasts every product in a shop (a batch of ordinary jobs, with progress and retry);
- forecast history and CSV export;
- CSV sales imports for business stores: upload, column mapping, validation report, merge with skip or replace for overlapping days, and delete;
- own-data AI models: eligibility, training jobs on the ML service, the beat-the-baseline result, and weekly retraining;
- team invitations and roles, password reset and change, rate limiting on sign-in, and the research summary and export.

For demos without real business data, the Android app has a built-in sample sales file (Data → Upload sales data → Use the sample sales file), and the ML engine exports three Walmart sample CSVs (`python -m mlengine export-samples`; see [`docs/api.md`](docs/api.md#sample-data-for-demos)). The demo store (the Walmart "sample shop" in the app) is loaded from the ML engine's reference export. See [`docs/architecture.md`](docs/architecture.md#build-plan) for the build history.

## Stack

- Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16
- Argon2id password hashing, HS256 JWTs (PyJWT)
- httpx for the ML service client
- Docker Compose, with Caddy for automatic HTTPS

## Set it up on Windows (for building the app)

With `SemProject_MLEngine`, `SemProject_Backend` and the Android project in one folder, and Docker Desktop running:

```powershell
cd SemProject_Backend
powershell -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1
```

The script:
- checks the tools, and repairs model files that Git checked out with Windows line endings (LightGBM can't read them);
- writes `.env` with random local secrets;
- creates the ML engine's Python environment (with its test tools) and refreshes its demo export and sample CSVs;
- builds and starts the stack, and checks the API, the ML service and that every model loads;
- loads the demo store (about 7 minutes the first time);
- optionally creates a research user (`-ResearchEmail you@example.com`, or answer the prompt; only the app's `research` branch uses one);
- points the Android app at the API (`local.properties`, `adb reverse` for a USB phone).

Steps already done are skipped, so it's safe to re-run. It needs the M5 data in `SemProject_MLEngine\data\raw\` (it isn't in Git). The full walkthrough is `SETUP.md` in the app repository.

## Run it locally (without Docker)

You need PostgreSQL 16 and a running ML service (see the ML engine repository's `docs/operations.md`).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # then set DATABASE_URL, JWT_SECRET, ML_SERVICE_URL, ML_SERVICE_TOKEN
alembic upgrade head

# Demo data: export it from the ML engine repo, then load it (2.8M rows: ~2 min natively, ~7 min in Docker Desktop)
(cd ../SemProject_MLEngine && python -m mlengine export-reference)
python -m app.cli seed-reference ../SemProject_MLEngine/data/processed/reference

python -m app.cli create-user you@example.com --research   # optional: a research user
uvicorn app.main:app --reload --port 8000                    # API; docs at http://localhost:8000/docs
python -m app.worker                                         # in a second terminal: runs forecast jobs
```

For quick local use, `RUN_WORKER_IN_PROCESS=true` runs each job inside the API process, so you don't need a separate worker.

## Tests

```bash
createdb forecast_test                                   # once
TEST_DATABASE_URL=postgresql+psycopg://user@localhost/forecast_test pytest
```

There are 67 tests. They run against a real Postgres database and a fake ML service. For the demo replay, the fake ML service returns a response captured from the real one (`tests/fixtures/ml/`). The tests also check that requests sent to the ML service match the captured contract.

## Documentation

| Document | What it covers |
| --- | --- |
| [`docs/how-the-backend-works.md`](docs/how-the-backend-works.md) | **Start here if you are new:** what a forecasting backend usually does, how this one does each part, and walkthroughs of uploading sales, forecasting every product and training a shop's model |
| [`docs/api.md`](docs/api.md) | Guide to the public API for the Android app: auth flow, every endpoint, error codes, polling |
| [`docs/openapi.json`](docs/openapi.json) | Machine-readable contract for generating the app's network client (`python scripts/export_openapi.py`; a test keeps it current) |
| [`docs/architecture.md`](docs/architecture.md) | Design: modules, data model, forecast job lifecycle, batches, imports, training, security, build plan |
| [`docs/deployment.md`](docs/deployment.md) | Deploying to one Linux server with Docker Compose and Caddy; backups; updates |
| [`scripts/setup-local.ps1`](scripts/setup-local.ps1) | One-command local setup on Windows (above) |
| `SETUP.md` in the app repository | Setting up all three repositories from scratch |
