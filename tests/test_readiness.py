import datetime as dt

from conftest import auth, signup

from app.services.history import readiness_for

D = dt.date(2016, 5, 22)


def test_readiness_rules():
    assert readiness_for(D - dt.timedelta(days=100), D, 21, D).ready
    assert readiness_for(D - dt.timedelta(days=20), D, 21, D).ready  # exactly 21 days
    assert readiness_for(D - dt.timedelta(days=19), D, 20, D).reasons == ["INSUFFICIENT_HISTORY"]
    assert readiness_for(D - dt.timedelta(days=100), D, 18, D).ready  # 3 gaps allowed
    assert readiness_for(D - dt.timedelta(days=100), D, 17, D).reasons == ["TOO_MANY_GAPS"]
    stale = readiness_for(D - dt.timedelta(days=100), D - dt.timedelta(days=2), 19, D)
    assert stale.reasons == ["STALE_HISTORY"]
    assert readiness_for(None, None, 0, D).reasons == ["INSUFFICIENT_HISTORY"]


def test_store_readiness_and_capabilities(client, fake_ml, reference):
    h = auth(signup(client))
    sid = reference["store"]
    ready = client.get(f"/v1/stores/{sid}/readiness", headers=h).json()
    assert (ready["productsReady"], ready["productsTotal"]) == (1, 2)
    assert ready["products"][0]["sku"] == "FOODS_NEW" and ready["products"][0]["reasons"] == ["INSUFFICIENT_HISTORY"]
    caps = client.get(f"/v1/stores/{sid}/models/capabilities", headers=h).json()
    assert caps["horizons"] == [1, 7] and caps["aiAvailable"] is True
    assert caps["asOf"] == {"default": "2016-05-22", "editable": True, "replayMin": "2016-04-24",
                            "replayMax": "2016-05-21", "researchMin": None, "calendarEnd": "2016-06-19"}
    assert caps["activeModel"] == {"id": "m5-ca1-foods", "version": "1.1.0"}
    card = client.get(f"/v1/stores/{sid}/models/active", headers=h).json()
    assert card["model"]["version"] == "1.1.0" and card["card"]["title"] == "Demand model"


def test_history_marks_unrecorded_days(client, fake_ml, reference):
    h = auth(signup(client))
    url = f"/v1/stores/{reference['store']}/products/{reference['short']}/history"
    points = client.get(url, params={"from": "2016-05-10", "to": "2016-05-22"}, headers=h).json()["points"]
    assert [p["units"] for p in points[:3]] == [None, None, None] and points[-1]["units"] == 1.0


def test_new_store_has_no_ai_yet(client, fake_ml):
    h = auth(signup(client))
    ws = client.post("/v1/workspaces", json={"name": "Duka"}, headers=h).json()
    store = client.post(f"/v1/workspaces/{ws['id']}/stores", json={
        "code": "MAIN", "name": "Main", "country": "KE", "timezone": "Africa/Nairobi", "currency": "KES"},
        headers=h).json()
    caps = client.get(f"/v1/stores/{store['id']}/models/capabilities", headers=h).json()
    assert caps["aiAvailable"] is False and caps["aiUnavailableReason"] == "NOT_TRAINED"
    assert caps["methods"] == ["BASELINE"] and caps["asOf"]["editable"] is False
