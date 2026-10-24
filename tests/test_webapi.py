import pytest
from fastapi.testclient import TestClient

from planner import webapi
from test_db import eng, schema_engine  # noqa: F401  (fixtures: migrated + ingested per-test schema)

CSVH = {"x-mode": "csv"}


@pytest.fixture()
def csv_client(monkeypatch):
    monkeypatch.setenv("STORAGE", "csv")
    webapi.bust()
    return TestClient(webapi.app)


def test_offline_plan_matches_engine(csv_client):
    d = csv_client.get("/api/plan").json()
    assert d["mode"] == "csv" and d["writes_enabled"] is False and d["stale"] is None
    r = {x["sku_id"]: x for x in d["recs"]}
    assert r["SKU004"]["recommended_qty"] == 150 and r["SKU007"]["action"] == "REVIEW"
    assert round(r["SKU001"]["cover_weeks"], 1) == 3.4 and "HIGH_DEMAND" in r["SKU001"]["tags"]
    assert set(d["draft_pos"]) == {"DIST001", "DIST002", "DIST004"}
    assert len(d["series"]["SKU001"]) == 16


def test_offline_blocks_writes_and_decisions(csv_client):
    assert csv_client.post("/api/run").status_code == 403
    assert csv_client.get("/api/shadow").status_code == 403
    assert csv_client.post("/api/skus/commit", json={"action": "retire", "sku_id": "SKU004"}).status_code == 403


def test_downloads_scenario_festival(csv_client):
    assert csv_client.get("/api/download/po/DIST002.csv").text.startswith("distributor_id")
    assert "DRAFT" in csv_client.get("/api/download/po/DIST002.csv").text
    s = csv_client.post("/api/scenario", json={"sku": "SKU005", "preset": "stockdrop"}).json()
    assert s["before"]["primary_flag"] == "OK" and s["after"]["primary_flag"] == "STOCKOUT_RISK"
    f = csv_client.post("/api/festival", json={"overrides": {"Snack": 1.0}}).json()
    assert f["above_20"] == ["SKU001"] and abs(f["median_all"] - 0.03) < 0.03


def test_errors_never_leak_connection_strings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:" + "secretpw" + "@nonexistent.invalid/db")
    monkeypatch.delenv("STORAGE", raising=False)
    monkeypatch.setattr(webapi, "_engine", None)
    monkeypatch.setattr(webapi, "_awake", False)
    monkeypatch.setattr("planner.db.time.sleep", lambda s: None)
    webapi.bust()
    r = TestClient(webapi.app).get("/api/plan")
    assert r.status_code == 503 and r.json()["waking"] is True
    assert "secretpw" not in r.text


def test_db_flow_end_to_end(monkeypatch, eng):  # noqa: F811
    monkeypatch.setenv("DATABASE_URL", "postgresql://dummy/dummy")
    monkeypatch.setenv("APP_PASSCODE", "pw")
    monkeypatch.delenv("STORAGE", raising=False)
    monkeypatch.setattr(webapi, "_engine", eng)
    monkeypatch.setattr(webapi, "_awake", True)
    webapi.bust()
    c = TestClient(webapi.app)
    P = {"x-passcode": "pw"}
    plan = c.get("/api/plan").json()
    assert plan["mode"] == "db" and plan["writes_enabled"] and plan["stale"] is True and plan["latest_run"] is None
    assert c.post("/api/run").status_code == 401
    assert c.post("/api/run", headers={"x-passcode": "bad"}).status_code == 401
    rid = c.post("/api/run", headers=P).json()["run_id"]
    webapi.bust()
    plan = c.get("/api/plan").json()
    assert plan["stale"] is False and plan["latest_run"]["run_id"] == rid

    d = {"run_id": rid, "sku_id": "SKU003", "decision": "Reject", "reason": "  ", "by": "raghav"}
    assert c.post("/api/decisions", headers=P, json=d).status_code == 422
    d["reason"] = "Supplier on allocation"
    assert c.post("/api/decisions", headers=P, json=d).status_code == 200
    assert c.post("/api/decisions", headers=P, json={**d, "sku_id": "SKU999"}).status_code == 404
    sh = c.get("/api/shadow").json()
    assert sh["total"] == 1 and sh["agreement"] == 0.0 and sh["log"][0]["tool_flag"] == "STOCKOUT_RISK"

    body = {"action": "add", "sku_id": "SKU900", "by": "tester", "onhand": {"units": 50, "date": "2026-10-19"},
            "fields": {"sku_name": "Test", "category": "Snack", "perishability": "Low", "standard_reorder_qty": 100, "distributor_id": "DIST001"},
            "sales_csv": "SKU_ID,Week,Units_Sold\nSKU900,2026-W41,10\nSKU900,2026-W42,12"}
    pv = c.post("/api/skus/preview", headers=P, json=body).json()
    assert pv["diff"]["sales_rows_added"] == 2
    assert c.post("/api/skus/commit", headers=P, json=body).status_code == 200
    assert c.post("/api/skus/commit", headers=P, json=body).status_code == 422  # duplicate id refused
    webapi.bust()
    plan = c.get("/api/plan").json()
    assert plan["stale"] is True  # master data changed since the saved run
    sku900 = [r for r in plan["recs"] if r["sku_id"] == "SKU900"][0]
    assert sku900["action"] == "REVIEW"
    assert c.post("/api/skus/undo", headers=P, json={"by": "tester"}).status_code == 200
    webapi.bust()
    plan = c.get("/api/plan").json()
    assert plan["stale"] is False and all(r["sku_id"] != "SKU900" for r in plan["recs"])
    assert c.post("/api/skus/commit", headers=P, json={"action": "add", "sku_id": "SKU901", "by": "t",
        "onhand": {"units": 5, "date": "2026-10-19"}, "fields": {**body["fields"], "distributor_id": "DIST999"}}).status_code == 422
    assert len(c.get("/api/skus/changes").json()) == 2
    cfgr = c.post("/api/config", headers=P, json={"changes": {"festival_uplift_default": 1.1}, "note": ""})
    assert cfgr.status_code == 400
    assert c.post("/api/config", headers=P, json={"changes": {"festival_uplift_default": 1.1}, "note": "test"}).status_code == 200
    webapi.bust()
    assert c.get("/api/plan").json()["stale"] is True


def test_approved_po_download_uses_final_quantities(monkeypatch, eng):  # noqa: F811
    import io
    import pandas as pd
    monkeypatch.setenv("DATABASE_URL", "postgresql://dummy/dummy")
    monkeypatch.setenv("APP_PASSCODE", "pw")
    monkeypatch.delenv("STORAGE", raising=False)
    monkeypatch.setattr(webapi, "_engine", eng)
    monkeypatch.setattr(webapi, "_awake", True)
    webapi.bust()
    c, P = TestClient(webapi.app), {"x-passcode": "pw"}
    assert c.get("/api/download/approved/DIST002.csv").status_code == 409  # no saved run yet
    rid = c.post("/api/run", headers=P).json()["run_id"]
    webapi.bust()
    assert c.get("/api/download/approved/DIST002.csv").status_code == 404  # nothing approved yet
    dec = lambda sku, d, **k: c.post("/api/decisions", headers=P, json={"run_id": rid, "sku_id": sku, "decision": d, "by": "raghav", **k})  # noqa: E731
    assert dec("SKU004", "Change quantity", qty=300, reason="Supplier MOQ").status_code == 200
    assert dec("SKU003", "Hold", reason="Waiting on quote").status_code == 200
    webapi.bust()
    r = c.get("/api/download/approved/DIST002.csv")
    assert r.status_code == 200 and 'filename="DIST002_approved.csv"' in r.headers["content-disposition"]
    df = pd.read_csv(io.StringIO(r.text))
    assert list(df["sku_id"]) == ["SKU004"]  # held line and undecided lines are excluded
    row = df.iloc[0]
    assert row["draft_quantity"] == 150 and row["approved_quantity"] == 300 and row["decided_by"] == "raghav" and "never places orders" in row["status"]
    assert dec("SKU003", "Accept").status_code == 200  # the latest decision wins
    webapi.bust()
    assert set(pd.read_csv(io.StringIO(c.get("/api/download/approved/DIST002.csv").text))["sku_id"]) == {"SKU004", "SKU003"}
    assert c.get("/api/download/approved/NOPE.csv").status_code == 404
    c.post("/api/skus/commit", headers=P, json={"action": "retire", "sku_id": "SKU001", "by": "t"})
    webapi.bust()
    assert c.get("/api/download/approved/DIST002.csv").status_code == 409  # data changed since the run


def test_approved_po_blocked_offline(csv_client):
    assert csv_client.get("/api/download/approved/DIST002.csv").status_code == 403


def test_homepage_is_the_landing_and_dashboard_lives_under_dashboard(csv_client):
    home = csv_client.get("/")
    assert home.status_code == 200 and "<title>Freshroots Demand Planning</title>" in home.text and 'id="root"' in home.text
    dash = csv_client.get("/dashboard/")
    assert dash.status_code == 200 and 'id="nav"' in dash.text and "/dashboard/app.js" in dash.text
    for asset, ctype in (("/dashboard/app.js", "javascript"), ("/dashboard/style.css", "css"), ("/favicon.svg", "svg")):
        r = csv_client.get(asset)
        assert r.status_code == 200 and ctype in r.headers["content-type"], asset
    # every file the landing page references must exist
    import re
    for ref in re.findall(r'(?:src|href)="(/assets/[^"]+)"', home.text):
        assert csv_client.get(ref).status_code == 200, ref
    assert csv_client.get("/api/health").json() == {"ok": True}  # API routes still win over the static catch-all


@pytest.mark.parametrize("path", ["/../src/planner/db.py", "/..%2fsrc%2fplanner%2fdb.py", "/dashboard/../../.env", "/%2e%2e/.env", "/nope.js", "/dashboard/missing.css"])
def test_static_server_cannot_escape_public(csv_client, path):
    r = csv_client.get(path)
    assert r.status_code == 404 and "DATABASE_URL" not in r.text and "def " not in r.text
