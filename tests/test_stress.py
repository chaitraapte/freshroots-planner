"""Regression tests for bugs found by the UI/API stress test."""
from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from planner import store, webapi
from planner.config import merged, validate_config
from planner.csvio import load_csv_dir
from planner.engine import GateStop, run_plan
from test_db import eng, schema_engine  # noqa: F401

P = {"x-passcode": "pw"}
NEWSKU = {"sku_name": "T", "category": "Snack", "perishability": "Low", "standard_reorder_qty": 10, "distributor_id": "DIST001"}


@pytest.fixture()
def csvc(monkeypatch):
    monkeypatch.setenv("STORAGE", "csv")
    webapi.bust()
    return TestClient(webapi.app)


@pytest.fixture()
def dbc(monkeypatch, eng):  # noqa: F811
    monkeypatch.setenv("DATABASE_URL", "postgresql://dummy/dummy")
    monkeypatch.setenv("APP_PASSCODE", "pw")
    monkeypatch.delenv("STORAGE", raising=False)
    monkeypatch.setattr(webapi, "_engine", eng)
    monkeypatch.setattr(webapi, "_awake", True)
    webapi.bust()
    return TestClient(webapi.app), eng


# ---- engine / gate
def test_impossible_iso_week_stops_the_gate_not_the_app():
    d = load_csv_dir("data")
    d["weekly_sales"] = pd.concat([d["weekly_sales"], pd.DataFrame([{"SKU_ID": "SKU002", "Week": "2026-W99", "Units_Sold": 5}])])
    with pytest.raises(GateStop) as e:
        run_plan(d, date(2026, 10, 19))
    assert "invalid_week" in {f.code for f in e.value.gate.stops}


def test_week_still_in_progress_is_not_counted_as_demand():
    d = load_csv_dir("data")
    base = run_plan(d, date(2026, 10, 19)).recs.set_index("sku_id").loc["SKU002", "avg12"]
    d["weekly_sales"] = pd.concat([d["weekly_sales"], pd.DataFrame([{"SKU_ID": "SKU002", "Week": "2026-W43", "Units_Sold": 1}])])
    assert run_plan(d, date(2026, 10, 19)).recs.set_index("sku_id").loc["SKU002", "avg12"] == base
    assert run_plan(d, date(2026, 10, 26)).recs.set_index("sku_id").loc["SKU002", "avg12"] != base  # once the week has ended it counts


def test_config_validation():
    assert validate_config(merged()) == []
    for bad in ({"festival_uplift_default": -1}, {"festival_uplift_default": 99}, {"festival_uplift_default": None}, {"stockout_weeks": 0},
                {"lead_time_weeks": {"Low": "x", "High": 1}}, {"lead_time_weeks": {"Low": -2, "High": 1}}, {"stockout_weeks": 20, "overstock_weeks": 10},
                {"target_cover_weeks": 99}, {"high_demand_trend": float("nan")}):
        assert validate_config(merged(bad)), bad


# ---- scenario + festival
@pytest.mark.parametrize("custom", [{"last_n": 0}, {"last_n": -1}, {"last_n": 2.5}, {"last_n": 999}, {"multiplier": -1}, {"multiplier": ""}, {"multiplier": 99},
                                    {"on_hand": -5}, {"on_hand": 1.5}, {"lead_time": 99}, {"lead_time": -1}, {"multiplier": "abc"}])
def test_scenario_rejects_bad_numbers(csvc, custom):
    r = csvc.post("/api/scenario", json={"sku": "SKU011", "custom": custom})
    assert r.status_code == 422 and "detail" in r.json()


def test_scenario_accepts_edges(csvc):
    for custom in ({"last_n": 1, "multiplier": 0}, {"last_n": 52, "multiplier": 10}, {"on_hand": 0}, {"lead_time": 0}):
        assert csvc.post("/api/scenario", json={"sku": "SKU011", "custom": custom}).status_code == 200


@pytest.mark.parametrize("v", [0, 0.2, 5, -1, "", "abc", None])
def test_festival_rejects_bad_uplift(csvc, v):
    assert csvc.post("/api/festival", json={"overrides": {"Snack": v}}).status_code == 422


def test_server_errors_never_leak_sql(monkeypatch, csvc):
    monkeypatch.setattr(webapi, "run_plan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("SELECT secret FROM x -- postgresql://u:p@h/db")))
    c = TestClient(webapi.app, raise_server_exceptions=False)
    r = c.get("/api/plan")
    assert r.status_code == 500 and "SELECT" not in r.text and "postgresql" not in r.text


# ---- DB-backed
def test_bad_saved_config_cannot_brick_the_dashboard(dbc):
    c, e = dbc
    with e.begin() as conn:
        store.save_config_version(conn, {**merged(), "lead_time_weeks": {"Low": "x", "High": 1}}, "poison")
    webapi.bust()
    r = c.get("/api/plan")
    assert r.status_code == 200 and r.json()["config_problems"]
    # saving invalid settings through the API is refused, and reset-to-defaults fixes it
    assert c.post("/api/config", headers=P, json={"changes": {"festival_uplift_default": 99}, "note": "x"}).status_code == 422
    assert c.post("/api/config", headers=P, json={"changes": {"lead_time_weeks": {"Low": "x"}}, "note": "x"}).status_code == 422
    assert c.post("/api/config", headers=P, json={"reset": True, "note": "reset"}).status_code == 200
    webapi.bust()
    assert c.get("/api/plan").json()["config_problems"] == []


def test_auth_endpoint(dbc):
    c, _ = dbc
    assert c.post("/api/auth").status_code == 401
    assert c.post("/api/auth", headers={"x-passcode": "no"}).status_code == 401
    assert c.post("/api/auth", headers=P).status_code == 200


BAD_PREVIEWS = [
    ({"action": "explode", "sku_id": "SKU005"}, "Unknown action"),
    ({"action": "add", "sku_id": " sku 700 ", "fields": NEWSKU, "onhand": {"units": 5, "date": "2026-10-19"}}, "no spaces"),
    ({"action": "add", "sku_id": "S" * 40, "fields": NEWSKU, "onhand": {"units": 5, "date": "2026-10-19"}}, "1 to 32"),
    ({"action": "add", "sku_id": "SKU700", "fields": {**NEWSKU, "sku_name": "  "}, "onhand": {"units": 5, "date": "2026-10-19"}}, "required"),
    ({"action": "add", "sku_id": "SKU700", "fields": NEWSKU, "onhand": {"units": 5, "date": ""}}, "valid as-of date"),
    ({"action": "add", "sku_id": "SKU700", "fields": NEWSKU, "onhand": {"units": 5, "date": "not-a-date"}}, "valid as-of date"),
    ({"action": "add", "sku_id": "SKU700", "fields": NEWSKU, "onhand": {"units": 12.5, "date": "2026-10-19"}}, "whole number"),
    ({"action": "add", "sku_id": "SKU700", "fields": NEWSKU, "onhand": {"units": -1, "date": "2026-10-19"}}, "whole number"),
    ({"action": "add", "sku_id": "SKU700", "fields": NEWSKU, "onhand": {"units": 10**12, "date": "2026-10-19"}}, "whole number"),
    ({"action": "edit", "sku_id": "SKU005", "fields": {"standard_reorder_qty": 10**11}}, "quantity"),
    ({"action": "edit", "sku_id": "SKU005", "fields": {"sku_name": "   "}}, "blank"),
    ({"action": "edit", "sku_id": "SKU005", "fields": {"sku_name": "x" * 500}}, "too long"),
    ({"action": "append_sales", "sku_id": "SKU002", "sales_csv": "SKU_ID,Week,Units_Sold\nSKU002,2026-W99,5"}, "real ISO week"),
    ({"action": "append_sales", "sku_id": "SKU002", "sales_csv": "SKU_ID,Week,Units_Sold\nSKU002,2026-W00,5"}, "real ISO week"),
    ({"action": "append_sales", "sku_id": "SKU002", "sales_csv": "SKU_ID,Week,Units_Sold\nSKU002,2026-W43,5.5"}, "whole numbers"),
]


@pytest.mark.parametrize("body,expect", BAD_PREVIEWS)
def test_sku_manager_rejects_bad_input_before_writing(dbc, body, expect):
    c, e = dbc
    with e.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM sku_changes")).scalar()
    r = c.post("/api/skus/preview", headers=P, json={**body, "by": "t"})
    assert r.status_code == 422 and expect.lower() in r.json()["detail"].lower(), r.text
    r = c.post("/api/skus/commit", headers=P, json={**body, "by": "t"})
    assert r.status_code == 422
    with e.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM sku_changes")).scalar() == before


def test_bulk_empty_and_garbage(dbc):
    c, _ = dbc
    for csv in ("", "   ", "\x00\x01 not csv"):
        assert c.post("/api/skus/bulk-preview", headers=P, json={"csv": csv}).status_code == 422


def test_sales_units_stored_as_whole_numbers(dbc):
    c, e = dbc
    r = c.post("/api/skus/commit", headers=P, json={"action": "append_sales", "sku_id": "SKU002", "by": "t", "sales_csv": "SKU_ID,Week,Units_Sold\nSKU002,2026-W43,88"})
    assert r.status_code == 200
    with e.connect() as conn:
        assert conn.execute(text("SELECT units_sold FROM weekly_sales WHERE sku_id='SKU002' AND iso_week='2026-W43'")).scalar() == 88
