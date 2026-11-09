# Public API Guide (for the Android app)

- Base URL: `https://<API_DOMAIN>`. Locally, `http://10.0.2.2:8000` from the Android emulator.
- Interactive docs: `/docs`. Contract: [`openapi.json`](openapi.json). Generate the Retrofit/Kotlin client from it rather than writing it by hand.
- JSON fields are camelCase. Dates are `YYYY-MM-DD` in the store's timezone. Timestamps are ISO 8601 UTC. IDs are UUIDs.
- Send `X-Request-Id` if you have one. Every response echoes it, or a generated ID.

## Errors

Every error has the same shape. Map `code` to your own localised message, and fall back to `message`, which is already plain language.

```json
{"error": {"code": "INSUFFICIENT_HISTORY", "message": "This product needs at least 21 days of sales history.",
           "details": {"recordedDays": 10, "missingDays": 11, "requiredDays": 21}, "requestId": "req_…"}}
```

| Code | HTTP | When |
| --- | --- | --- |
| `INVALID_REQUEST` | 422 | Body or query is invalid; `details.errors[]` gives `loc` and `msg` |
| `UNAUTHORIZED` | 401 | Missing or expired access token. Refresh once, then send the user to sign-in |
| `INVALID_CREDENTIALS` | 401 | Wrong email or password |
| `INVALID_REFRESH_TOKEN` | 401 | Refresh token expired, revoked or reused. Sign in again |
| `EMAIL_TAKEN`, `STORE_CODE_TAKEN`, `SKU_TAKEN` | 409 | Duplicates |
| `REGISTRATION_CLOSED` | 403 | Self sign-up is disabled on this server |
| `FORBIDDEN` | 403 | Role doesn't allow it (viewer, demo data is read-only, research-only option) |
| `NOT_FOUND` | 404 | Also returned for things the user isn't allowed to see |
| `INSUFFICIENT_HISTORY` | 422 | Fewer than 21 days of history ending on the as-of date (AI method) |
| `TOO_MANY_GAPS` | 422 | More than 3 unrecorded days in the last 21 (AI method) |
| `AI_NOT_AVAILABLE` | 422 | No model for this store yet. Offer the baseline |
| `HORIZON_NOT_SUPPORTED` | 422 | `details.horizons` lists the allowed values |
| `AS_OF_OUT_OF_RANGE` | 422 | `details.min` / `details.max`, or `details.calendarEnd` |
| `NO_MODEL_FOR_AS_OF` | 422 | Research only: the chosen version was trained after that date (leakage rule) |
| `MODEL_NOT_FOUND` | 404 | Research only: unknown version |
| `ML_SERVICE_UNAVAILABLE` | 503 | Forecasting service is down. Retry later |
| `NOT_RETRYABLE` | 409 | Retry on a job that hasn't failed, or on a batch with no retryable failures |
| `NO_PRODUCTS`, `BATCH_TOO_LARGE` | 422 | Store-wide forecast with nothing to forecast, or more than `details.max` products |
| `RATE_LIMITED` | 429 | Too many attempts; wait `Retry-After` seconds |
| `INVALID_RESET_CODE` | 400 | Password reset code wrong, used or expired |
| `LAST_OWNER`, `ALREADY_MEMBER`, `INVITATION_NOT_PENDING` | 409 | Team management (see Team) |

## Authentication

1. `POST /v1/auth/register {email, password (≥10 chars), displayName}` → `201` and tokens. `POST /v1/auth/login {email, password}` → tokens.
2. The tokens response is `{accessToken, refreshToken, tokenType: "Bearer", expiresIn: 900}`.
3. Send `Authorization: Bearer <accessToken>` on every request.
4. When a request returns `401 UNAUTHORIZED`, call `POST /v1/auth/refresh {refreshToken}` **once**. You get a *new* pair, and the old refresh token stops working. Replace both. If refresh fails, go to sign-in.
5. Reusing an old refresh token ends all of that user's sessions, because it is treated as theft. So never refresh from two places at once: serialise refreshes in an OkHttp `Authenticator`.
6. `POST /v1/auth/logout {refreshToken}` → `204`.
7. Store tokens in Android Keystore-backed encrypted storage.

`GET /v1/me` returns `{id, email, displayName, isResearch, workspaces: [{workspaceId, name, role, isReference}]}`. `role` is `OWNER`, `MANAGER`, `VIEWER` or `REFERENCE`.

## Permissions

| | Demo (Reference) workspace | Own workspace: OWNER/MANAGER | Own workspace: VIEWER |
| --- | --- | --- | --- |
| Read stores, products, history | yes | yes | yes |
| Create forecasts | yes | yes | no |
| Add or edit products and stores | no (read-only) | yes | no |
| See forecasts | only your own | everyone's in the workspace | everyone's |

## Workspaces, stores, products

| Endpoint | Notes |
| --- | --- |
| `GET /v1/workspaces` | The demo workspace first, then your own |
| `POST /v1/workspaces {name}` | You become `OWNER` |
| `GET /v1/workspaces/{wid}/stores` | `[{id, workspaceId, code, name, country, subdivision, timezone, currency, isReference, historyStart, historyEnd}]` |
| `POST /v1/workspaces/{wid}/stores {code, name, country, subdivision?, timezone, currency}` | `timezone` is IANA, e.g. `Africa/Nairobi`; `country` is ISO-2 (used for holidays) |
| `GET /v1/stores/{sid}` | One store |
| `GET /v1/stores/{sid}/products?q=&category=&demandClass=&page=&pageSize=` | `{page, pageSize, total, items[]}`; `q` searches SKU and name |
| `POST /v1/stores/{sid}/products {sku, name, category?, unitOfMeasure?, defaultPrice?}` | |
| `GET/PATCH /v1/stores/{sid}/products/{pid}` | `PATCH` takes any of `name, category, unitOfMeasure, defaultPrice, isActive` |
| `GET /v1/stores/{sid}/products/{pid}/history?from=&to=` | One point per day: `{date, units, price}`. **`units: null` means not recorded, which is different from 0.** Defaults to the last 28 days; maximum two years |

A product's `demandClass` is `{class, label, adi, cv2, zeroShare}` or `null`. It is filled in after the product's first forecast. `class` is `SMOOTH`, `ERRATIC`, `INTERMITTENT`, `LUMPY` or `INSUFFICIENT_DATA`. Show `label`, which is plain language.

## Before a forecast: capabilities and readiness

`GET /v1/stores/{sid}/models/capabilities`. Build the forecast form from this, and never hardcode horizons or dates.

```json
{"storeId": "…", "isReference": true, "aiAvailable": true, "aiUnavailableReason": null,
 "methods": ["AI", "BASELINE", "COMPARE"], "horizons": [1, 7], "frequency": "DAILY",
 "asOf": {"default": "2016-05-22", "editable": true, "replayMin": "2016-04-24", "replayMax": "2016-05-21",
          "researchMin": null, "calendarEnd": "2016-06-19"},
 "minHistoryDays": 21, "maxMissingDaysInLookback": 3, "predictionIntervals": false,
 "baselines": [{"id": "naive_last_value", "label": "Same as last day"},
               {"id": "seasonal_naive_7", "label": "Same day last week"}],
 "activeModel": {"id": "m5-ca1-foods", "version": "1.1.0"}}
```

- `asOf.editable` is false for business stores: the forecast always starts after the latest day of history.
- In the demo, a date from `replayMin` to `replayMax` is a **replay**, with actual sales known. `default` is a true forecast.
- `researchMin` is set only for research users.
- `aiUnavailableReason` is one of `NOT_TRAINED`, `NO_HISTORY` or `MODEL_MISSING`. When it is set, `methods` is `["BASELINE"]`.

`GET /v1/stores/{sid}/readiness[?asOfDate=]` returns `{asOfDate, minHistoryDays, maxMissingDaysInLookback, stale, productsReady, productsTotal, products: [{productId, sku, ready, reasons[], recordedDaysInLookback, missingDaysInLookback, latestDate}]}`. Products that aren't ready come first. `reasons` can include `INSUFFICIENT_HISTORY`, `TOO_MANY_GAPS` and `STALE_HISTORY`. `stale: true` means a business store's history doesn't reach yesterday.

`GET /v1/stores/{sid}/models/active` returns `{model, capabilities, card}`. `card` is the model card for the model info screen: `title`, `trainedOn`, `predicts`, `inputsPlainLanguage`, `backtest`, `horizonAccuracy.byDay[]`, `limitations`.

## Forecasts

### Create, then poll

`POST /v1/forecasts` takes:

```json
{"storeId": "…", "productId": "…", "asOfDate": "2016-05-08", "horizonDays": 7, "method": "COMPARE"}
```

- `asOfDate` is optional and defaults to the latest history day.
- `method` is `AI`, `BASELINE` or `COMPARE` (the default).
- `modelVersion` is for research users only.

The response is `202` with a job object:

```json
{"jobId": "…", "forecastId": "…", "status": "QUEUED", "createdAt": "…", "startedAt": null,
 "finishedAt": null, "error": null, "retryable": false}
```

Problems found up front return `422` with one of the codes above. Nothing is queued in that case.

Poll `GET /v1/forecast-jobs/{jobId}` every 1 s for the first 10 s, then every 3 s, and stop after 2 minutes. `status` moves through `QUEUED → PREPARING_DATA → RUNNING_MODEL → FINALIZING → COMPLETED | FAILED`. A forecast usually completes in under a second.

If it fails, `error` is `{code, message}`. Offer **Retry** only when `retryable` is true: `POST /v1/forecast-jobs/{jobId}/retry` → `202`. A job can also fail with a per-product code, for example `PRODUCT_NOT_IN_MODEL` with `method: AI`. Show its message.

### Result

`GET /v1/forecasts/{forecastId}` returns:

```json
{
  "forecastId": "…", "status": "COMPLETED", "createdAt": "…", "generatedAt": "…",
  "storeId": "…", "isReference": true,
  "product": {"id": "…", "sku": "FOODS_3_090", "name": "FOODS_3_090", "unitOfMeasure": "units",
              "demandClass": {"class": "SMOOTH", "label": "Steady seller", "adi": 1.0, "cv2": 0.2, "zeroShare": 0.0,
                              "observedDays": 365, "saleDays": 365}},
  "asOfDate": "2016-05-08", "isReplay": true, "method": "COMPARE", "error": null,
  "horizon": {"startDate": "2016-05-09", "endDate": "2016-05-15", "days": 7, "frequency": "DAILY", "validated": true},
  "model": {"id": "m5-ca1-foods", "version": "1.1.0-bt", "featureSchema": "m5-v1", "trainingEndDate": "2016-04-24"},
  "baselines": [{"id": "naive_last_value", "label": "Same as last day"}, {"id": "seasonal_naive_7", "label": "Same day last week"}],
  "aiUnavailableReason": null, "priceAssumption": "KNOWN", "missingDaysInLookback": 0, "warnings": [],
  "points": [{"date": "2016-05-09", "ai": 57.1206, "baseline": 77.0, "seasonalNaive": 59.0, "actual": 72.0}],
  "history": [{"date": "2016-04-11", "actual": 48.0}],
  "predictionIntervals": null,
  "runMetrics": {"window": "2016-05-09/2016-05-15", "n": 7, "ai": {"mae": 18.23}, "baseline": {"mae": 22.57}},
  "productBacktest": {"productId": "FOODS_3_090", "n": 28, "window": "…", "demandClass": "…",
                      "ai": {"mae": 11.5, "rmse": 14.83, "wape": 0.195}, "naive_last_value": {…}, "seasonal_naive_7": {…}},
  "historyVersion": 1, "mlengineVersion": "0.1.0", "mlRequestId": "fc_…"
}
```

The points in this example are real output for FOODS_3_090.

- `points[].baseline` is the main baseline ("Same as last day", `naive_last_value`), which the app calls the **simple forecast**: every forecast day gets the units from the product's most recent recorded day up to the as-of date (a recorded 0 counts; unrecorded days are skipped). `seasonalNaive` is the second one: each day gets the units from the same weekday in the 7 days before the forecast starts, or `null` if that day wasn't recorded. Both are computed for every run, need no model, and have no minimum history beyond one recorded day in the 365 days sent.
- `ai` is `null` when `method` is `BASELINE` or `aiUnavailableReason` is set.
- `actual` is non-null only on replays, or once actual sales arrive later.
- `history` holds the last 28 days before the as-of date, for the chart.
- `runMetrics` is present only when actuals exist. Show it as "Error on these days".
- `productBacktest` (demo only) is the 28-day historical backtest for this product. Always show AI and baseline from the same block, with the small-sample note.
- `priceAssumption`: `KNOWN` (prices known for every forecast day), `LAST_KNOWN` (last price carried forward), `NONE` or `NOT_USED`.
- Values are expected units, with decimals. Show one decimal place by default (see spec §8.10).
- While the job hasn't completed, the response has only the header fields and `error`.

### History, export, delete

- `GET /v1/forecasts?storeId=&productId=&replay=&status=&batch=&page=&pageSize=` returns `{page, pageSize, total, items: [{forecastId, status, storeId, productId, sku, productName, asOfDate, horizonDays, method, isReplay, modelVersion, createdAt, actualsAvailable}]}`, newest first. `actualsAvailable: true` means a past true forecast now has actual sales: tag it "Actuals now available". `batch=false` leaves out the runs created by store-wide batches (below), `batch=true` lists only those; leave it out for both.
- `GET /v1/forecasts/{id}/export?format=csv` returns a CSV file.
- `DELETE /v1/forecasts/{id}` → `204` (soft delete). You can delete your own forecasts; owners and managers can delete any in their workspace.

## Store-wide forecasts (batches)

One request forecasts every product of a store. This is the main forecast for a business owner: "what will I sell this week, product by product". Each product becomes an ordinary forecast run, so every item also opens with `GET /v1/forecasts/{forecastId}`.

| Endpoint | Notes |
| --- | --- |
| `POST /v1/stores/{sid}/forecast-batches {horizonDays, productIds?}` | `202` with a batch (below). Without `productIds` it forecasts every active product with sales history in the store. Owners and managers (and anyone in the demo). If the same user already has a batch running for this store and length, that batch comes back with `200` instead of a new one |
| `GET /v1/forecast-batches/{bid}` | Progress: poll every 2–3 s while `status` is `RUNNING` |
| `GET /v1/forecast-batches/{bid}/items?q=&status=&page=&pageSize=` | One row per product, ordered by product name; `q` searches name and SKU; `pageSize` up to 200 |
| `GET /v1/stores/{sid}/forecast-batches/latest?horizonDays=` | The newest batch for the store (in the demo: your newest). `404` if there is none yet |
| `POST /v1/forecast-batches/{bid}/retry` | Queues again the products that failed for a temporary reason (ML service down, timeout). `409 NOT_RETRYABLE` if there are none |

The batch:

```json
{"batchId": "…", "storeId": "…", "status": "RUNNING", "method": "COMPARE", "asOfDate": "2026-11-08",
 "horizonDays": 7, "total": 100, "completed": 40, "failed": 0, "pending": 60,
 "createdAt": "…", "finishedAt": null, "outdated": false}
```

- `method` is chosen for you: `COMPARE` (AI plus the baselines) when the store has an AI model, otherwise `BASELINE`. For a business store `asOfDate` is always the latest day of history.
- `status` is `COMPLETED` once nothing is pending, even if some products failed (`failed > 0`); their items carry the reason.
- `outdated: true` means the store's sales history changed after the batch (an import or a deleted import). Offer to forecast again.
- Single forecasts run before batch runs, so opening one product is never stuck behind a whole store.
- The demo store's per-product backtest is left out of batch runs; open the product's own forecast for it.

An item:

```json
{"forecastId": "…", "productId": "…", "sku": "SKU-1", "productName": "Maize flour 2kg", "unitOfMeasure": "units",
 "category": "Flour", "demandLabel": "Steady seller", "status": "COMPLETED", "error": null,
 "aiUnavailableReason": null,
 "points": [{"date": "2026-11-09", "ai": 12.4, "baseline": 11.0, "seasonalNaive": 13.0, "actual": null}]}
```

`points` is empty until the item is `COMPLETED`. `ai` is `null` when the store has no model or this product can't use it (`aiUnavailableReason`); show the baseline then.

## Sales imports (CSV)

These follow the import wizard in spec §8.6. Imports are allowed only in your own workspaces, by OWNER and MANAGER (`403` in the demo workspace).

### 1. Upload: `POST /v1/stores/{sid}/imports`

- Multipart field `file`: a UTF-8 CSV of up to 10 MB and 500,000 rows. Comma, semicolon, tab and `|` delimiters are detected.
- Send an `Idempotency-Key` header, for example a UUID generated per user action. Repeating the same key with the same file returns the same import; the same key with a different file returns `409 IDEMPOTENCY_KEY_REUSED`.
- The response is `201`, an import with `status: "UPLOADED"`. Nothing reaches the history yet.

```json
{"id": "…", "storeId": "…", "filename": "sales.csv", "sizeBytes": 2048, "status": "UPLOADED",
 "columns": ["sales_date", "product_id", "product_name", "category", "units_sold", "unit_price"],
 "detected": {"delimiter": ",", "preview": [{"sales_date": "2026-10-01", "...": "..."}],
              "suggestedMapping": {"salesDate": {"column": "sales_date", "confidence": "HIGH"},
                                   "unitsSold": {"column": "units_sold", "confidence": "HIGH"}},
              "dateFormats": ["YYYY-MM-DD"], "suggestedDateFormat": "YYYY-MM-DD", "dateFormatAmbiguous": false,
              "unusedColumns": []},
 "mapping": null, "missingDaysAreZero": null, "summary": null, "result": null}
```

- Pre-fill the mapping screen from `suggestedMapping`. Highlight `confidence: "CHECK"` entries for the user to confirm.
- `unusedColumns` (revenue, promotion, stock, returns) get the label "Saved, not used for forecasting yet".
- Errors: `EMPTY_FILE`, `FILE_NOT_UTF8`, `NO_HEADER`, `BAD_HEADER` (duplicate or empty column names), `FILE_TOO_LARGE` (413).

### 2. Map and validate: `PUT /v1/imports/{iid}/mapping`

```json
{"columns": {"salesDate": "sales_date", "productId": "product_id", "productName": "product_name",
             "unitsSold": "units_sold", "unitPrice": "unit_price", "category": "category"},
 "dateFormat": "YYYY-MM-DD",
 "missingDaysAreZero": false}
```

**Fields** (keys of `columns`):

| Field | Required? | Notes |
| --- | --- | --- |
| `salesDate` | yes | |
| `unitsSold` | yes | |
| `productId` or `productName` | at least one | With a name only, the SKU is made from the name: "Sugar 1kg" → `SUGAR-1KG` |
| `storeId` | no | Store **codes** in this workspace, for multi-store files. Rows without one belong to the store in the URL |
| `unitPrice` | no | |
| `category` | no | |
| `unitOfMeasure` | no | |
| `storeClosed` | no | `true`/`yes`/`1` marks the store-day closed |

**`dateFormat`** is one of `YYYY-MM-DD`, `DD/MM/YYYY`, `MM/DD/YYYY`, `YYYY/MM/DD`, `DD-MM-YYYY`, `DD.MM.YYYY`. You only need to send it when `dateFormatAmbiguous` is true (for example `01/02/2026`); otherwise you get `DATE_FORMAT_REQUIRED` with `details.options`.

**`missingDaysAreZero`** is the wizard's required question, "If a product has no row for a day, does that mean it sold zero?"
- `true`: each product's missing days, from its first day in the file to the file's last day, are stored as 0, carrying the last price.
- `false`: they stay *not recorded*.
- Closed days are never filled.

**What validation does:**
- **Rejects these rows** (each becomes an `ERROR` issue): `INVALID_DATE`, `FUTURE_DATE`, `UNKNOWN_STORE`, `MISSING_PRODUCT`, `PRODUCT_ID_TOO_LONG`, `MISSING_UNITS` (units are never worked out from revenue), `INVALID_UNITS`, `NEGATIVE_UNITS`.
- **Keeps these rows but flags them** (`WARNING`):
  - `INVALID_PRICE` and `NON_POSITIVE_PRICE`: the price is dropped;
  - `DUPLICATE_ROWS_SUMMED`: several rows for the same product-day are added together;
  - `SPIKE`: more than 10× the product's recent median. It is kept unchanged.

The response is the import with `summary` filled in. `status` is:
- `READY`: no issues;
- `NEEDS_ATTENTION`: some rows rejected or flagged, but the accepted rows can still be imported;
- `FAILED`: nothing importable.

You can send a new mapping any time before confirming.

```json
"summary": {"rowsRead": 642, "rowsAccepted": 642, "rowsRejected": 0, "rowsWithWarnings": 0,
            "rowsSummedAsDuplicates": 0, "dailyRecords": 719, "zeroFilledDays": 77, "closedDays": 0,
            "dateRange": {"from": "2026-09-08", "to": "2026-11-06"}, "products": 12, "newProducts": 12,
            "stores": ["NBO-1"], "overlappingDays": 0, "errorsByCode": {}, "warningsByCode": {},
            "issuesTruncated": false, "unusedColumns": []}
```

Other mapping errors: `REQUIRED_FIELDS_UNMAPPED` (`details.fields`), `UNKNOWN_COLUMN`, `COLUMN_MAPPED_TWICE`, `TOO_MANY_ROWS`, `IMPORT_LOCKED` (409, already imported).

### 3. Review

- `GET /v1/imports/{iid}/validation` returns `{importId, status, summary, preview: [{storeId, productId, productName, date, unitsSold, unitPrice, category, sourceRows, zeroFilled}]}`. The preview is the first 20 normalised daily records. `overlappingDays` is recounted against the current history.
- `GET /v1/imports/{iid}/errors?severity=ERROR|WARNING&page=&pageSize=` returns `{items: [{severity, code, rowNumber, column, value, message}]}`. `rowNumber` is the line in the file, where the header is line 1. Up to 5,000 issues are stored; `summary.issuesTruncated` says whether there were more. The counts in `errorsByCode` are always complete.

### 4. Confirm: `POST /v1/imports/{iid}/confirm {"overlapPolicy": "SKIP" | "REPLACE"}`

`overlapPolicy` decides what happens to days already in the history:
- `SKIP` (the default) keeps the existing days;
- `REPLACE` overwrites them with the file's values.

Then the backend:
1. creates new products, filling in missing categories;
2. merges the rows;
3. updates the store's history range;
4. attaches actual sales to past forecasts;
5. refreshes demand classes.

It returns the import with `status: "IMPORTED"` and:

```json
"result": {"overlapPolicy": "SKIP", "recordsInserted": 719, "recordsReplaced": 0, "overlapsSkipped": 0,
           "productsCreated": 12, "demandClassesUpdated": 12}
```

Afterwards, refresh readiness and capabilities. If the import isn't `READY` or `NEEDS_ATTENTION`, the response is `409 IMPORT_NOT_READY`.

### Other import endpoints

- `GET /v1/stores/{sid}/imports?status=&page=&pageSize=`: the Data screen's import list, newest first.
- `GET /v1/imports/{iid}`: one import.
- `DELETE /v1/imports/{iid}`: an imported batch's rows are removed from the history and `result.recordsDeleted` is set. Days that the batch *replaced* don't come back. Any other import is simply discarded.
- `GET /v1/stores/{sid}/history/summary`: `{historyStart, historyEnd, productsWithData, daysWithData, records, lastImportAt}` for the Data screen card.

### Status mapping for the Data screen

| API status | Spec label |
| --- | --- |
| `UPLOADED` | Uploaded |
| `NEEDS_ATTENTION` | Needs Attention |
| `READY` | Ready to Import |
| `IMPORTED` | Imported |
| `FAILED` | Failed |

Validation and confirmation run within the request, so a 10 MB file takes a few seconds. There are no separate "Validating" or "Importing" states to poll for: show a progress indicator while the request runs.

## Own-data AI models (training)

A business workspace gets its own AI model, trained on its own uploaded sales. The demo model can't be used on other products.
- **One model per workspace.** It learns from all of the workspace's stores; each store and product is its own series. Public holidays come from the requesting store's country and subdivision.
- **Every training run ends in one of three outcomes:**

| `outcome` | Meaning | What to show |
| --- | --- | --- |
| `TRAINED` | The AI beat "Same as last day" on **both** RMSE and MAE over the last 28 days | "AI forecasts are now available", with the backtest |
| `GATE_FAILED` | It didn't beat the baseline. Nothing changes; an earlier model, if any, stays active | Say so plainly, with the numbers. Baselines remain. This is a valid research result |
| `NOT_ELIGIBLE` | Not enough history | Progress toward eligibility |

### Eligibility: `GET /v1/stores/{sid}/models/eligibility`

```json
{"workspaceId": "…", "eligible": false, "storesInModel": ["NBO-1"],
 "checks": {"productsWithEnoughHistory": {"value": 30, "required": 10, "passed": true},
            "storeHistoryDays": {"value": 60, "required": 120, "passed": false},
            "observedProductDays": {"value": 1800, "required": 4500, "passed": false}},
 "rules": {…}, "latestRun": null}
```

Use it for the dashboard's progress line ("60 of 120 days"). The result is cached until the history changes. Viewers can read it. On the demo store, every training endpoint returns `409 REFERENCE_MODEL_FIXED`.

### Train: `POST /v1/stores/{sid}/models/train`

- Owners and managers only, with no body.
- Returns `202` with a training run, or `200` with the run already in progress (only one runs at a time per workspace).
- Returns `422 NOT_ELIGIBLE` with `details.checks` if the workspace isn't eligible.

Poll `GET /v1/training-runs/{runId}` every 2–3 s. `status` moves `QUEUED → TRAINING → COMPLETED | FAILED`. Training takes about 20–30 s for 100 products × 1 year.

```json
{"id": "…", "workspaceId": "…", "status": "COMPLETED", "outcome": "TRAINED", "trigger": "MANUAL",
 "createdAt": "…", "submittedAt": "…", "finishedAt": "…", "historyRows": 35095,
 "model": {"id": "own-<workspaceId>", "version": "1.0.0"}, "horizons": [1, 7],
 "eligibility": {…},
 "backtest": {"window": {"startDate": "2026-10-10", "endDate": "2026-11-06"}, "oneStepAhead": true,
              "ai": {"n": 2800, "mae": 1.312, "rmse": 2.503, "wape": 0.573},
              "naive_last_value": {"n": 2800, "mae": 1.566, "rmse": 3.062, "wape": 0.684},
              "seasonal_naive_7": {…}},
 "gate": {"passed": true, "rule": "AI RMSE and MAE both below naive_last_value"},
 "horizonAccuracy": {"validated": true, "byDay": [{"day": 1, "aiRmse": 2.43, "aiMae": …, "naiveRmse": 2.96, "naiveMae": …}]},
 "error": null, "retryable": false}
```

The numbers are real, from the sample shop.

- `horizons` is `[1, 7]` only if 7-day forecasts also beat both baselines; otherwise `[1]`.
- `FAILED` means something went wrong, not a bad model. `error.code` is one of `ML_SERVICE_UNAVAILABLE`, `TRAINING_LOST`, `TRAINING_TIMEOUT` or `TRAINING_FAILED`. If `retryable` is true, offer `POST /v1/training-runs/{runId}/retry`.
- `GET /v1/stores/{sid}/models/training-runs?page=` lists the history of runs, newest first.

### After training

- **Capabilities:** `GET /v1/stores/{sid}/models/capabilities` returns `aiAvailable: true`, `methods: ["AI", "BASELINE", "COMPARE"]`, the model's `horizons`, `activeModel: {id: "own-…", version}`, and `training: {runId, status, outcome, finishedAt}`.
- **When AI isn't available yet,** `aiUnavailableReason` is `NOT_TRAINED`, `TRAINING`, `GATE_FAILED` or `NOT_ELIGIBLE`. These map to the dashboard's "Not yet eligible / Training / Baselines only".
- **Model card:** `GET /v1/stores/{sid}/models/active` returns the model card. It has the workspace's own backtest and per-day accuracy.
- **Forecasts:** they work exactly as in the demo. Own-data forecasts have no `productBacktest`; the workspace-level backtest is on the model card. `priceAssumption` is usually `LAST_KNOWN`, because businesses don't upload future prices.
- **Retraining:** the backend retrains automatically each week if new sales were imported since the last training (`trigger: "SCHEDULED"`).

## Sample data for demos

You don't need real business data. `python -m mlengine export-samples`, run in the ML engine repo, writes three CSVs cut from a real Walmart store that the demo model never saw (M5 TX_1), with dates shifted so the history ends yesterday. `data/processed/samples/README.md` lists the wizard answers for each file.

| File | Use it to demo | Expected |
| --- | --- | --- |
| `sample_shop_eligible.csv` | Import → train → AI forecasts | Needs Attention (real spikes), then `TRAINED` with horizons `[1, 7]` |
| `sample_shop_small.csv` | Progress to eligibility, baseline-only forecasts | `NOT_ELIGIBLE`: 60 of 120 days |
| `sample_shop_messy.csv` | The validation report | 5 rejected rows (one each of five error types), plus warnings |

Create the store with country `US`, subdivision `TX`, currency `USD` and timezone `America/Chicago`.

## Account: password reset and change

| Endpoint | Notes |
| --- | --- |
| `POST /v1/auth/password-reset/request {email}` | Always `202 {"status": "SENT_IF_ACCOUNT_EXISTS"}`, so nobody can test which emails have accounts. If the account exists, an **8-digit code** is emailed. It works once, for 30 minutes, and only the newest code works |
| `POST /v1/auth/password-reset/confirm {email, code, newPassword}` | Returns new tokens and signs the user out everywhere else. A wrong or expired code returns `400 INVALID_RESET_CODE`. After 5 wrong attempts the code stops working, even the right one: ask for a new one |
| `POST /v1/auth/change-password {currentPassword, newPassword}` | Signed in. Returns new tokens; other sessions are signed out. A wrong current password returns `401 INVALID_CREDENTIALS` |

The app flow is: "Forgot password?" → enter email → "If an account exists, we've emailed a code" → enter code and new password → signed in.

### Rate limits

Too many attempts return `429 RATE_LIMITED` with a `Retry-After` header (seconds) and `details.retryAfterSeconds`. Show "Too many attempts. Try again in N seconds."

| Endpoint | Limit |
| --- | --- |
| login | 5 per minute per email, 20 per minute per IP |
| register | 5 per hour per IP |
| refresh | 60 per minute per IP |
| password-reset request | 3 per hour per email, 5 per hour per IP |
| password-reset confirm | 10 per 10 minutes per IP |

## Team (spec §8.12)

Only **owners** manage the team. Any member can see the member list and leave. The demo workspace has no team (`403`).

| Endpoint | Notes |
| --- | --- |
| `GET /v1/workspaces/{wid}/members` | `[{userId, email, displayName, role, joinedAt}]` |
| `PATCH /v1/workspaces/{wid}/members/{userId} {role}` | Owners only. `role` is `OWNER`, `MANAGER` or `VIEWER` |
| `DELETE /v1/workspaces/{wid}/members/{userId}` | Owners remove anyone; anyone can remove themselves (leave) |
| `POST /v1/workspaces/{wid}/invitations {email, role}` | Owners only, `201`. Emails a notification, and a new invitation replaces a pending one to the same address. `409 ALREADY_MEMBER` if they're already in the workspace |
| `GET /v1/workspaces/{wid}/invitations` | Pending invitations (owners), for the "pending invitations" list |
| `DELETE /v1/workspaces/{wid}/invitations/{iid}` | Revoke |
| `GET /v1/me/invitations` | Invitations to the signed-in user's email: `[{id, workspaceId, workspaceName, email, role, status, invitedBy, createdAt, expiresAt}]` |
| `POST /v1/invitations/{iid}/accept` / `decline` | Only the invited email's user can respond. `409 INVITATION_NOT_PENDING` if it was revoked, already answered, or expired (after 14 days) |

**Accepting doesn't use email links.** The invited person signs in, or registers with the invited email, and the app shows `GET /v1/me/invitations`. Check it after sign-in, and show a badge in Settings → Invitations. After accepting, reload `GET /v1/me`.

**Last owner:** removing or demoting a workspace's last owner returns `409 LAST_OWNER`.

## Research (research users only)

Both endpoints return `403` for other users. They include completed forecasts the user can see: in the demo workspace, their own runs; in their own workspaces, everyone's.

- **`GET /v1/research/summary?storeId=&from=&to=`** compares AI and the baselines on every forecast day whose actual sales are known. That means replays, and past forecasts once actuals were imported. Only days where all three methods and the actual exist are counted, so the comparison is like for like. Filter with `from` and `to` (as-of date).

  ```json
  {"storeId": "…", "runs": 2,
   "overall": {"n": 8, "ai": {"mae": …, "rmse": …, "wape": …}, "naive_last_value": {…}, "seasonal_naive_7": {…}},
   "byDemandClass": {"SMOOTH": {…}}, "byDayAhead": {"1": {…}, "7": {…}},
   "note": "Lower error means better on these days, not that it will be better tomorrow."}
  ```

- **`GET /v1/research/export?storeId=&from=&to=`** returns a CSV, one row per forecast day. The columns are `forecast_id, store, sku, demand_class, as_of_date, is_replay, method, model_id, model_version, feature_schema, horizon_days, day_ahead, date, ai, naive_last_value, seasonal_naive_7, actual, ai_abs_error, naive_abs_error, seasonal_abs_error, price_assumption, created_at`.

## Not yet available

Nothing from the design spec's backend list is outstanding. Later ideas: XLSX imports, PDF export, and push notifications when training finishes.
