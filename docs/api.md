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
| `NOT_RETRYABLE` | 409 | Retry on a job that hasn't failed |

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

- `points[].baseline` is the main baseline ("Same as last day"); `seasonalNaive` is the second one.
- `ai` is `null` when `method` is `BASELINE` or `aiUnavailableReason` is set.
- `actual` is non-null only on replays, or once actual sales arrive later.
- `history` holds the last 28 days before the as-of date, for the chart.
- `runMetrics` is present only when actuals exist. Show it as "Error on these days".
- `productBacktest` (demo only) is the 28-day historical backtest for this product. Always show AI and baseline from the same block, with the small-sample note.
- `priceAssumption`: `KNOWN` (prices known for every forecast day), `LAST_KNOWN` (last price carried forward), `NONE` or `NOT_USED`.
- Values are expected units, with decimals. Show one decimal place by default (see spec §8.10).
- While the job hasn't completed, the response has only the header fields and `error`.

### History, export, delete

- `GET /v1/forecasts?storeId=&productId=&replay=&status=&page=&pageSize=` returns `{page, pageSize, total, items: [{forecastId, status, storeId, productId, sku, productName, asOfDate, horizonDays, method, isReplay, modelVersion, createdAt, actualsAvailable}]}`, newest first. `actualsAvailable: true` means a past true forecast now has actual sales: tag it "Actuals now available".
- `GET /v1/forecasts/{id}/export?format=csv` returns a CSV file.
- `DELETE /v1/forecasts/{id}` → `204` (soft delete). You can delete your own forecasts; owners and managers can delete any in their workspace.

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

## Not yet available

Own-data training (`/v1/stores/{sid}/models/train`), team invitations and the research export are planned for slices 3 and 4. Until then, a business store gets baseline forecasts only (`capabilities.methods == ["BASELINE"]`).
