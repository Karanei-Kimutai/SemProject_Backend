import datetime as dt

import pytest
from conftest import auth, signup

from app import worker
from app.models import Membership, Role, SalesDaily, User

TODAY = dt.date.today()
MAPPING = {"salesDate": "sales_date", "productId": "product_id", "productName": "product_name",
           "category": "category", "unitsSold": "units_sold", "unitPrice": "unit_price"}
HEADER = "sales_date,product_id,product_name,category,units_sold,unit_price"


@pytest.fixture
def shop(client):
    h = auth(signup(client, "owner@example.com"))
    ws = client.post("/v1/workspaces", json={"name": "Duka"}, headers=h).json()
    store = client.post(f"/v1/workspaces/{ws['id']}/stores", json={
        "code": "NAIROBI-01", "name": "Nairobi", "country": "KE", "timezone": "Africa/Nairobi", "currency": "KES"},
        headers=h).json()
    return {"h": h, "ws": ws["id"], "store": store["id"]}


def days_ago(n: int) -> str:
    return (TODAY - dt.timedelta(days=n)).isoformat()


def csv_file(lines: list[str], header: str = HEADER) -> bytes:
    return ("\n".join([header, *lines]) + "\n").encode()


def upload(client, shop, content: bytes, key: str | None = None, name: str = "sales.csv"):
    headers = {**shop["h"], **({"Idempotency-Key": key} if key else {})}
    return client.post(f"/v1/stores/{shop['store']}/imports", files={"file": (name, content, "text/csv")},
                       headers=headers)


def import_file(client, shop, content: bytes, zero: bool = False, policy: str = "SKIP", mapping=MAPPING, **extra):
    up = upload(client, shop, content)
    assert up.status_code == 201, up.text
    mapped = client.put(f"/v1/imports/{up.json()['id']}/mapping",
                        json={"columns": mapping, "missingDaysAreZero": zero, **extra}, headers=shop["h"])
    assert mapped.status_code == 200, mapped.text
    return mapped.json()


def confirm(client, shop, import_id, policy="SKIP"):
    response = client.post(f"/v1/imports/{import_id}/confirm", json={"overlapPolicy": policy}, headers=shop["h"])
    assert response.status_code == 200, response.text
    return response.json()


def regular_rows(days: int, skus=("WATER-500ML", "BREAD-400G"), start_ago: int = 1, units=lambda k: 5 + k % 3):
    lines = []
    for sku in skus:
        for k in range(days):
            lines.append(f"{days_ago(start_ago + k)},{sku},{sku.title()},Food,{units(k)},50")
    return lines


def test_upload_detects_columns_and_preview(client, shop):
    response = upload(client, shop, csv_file(regular_rows(3)))
    body = response.json()
    assert body["status"] == "UPLOADED" and body["columns"] == HEADER.split(",")
    assert body["detected"]["suggestedMapping"]["salesDate"] == {"column": "sales_date", "confidence": "HIGH"}
    assert body["detected"]["suggestedMapping"]["unitsSold"]["column"] == "units_sold"
    assert body["detected"]["suggestedDateFormat"] == "YYYY-MM-DD"
    assert len(body["detected"]["preview"]) == 5


def test_happy_path_import(client, fake_ml, shop):
    result = import_file(client, shop, csv_file(regular_rows(30)))
    assert result["status"] == "READY"
    summary = result["summary"]
    assert (summary["rowsRead"], summary["rowsAccepted"], summary["rowsRejected"]) == (60, 60, 0)
    assert summary["products"] == 2 and summary["newProducts"] == 2 and summary["overlappingDays"] == 0
    assert summary["dateRange"] == {"from": days_ago(30), "to": days_ago(1)}
    preview = client.get(f"/v1/imports/{result['id']}/validation", headers=shop["h"]).json()
    assert preview["preview"][0]["unitsSold"] in (5.0, 6.0, 7.0)

    done = confirm(client, shop, result["id"])
    assert done["status"] == "IMPORTED"
    assert done["result"]["recordsInserted"] == 60 and done["result"]["productsCreated"] == 2
    assert done["result"]["demandClassesUpdated"] == 2

    summary = client.get(f"/v1/stores/{shop['store']}/history/summary", headers=shop["h"]).json()
    assert summary["records"] == 60
    assert summary["historyEnd"] == days_ago(1) and summary["productsWithData"] == 2
    products = client.get(f"/v1/stores/{shop['store']}/products", headers=shop["h"]).json()["items"]
    assert [(p["sku"], p["category"], p["demandClass"]["class"]) for p in products] == [
        ("BREAD-400G", "Food", "INTERMITTENT"), ("WATER-500ML", "Food", "INTERMITTENT")]
    ready = client.get(f"/v1/stores/{shop['store']}/readiness", headers=shop["h"]).json()
    assert ready["productsReady"] == 2 and ready["stale"] is False

    # No own-data model yet: only baselines, and they work on the imported history.
    caps = client.get(f"/v1/stores/{shop['store']}/models/capabilities", headers=shop["h"]).json()
    assert caps["methods"] == ["BASELINE"]
    job = client.post("/v1/forecasts", json={"storeId": shop["store"], "productId": products[0]["id"],
                                             "horizonDays": 7, "method": "BASELINE"}, headers=shop["h"])
    assert job.status_code == 202, job.text
    assert worker.run_once()
    forecast = client.get(f"/v1/forecasts/{job.json()['forecastId']}", headers=shop["h"]).json()
    assert forecast["status"] == "COMPLETED" and forecast["points"][0]["date"] == TODAY.isoformat()
    assert len(fake_ml.requests[-1]["series"][0]["history"]) == 30
    ai = client.post("/v1/forecasts", json={"storeId": shop["store"], "productId": products[0]["id"],
                                            "horizonDays": 1, "method": "AI"}, headers=shop["h"])
    assert ai.json()["error"]["code"] == "AI_NOT_AVAILABLE"


def test_validation_rules(client, fake_ml, shop):
    good = regular_rows(28, skus=("WATER-500ML",), start_ago=3)
    lines = good + [
        f"{days_ago(1)},WATER-500ML,Water,Food,200,50",        # spike: > 10x the usual ~6
        f"{days_ago(2)},WATER-500ML,Water,Food,3,50",          # duplicate day, summed with the next line
        f"{days_ago(2)},WATER-500ML,Water,Food,4,0",           # price 0 → missing (warning)
        "31/02/2026,WATER-500ML,Water,Food,3,50",              # bad date
        f"{days_ago(1)},MILK,Milk,Dairy,-2,60",                # negative units
        f"{days_ago(1)},MILK,Milk,Dairy,,60",                  # missing units
        f"{days_ago(1)},MILK,Milk,Dairy,abc,60",               # non-numeric units
        f"{(TODAY + dt.timedelta(days=3)).isoformat()},MILK,Milk,Dairy,1,60",  # future date
        f"{days_ago(1)},,,Dairy,1,60",                         # no product
        f"{days_ago(1)},EGGS,Eggs,Dairy,2,cheap",              # bad price (warning)
    ]
    result = import_file(client, shop, csv_file(lines))
    s = result["summary"]
    assert result["status"] == "NEEDS_ATTENTION"
    assert s["errorsByCode"] == {"INVALID_DATE": 1, "NEGATIVE_UNITS": 1, "MISSING_UNITS": 1, "INVALID_UNITS": 1,
                                 "FUTURE_DATE": 1, "MISSING_PRODUCT": 1}
    assert s["warningsByCode"] == {"SPIKE": 1, "DUPLICATE_ROWS_SUMMED": 1, "NON_POSITIVE_PRICE": 1,
                                   "INVALID_PRICE": 1}
    assert s["rowsRejected"] == 6 and s["rowsAccepted"] == s["rowsRead"] - 6 and s["rowsSummedAsDuplicates"] == 2
    errors = client.get(f"/v1/imports/{result['id']}/errors", params={"severity": "ERROR"}, headers=shop["h"]).json()
    assert errors["total"] == 6
    bad_date = next(e for e in errors["items"] if e["code"] == "INVALID_DATE")
    assert bad_date["rowNumber"] == 33 and bad_date["column"] == "sales_date" and "31/02/2026" in bad_date["message"]
    done = confirm(client, shop, result["id"])
    assert done["result"]["recordsInserted"] == 28 + 3  # 28 regular days, the spike day, the summed day, EGGS
    history = client.get(f"/v1/stores/{shop['store']}/products", params={"q": "WATER"}, headers=shop["h"]).json()
    pid = history["items"][0]["id"]
    points = client.get(f"/v1/stores/{shop['store']}/products/{pid}/history",
                        params={"from": days_ago(2), "to": days_ago(1)}, headers=shop["h"]).json()["points"]
    assert [p["units"] for p in points] == [7.0, 200.0]


def test_missing_days_zero_or_not_recorded(client, fake_ml, shop):
    lines = [f"{days_ago(10)},A,A,X,4,10", f"{days_ago(5)},A,A,X,2,10", f"{days_ago(1)},B,B,X,1,10"]
    as_missing = import_file(client, shop, csv_file(lines), zero=False)
    assert as_missing["summary"]["zeroFilledDays"] == 0 and as_missing["summary"]["dailyRecords"] == 3
    as_zero = client.put(f"/v1/imports/{as_missing['id']}/mapping",
                         json={"columns": MAPPING, "missingDaysAreZero": True}, headers=shop["h"]).json()
    # A: days 9..6 and 4..1 become 0 (8 days); B starts on the last day, nothing to fill.
    assert as_zero["summary"]["zeroFilledDays"] == 8 and as_zero["summary"]["dailyRecords"] == 11
    confirm(client, shop, as_zero["id"])
    pid = client.get(f"/v1/stores/{shop['store']}/products", params={"q": "A"}, headers=shop["h"]).json()["items"][0]["id"]
    points = client.get(f"/v1/stores/{shop['store']}/products/{pid}/history",
                        params={"from": days_ago(10), "to": days_ago(1)}, headers=shop["h"]).json()["points"]
    assert [p["units"] for p in points] == [4, 0, 0, 0, 0, 2, 0, 0, 0, 0]
    assert points[1]["price"] == 10.0  # zero-filled days carry the last known price


def test_store_closed_days_are_never_zero_filled(client, fake_ml, shop):
    header = HEADER + ",store_closed"
    lines = [f"{days_ago(4)},A,A,X,3,10,", f"{days_ago(3)},A,A,X,,,yes", f"{days_ago(1)},A,A,X,5,10,no"]
    result = import_file(client, shop, csv_file(lines, header), zero=True,
                         mapping={**MAPPING, "storeClosed": "store_closed"})
    assert result["summary"]["closedDays"] == 1 and result["summary"]["zeroFilledDays"] == 1  # only day 2
    assert result["status"] == "READY"


def test_overlap_skip_and_replace(client, fake_ml, shop):
    confirm(client, shop, import_file(client, shop, csv_file([f"{days_ago(2)},A,A,X,3,10",
                                                              f"{days_ago(1)},A,A,X,4,10"]))["id"])
    second = import_file(client, shop, csv_file([f"{days_ago(1)},A,A,X,9,11", f"{days_ago(0)},A,A,X,1,11"]))
    assert second["summary"]["overlappingDays"] == 1
    skipped = confirm(client, shop, second["id"], "SKIP")["result"]
    assert (skipped["recordsInserted"], skipped["overlapsSkipped"]) == (1, 1)
    third = import_file(client, shop, csv_file([f"{days_ago(1)},A,A,X,9,11"]))
    replaced = confirm(client, shop, third["id"], "REPLACE")["result"]
    assert (replaced["recordsInserted"], replaced["recordsReplaced"]) == (1, 1)
    pid = client.get(f"/v1/stores/{shop['store']}/products", headers=shop["h"]).json()["items"][0]["id"]
    points = client.get(f"/v1/stores/{shop['store']}/products/{pid}/history",
                        params={"from": days_ago(2), "to": days_ago(0)}, headers=shop["h"]).json()["points"]
    assert [p["units"] for p in points] == [3, 9, 1]
    again = client.post(f"/v1/imports/{third['id']}/confirm", json={}, headers=shop["h"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "IMPORT_NOT_READY"


def test_delete_import_removes_its_rows(client, db, fake_ml, shop):
    first = confirm(client, shop, import_file(client, shop, csv_file(regular_rows(5, skus=("A",), start_ago=6)))["id"])
    second = confirm(client, shop, import_file(client, shop, csv_file(regular_rows(5, skus=("A",), start_ago=1)))["id"])
    store = client.get(f"/v1/stores/{shop['store']}", headers=shop["h"]).json()
    assert store["historyEnd"] == days_ago(1)
    deleted = client.delete(f"/v1/imports/{second['id']}", headers=shop["h"]).json()
    assert deleted["status"] == "DELETED" and deleted["result"]["recordsDeleted"] == 5
    store = client.get(f"/v1/stores/{shop['store']}", headers=shop["h"]).json()
    assert (store["historyStart"], store["historyEnd"]) == (days_ago(10), days_ago(6))
    assert db.query(SalesDaily).count() == 5
    listing = client.get(f"/v1/stores/{shop['store']}/imports", headers=shop["h"]).json()
    assert [i["id"] for i in listing["items"]] == [first["id"]]
    assert client.get(f"/v1/imports/{second['id']}", headers=shop["h"]).status_code == 404


def test_idempotency_key(client, shop):
    content = csv_file(regular_rows(2))
    first = upload(client, shop, content, key="abc-1")
    again = upload(client, shop, content, key="abc-1")
    assert again.json()["id"] == first.json()["id"]
    other = upload(client, shop, csv_file(regular_rows(3)), key="abc-1")
    assert other.status_code == 409 and other.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_date_formats(client, fake_ml, shop):
    ambiguous = csv_file(["01/02/2026,A,A,X,1,10", "03/04/2026,A,A,X,1,10"])
    up = upload(client, shop, ambiguous).json()
    assert up["detected"]["dateFormatAmbiguous"] is True and up["detected"]["suggestedDateFormat"] is None
    needs = client.put(f"/v1/imports/{up['id']}/mapping", json={"columns": MAPPING, "missingDaysAreZero": False},
                       headers=shop["h"])
    assert needs.json()["error"]["code"] == "DATE_FORMAT_REQUIRED"
    assert set(needs.json()["error"]["details"]["options"]) == {"DD/MM/YYYY", "MM/DD/YYYY"}
    done = client.put(f"/v1/imports/{up['id']}/mapping", json={"columns": MAPPING, "missingDaysAreZero": False,
                                                                "dateFormat": "DD/MM/YYYY"}, headers=shop["h"]).json()
    assert done["summary"]["dateRange"] == {"from": "2026-02-01", "to": "2026-04-03"}
    day_first = upload(client, shop, csv_file(["25/12/2025,A,A,X,1,10"])).json()
    assert day_first["detected"]["suggestedDateFormat"] == "DD/MM/YYYY"


def test_bad_files_and_mappings(client, shop):
    assert upload(client, shop, b"").json()["error"]["code"] == "EMPTY_FILE"
    assert upload(client, shop, "date,qty\n2026-01-01,\xff".encode("latin-1")).json()["error"]["code"] == "FILE_NOT_UTF8"
    assert upload(client, shop, b"a,a\n1,2\n").json()["error"]["code"] == "BAD_HEADER"
    big = b"x" * (10 * 1024 * 1024 + 1)
    assert upload(client, shop, big).status_code == 413
    up = upload(client, shop, csv_file(regular_rows(2))).json()
    missing = client.put(f"/v1/imports/{up['id']}/mapping",
                         json={"columns": {"salesDate": "sales_date"}, "missingDaysAreZero": False}, headers=shop["h"])
    assert missing.json()["error"]["code"] == "REQUIRED_FIELDS_UNMAPPED"
    unknown = client.put(f"/v1/imports/{up['id']}/mapping",
                         json={"columns": {**MAPPING, "unitsSold": "nope"}, "missingDaysAreZero": False},
                         headers=shop["h"])
    assert unknown.json()["error"]["code"] == "UNKNOWN_COLUMN"
    no_answer = client.put(f"/v1/imports/{up['id']}/mapping", json={"columns": MAPPING}, headers=shop["h"])
    assert no_answer.json()["error"]["code"] == "INVALID_REQUEST"


def test_semicolon_csv_and_names_only(client, fake_ml, shop):
    content = f"Date;Item Name;Qty\n{days_ago(2)};Sugar 1kg;3\n{days_ago(1)};Sugar 1kg;2\n".encode()
    up = upload(client, shop, content).json()
    suggestion = {k: v["column"] for k, v in up["detected"]["suggestedMapping"].items()}
    assert suggestion == {"salesDate": "Date", "productName": "Item Name", "unitsSold": "Qty"}
    result = client.put(f"/v1/imports/{up['id']}/mapping", json={"columns": suggestion, "missingDaysAreZero": False},
                        headers=shop["h"]).json()
    assert result["status"] == "READY"
    confirm(client, shop, result["id"])
    product = client.get(f"/v1/stores/{shop['store']}/products", headers=shop["h"]).json()["items"][0]
    assert (product["sku"], product["name"]) == ("SUGAR-1KG", "Sugar 1kg")


def test_multi_store_file(client, fake_ml, shop):
    other = client.post(f"/v1/workspaces/{shop['ws']}/stores", json={
        "code": "MOMBASA-01", "name": "Mombasa", "country": "KE", "timezone": "Africa/Nairobi", "currency": "KES"},
        headers=shop["h"]).json()
    header = HEADER + ",store_id"
    lines = [f"{days_ago(1)},A,A,X,1,10,NAIROBI-01", f"{days_ago(1)},A,A,X,2,10,MOMBASA-01",
             f"{days_ago(1)},A,A,X,2,10,KISUMU-01"]
    result = import_file(client, shop, csv_file(lines, header), mapping={**MAPPING, "storeId": "store_id"})
    assert result["summary"]["stores"] == ["MOMBASA-01", "NAIROBI-01"]
    assert result["summary"]["errorsByCode"] == {"UNKNOWN_STORE": 1}
    confirm(client, shop, result["id"])
    assert client.get(f"/v1/stores/{other['id']}", headers=shop["h"]).json()["historyEnd"] == days_ago(1)


def test_permissions(client, db, fake_ml, shop, reference):
    viewer_tokens = signup(client, "viewer@example.com")
    viewer = db.query(User).filter_by(email="viewer@example.com").one()
    db.add(Membership(workspace_id=shop["ws"], user_id=viewer.id, role=Role.VIEWER))
    db.commit()
    h = auth(viewer_tokens)
    content = csv_file(regular_rows(2))
    files = {"file": ("s.csv", content, "text/csv")}
    assert client.post(f"/v1/stores/{shop['store']}/imports", files=files, headers=h).status_code == 403
    demo = client.post(f"/v1/stores/{reference['store']}/imports", files=files, headers=shop["h"])
    assert demo.status_code == 403
    up = upload(client, shop, content).json()
    assert client.get(f"/v1/imports/{up['id']}", headers=h).status_code == 200
    assert client.delete(f"/v1/imports/{up['id']}", headers=h).status_code == 403
    stranger = auth(signup(client, "stranger@example.com"))
    assert client.get(f"/v1/imports/{up['id']}", headers=stranger).status_code == 404


def test_actuals_attach_after_import(client, fake_ml, shop):
    confirm(client, shop, import_file(client, shop, csv_file(regular_rows(30, skus=("A",), start_ago=2)))["id"])
    pid = client.get(f"/v1/stores/{shop['store']}/products", headers=shop["h"]).json()["items"][0]["id"]
    job = client.post("/v1/forecasts", json={"storeId": shop["store"], "productId": pid, "horizonDays": 1,
                                             "method": "BASELINE"}, headers=shop["h"]).json()
    assert worker.run_once()
    confirm(client, shop, import_file(client, shop, csv_file([f"{days_ago(1)},A,A,X,6,50"]))["id"])
    forecast = client.get(f"/v1/forecasts/{job['forecastId']}", headers=shop["h"]).json()
    assert forecast["points"][0]["actual"] == 6.0 and forecast["runMetrics"]["baseline"]["mae"] == 4.0
