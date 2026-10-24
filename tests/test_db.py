import uuid
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

from planner import db, store
from planner.csvio import load_csv_dir
from planner.engine import run_plan
from planner.store import ChangeRefused

ROOT = Path(__file__).resolve().parents[1]
CSV = load_csv_dir(ROOT / "data")


def test_harness_refuses_without_or_equal_url(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        db.require_test_url()
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    with pytest.raises(RuntimeError):
        db.require_test_url()


DATA_TABLES = ["distributors", "skus", "weekly_sales", "inventory_snapshots", "calendar", "lead_times", "open_pos", "shelf_life",
               "unit_costs", "config_versions", "runs", "recommendations", "decisions", "sku_changes"]


@pytest.fixture(scope="session")
def schema_engine():
    url = db.require_test_url()  # raises (test errors) rather than touching any other database
    schema = "t_" + uuid.uuid4().hex[:10]
    admin = db.make_engine(url)
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    eng = db.make_engine(url, schema=schema)
    with eng.begin() as c:  # migrate once per session; tests only reset the data (far fewer network round trips)
        store.apply_migrations(c, ROOT / "migrations")
    yield eng
    eng.dispose()
    with admin.begin() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


@pytest.fixture()
def eng(schema_engine):
    """Same migrated schema, data reset to the ingested CSVs for each test."""
    with schema_engine.begin() as c:
        c.exec_driver_sql("TRUNCATE " + ", ".join(DATA_TABLES) + " RESTART IDENTITY CASCADE")
        store.ingest(c, CSV)
    return schema_engine


def load(eng):
    with eng.connect() as c:
        return store.load_data(c)


def shadow(eng):
    with eng.connect() as c:
        return store.shadow_stats(c)


def counts(conn, sku):
    return {t: conn.execute(text(f"SELECT count(*) FROM {t} WHERE sku_id = :s"), {"s": sku}).scalar()
            for t in ["skus"] + store.SKU_CHILD_TABLES}


def state(conn):
    out = {}
    for t, order in [("skus", "sku_id"), ("weekly_sales", "sku_id, iso_week"), ("inventory_snapshots", "sku_id, location, as_of_date")]:
        out[t] = conn.execute(text(f"SELECT coalesce(json_agg(row_to_json(x) ORDER BY {order}), '[]'::json) FROM (SELECT * FROM {t}) x")).scalar() \
            if False else conn.execute(text(f"SELECT coalesce(json_agg(x), '[]'::json) FROM (SELECT * FROM {t} ORDER BY {order}) x")).scalar()
    return out


NEW = dict(sku_name="Test Biscuit", category="Snack", perishability="Low", standard_reorder_qty=100, distributor_id="DIST001")


def new_sales(sku="SKU900", n=3):
    return pd.DataFrame({"SKU_ID": sku, "Week": [f"2026-W{40 + i}" for i in range(n)], "Units_Sold": [10, 12, 11][:n]})


def test_migrations_twice_and_ingest_idempotent(eng):
    with eng.begin() as c:
        assert store.apply_migrations(c, ROOT / "migrations") == []
        before = state(c)
        rep = store.ingest(c, CSV)
        assert all(i == 0 and u == 0 for i, u, _ in rep.values())
        assert state(c) == before
        assert c.execute(text("SELECT count(*) FROM weekly_sales")).scalar() == 240


def test_add_sku_writes_exact_rows(eng):
    with eng.begin() as c:
        store.apply_change(c, "add", "SKU900", "tester", NEW, new_sales(), (250, date(2026, 10, 19)))
    with eng.connect() as c:
        n = counts(c, "SKU900")
        assert n["skus"] == 1 and n["inventory_snapshots"] == 1 and n["weekly_sales"] == 3
        assert c.execute(text("SELECT count(*) FROM sku_changes")).scalar() == 1


@pytest.mark.parametrize("sku,fields,dist_ok", [
    ("SKU001", NEW, True),
    ("SKU901", {**NEW, "distributor_id": "DIST999"}, False),
    ("SKU902", {**NEW, "standard_reorder_qty": 0}, True),
    ("SKU903", {**NEW, "standard_reorder_qty": -5}, True),
])
def test_bad_add_writes_nothing(eng, sku, fields, dist_ok):
    with eng.connect() as c:
        before = state(c)
    with pytest.raises(ChangeRefused):
        with eng.begin() as c:
            store.apply_change(c, "add", sku, "tester", fields, None, (10, date(2026, 10, 19)))
    with eng.connect() as c:
        assert state(c) == before
        assert c.execute(text("SELECT count(*) FROM sku_changes")).scalar() == 0


def test_retire_keeps_all_rows(eng):
    with eng.connect() as c:
        before = counts(c, "SKU004")
    with eng.begin() as c:
        store.apply_change(c, "retire", "SKU004", "tester")
    with eng.connect() as c:
        assert counts(c, "SKU004") == before
        assert c.execute(text("SELECT status FROM skus WHERE sku_id='SKU004'")).scalar() == "Discontinued"
    res = run_plan(load(eng), date(2026, 10, 19))
    assert "SKU004" not in set(res.recs.sku_id)


def seed_history(eng, sku):
    data = load(eng)
    res = run_plan(data, date(2026, 10, 19))
    with eng.begin() as c:
        rid = store.save_run(c, res, None)
        store.log_decision(c, rid, sku, "STOCKOUT_RISK", 150, "Accept", None, None, "tester")
    return rid


def test_permanent_delete_removes_only_that_sku_and_keeps_history(eng):
    rid = seed_history(eng, "SKU004")
    with eng.connect() as c:
        other = counts(c, "SKU005")
    with eng.begin() as c:
        store.apply_change(c, "delete", "SKU004", "tester")
    with eng.connect() as c:
        assert all(v == 0 for v in counts(c, "SKU004").values())
        assert counts(c, "SKU005") == other
        assert c.execute(text("SELECT count(*) FROM recommendations WHERE sku_id='SKU004'")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM decisions WHERE sku_id='SKU004'")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM sku_changes WHERE action='delete'")).scalar() == 1
    assert rid


@pytest.mark.parametrize("action,sku,kw", [
    ("retire", "SKU004", {}),
    ("edit", "SKU005", {"fields": {"sku_name": "Renamed", "standard_reorder_qty": 175}, "onhand": (777, date(2026, 10, 20))}),
    ("delete", "SKU004", {}),
    ("add", "SKU900", {"fields": NEW, "sales": new_sales(), "onhand": (250, date(2026, 10, 19))}),
    ("append_sales", "SKU004", {"sales": pd.DataFrame({"SKU_ID": "SKU004", "Week": ["2026-W43"], "Units_Sold": [90]})}),
])
def test_every_change_logged_and_undo_restores_exactly(eng, action, sku, kw):
    with eng.connect() as c:
        before = state(c)
    with eng.begin() as c:
        store.apply_change(c, action, sku, "tester", **kw)
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM sku_changes WHERE action=:a AND sku_id=:s"), {"a": action, "s": sku}).scalar() == 1
        assert state(c) != before
    with eng.begin() as c:
        store.undo_last(c, "tester")
    with eng.connect() as c:
        assert state(c) == before
        assert c.execute(text("SELECT count(*) FROM sku_changes WHERE action='undo'")).scalar() == 1
        assert store.last_undoable(c) is None


def test_failure_mid_change_rolls_back(eng):
    with eng.connect() as c:
        before = state(c)

    def boom(step):
        if step == "after_inventory":
            raise RuntimeError("simulated failure")

    with pytest.raises(RuntimeError):
        with eng.begin() as c:
            store.apply_change(c, "add", "SKU900", "tester", NEW, new_sales(), (250, date(2026, 10, 19)), fault_hook=boom)
    with eng.connect() as c:
        assert state(c) == before
        assert c.execute(text("SELECT count(*) FROM sku_changes")).scalar() == 0

    def boom2(step):
        if step == "after_weekly_sales":
            raise RuntimeError("simulated failure")
    with pytest.raises(RuntimeError):
        with eng.begin() as c:
            store.apply_change(c, "delete", "SKU004", "tester", fault_hook=boom2)
    with eng.connect() as c:
        assert state(c) == before


def test_gate_dry_run_refuses_write(eng):
    # unknown distributor is refused by validation; a missing-inventory add is refused by the dry-run gate
    with pytest.raises(ChangeRefused):
        with eng.begin() as c:
            store.apply_change(c, "add", "SKU905", "tester", NEW, None, (None, None))
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM skus WHERE sku_id='SKU905'")).scalar() == 0


def test_sales_upload_for_other_sku_refused(eng):
    with pytest.raises(ChangeRefused):
        with eng.begin() as c:
            store.apply_change(c, "add", "SKU900", "tester", NEW, new_sales("SKU001"), (10, date(2026, 10, 19)))


def test_decision_logged_with_reason_and_missing_reason_rejected(eng):
    data = load(eng)
    res = run_plan(data, date(2026, 10, 19))
    with eng.begin() as c:
        rid = store.save_run(c, res, None)
        store.log_decision(c, rid, "SKU003", "STOCKOUT_RISK", 900, "Change quantity", 600, "Distributor MOQ", "raghav")
    with eng.connect() as c:
        r = c.execute(text("SELECT human_decision, human_qty, reason FROM decisions WHERE sku_id='SKU003'")).fetchone()
        assert tuple(r) == ("Change quantity", 600, "Distributor MOQ")
    for dec in ("Change quantity", "Reject", "Hold"):
        with pytest.raises(ChangeRefused):
            with eng.begin() as c:
                store.log_decision(c, rid, "SKU003", "STOCKOUT_RISK", 900, dec, 500, "  ", "raghav")
    # the database itself also enforces it
    with pytest.raises(Exception):
        with eng.begin() as c:
            c.execute(text("INSERT INTO decisions (run_id, sku_id, human_decision, decided_by) VALUES (:r, 'SKU003', 'Reject', 'x')"), {"r": rid})
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM decisions")).scalar() == 1
    s = shadow(eng)
    assert s["total"] == 1 and s["agreement"] == 0.0


def test_shadow_empty_state(eng):
    assert shadow(eng)["empty"] is True
