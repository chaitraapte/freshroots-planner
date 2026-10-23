from datetime import date

import pandas as pd
import pytest

from planner.csvio import load_csv_dir
from planner.engine import GateStop, run_plan

AS_OF = date(2026, 10, 19)


@pytest.fixture(scope="module")
def data():
    return load_csv_dir("data")


@pytest.fixture(scope="module")
def res(data):
    return run_plan(data, AS_OF)


def row(res, s):
    return res.recs[res.recs.sku_id == s].iloc[0]


@pytest.mark.parametrize("sku,flag,cover", [
    ("SKU001", "STOCKOUT_RISK", 3.4), ("SKU003", "STOCKOUT_RISK", 4.4), ("SKU004", "STOCKOUT_RISK", 1.8),
    ("SKU011", "STOCKOUT_RISK", 1.8), ("SKU013", "STOCKOUT_RISK", 3.2),
    ("SKU006", "OVERSTOCK_RISK", 22.2), ("SKU007", "OVERSTOCK_RISK", 4.1)])
def test_primary_flags(res, sku, flag, cover):
    r = row(res, sku)
    assert r.primary_flag == flag
    assert r.cover_weeks == pytest.approx(cover, abs=0.05)


def test_all_others_ok(res):
    named = {"SKU001", "SKU003", "SKU004", "SKU011", "SKU013", "SKU006", "SKU007"}
    others = res.recs[~res.recs.sku_id.isin(named)]
    assert len(others) == 8 and (others.primary_flag == "OK").all()


def test_sku001_high_demand(res):
    assert "HIGH_DEMAND" in row(res, "SKU001").tags
    assert row(res, "SKU001").recommended_qty >= 750


def test_sku007_ends_in_review_and_unstable(res):
    r = row(res, "SKU007")
    assert r.action == "REVIEW" and r.stability == "Unstable"
    assert any("last-4-week" in x for x in r.review_reasons)


def test_sku002_unstable_overstock_without_uplift(res):
    r = row(res, "SKU002")
    assert r.stability == "Unstable" and r.action == "REVIEW"
    assert any("uplift off -> OVERSTOCK_RISK" in x for x in r.review_reasons)
    assert r.on_hand / r.avg12 == pytest.approx(14.3, abs=0.05)


def test_sizing_not_blind_round_up(res):
    r = row(res, "SKU004")
    assert r.multiples == 1 and r.recommended_qty == 150
    assert r.recommended_cover_after == pytest.approx(3.3, abs=0.05)


def test_cover_after_orders_inside_range(res):
    ordered = res.recs[res.recs.recommended_qty > 0]
    assert ((ordered.recommended_cover_after >= ordered.min_weeks) & (ordered.recommended_cover_after <= ordered.max_weeks)).all()
    ok = res.recs[res.recs.primary_flag == "OK"]
    assert ((ok.cover_weeks >= ok.min_weeks) & (ok.cover_weeks <= ok.max_weeks)).all()


def test_brief_literal_gap(res):
    assert row(res, "SKU003").brief_literal_cover_after == pytest.approx(6.0, abs=0.05)
    assert row(res, "SKU013").brief_literal_cover_after == pytest.approx(5.3, abs=0.05)
    assert row(res, "SKU003").recommended_cover_after > 8


def test_draft_pos_skip_review_and_ok(res):
    allpo = pd.concat(res.draft_pos.values())
    for s in ("SKU007", "SKU002", "SKU006"):
        assert s not in allpo["sku_id"].tolist()
    assert "SKU004" in allpo["sku_id"].tolist()
    assert (allpo["status"] == "DRAFT - requires human approval").all()


# ---- gate ----
def broken(data, **mods):
    d = {k: v.copy() for k, v in data.items()}
    for k, f in mods.items():
        d[k] = f(d[k])
    return d


@pytest.mark.parametrize("mod,code", [
    ({"weekly_sales": lambda d: pd.concat([d, d.iloc[[0]]])}, "duplicate_sales"),
    ({"weekly_sales": lambda d: d.assign(Units_Sold=d.Units_Sold.where(d.index != 0, -5))}, "negative_sales"),
    ({"weekly_sales": lambda d: pd.concat([d, pd.DataFrame([{"SKU_ID": "SKU999", "Week": "2026-W42", "Units_Sold": 1}])])}, "unknown_sku"),
    ({"current_inventory": lambda d: d.iloc[1:]}, "missing_inventory"),
    ({"sku_master": lambda d: d.assign(Distributor_ID=d.Distributor_ID.where(d.index != 0, "DIST999"))}, "unknown_distributor"),
])
def test_gate_stops(data, mod, code):
    with pytest.raises(GateStop) as e:
        run_plan(broken(data, **mod), AS_OF)
    assert code in {f.code for f in e.value.gate.stops}


def test_stale_inventory_only_warns(data):
    r = run_plan(data, AS_OF, run_date=date(2026, 10, 25))
    assert r.gate.ok and "stale_inventory" in {f.code for f in r.gate.warnings}


# ---- exceptions ----
def test_short_history_and_no_history_sku(data):
    new = pd.DataFrame([
        {"SKU_ID": "SKU101", "SKU_Name": "New A", "Category": "Snack", "Perishability": "Low", "Standard_Reorder_Qty": 100, "Unit_of_Measure": "Units", "Distributor_ID": "DIST001"},
        {"SKU_ID": "SKU102", "SKU_Name": "New B", "Category": "Snack", "Perishability": "Low", "Standard_Reorder_Qty": 100, "Unit_of_Measure": "Units", "Distributor_ID": "DIST001"}])
    wk = [f"2026-W{w}" for w in range(37, 43)]
    sales = pd.concat([data["weekly_sales"], pd.DataFrame({"SKU_ID": "SKU101", "Week": wk, "Units_Sold": 50})])
    inv = pd.concat([data["current_inventory"], pd.DataFrame([
        {"SKU_ID": s, "Location": "Pune Central Warehouse", "Units_On_Hand": 300, "As_Of_Date": "2026-10-19"} for s in ("SKU101", "SKU102")])])
    d = {**data, "sku_master": pd.concat([data["sku_master"], new]), "weekly_sales": sales, "current_inventory": inv}
    r = run_plan(d, AS_OF)
    exc = r.exceptions.set_index("sku_id")["reason"]
    assert "shorter than 12" in exc["SKU101"] and "no history" in exc["SKU102"]
    assert row(r, "SKU101").action == "REVIEW" and row(r, "SKU102").action == "REVIEW"


# ---- conflict rules ----
def single(data, sku="SKU001", perish=None, on_hand=None):
    d = {k: v.copy() for k, v in data.items()}
    d["sku_master"] = d["sku_master"][d["sku_master"].SKU_ID == sku].copy()
    d["weekly_sales"] = d["weekly_sales"][d["weekly_sales"].SKU_ID == sku].copy()
    d["current_inventory"] = d["current_inventory"][d["current_inventory"].SKU_ID == sku].copy()
    if perish:
        d["sku_master"]["Perishability"] = perish
    if on_hand is not None:
        d["current_inventory"]["Units_On_Hand"] = on_hand
    return d


def test_high_demand_with_overstock_gives_review(data):
    x = row(run_plan(single(data, on_hand=20000), AS_OF), "SKU001")
    assert x.primary_flag == "OVERSTOCK_RISK" and "HIGH_DEMAND" in x.tags and x.action == "REVIEW"


def test_overstock_with_rising_trend_review(data):
    d = single(data, "SKU012", on_hand=20000)  # trend about +5.7%: no review from trend alone
    assert row(run_plan(d, AS_OF), "SKU012").rule_action == "PAUSE ORDER"
    s = d["weekly_sales"]
    m = s.Week >= "2026-W39"
    s.loc[m, "Units_Sold"] = (s.loc[m, "Units_Sold"] * 1.12).round().astype(int)
    x = row(run_plan(d, AS_OF), "SKU012")
    assert x.trend >= 0.10 and x.action == "REVIEW"
    assert any("demand is rising" in r for r in x.review_reasons)


def test_perishable_window_overrides_general(data):
    # SKU002 cover about 11.5 weeks: OK as non-perishable, overstock when perishable
    assert row(run_plan(single(data, "SKU002", perish="Low"), AS_OF), "SKU002").primary_flag == "OK"
    x = row(run_plan(single(data, "SKU002", perish="High"), AS_OF), "SKU002")
    assert x.primary_flag == "OVERSTOCK_RISK" and x.max_weeks == 4


def test_discontinued_excluded(data):
    d = {k: v.copy() for k, v in data.items()}
    d["sku_master"]["Status"] = "Active"
    d["sku_master"].loc[d["sku_master"].SKU_ID == "SKU004", "Status"] = "Discontinued"
    r = run_plan(d, AS_OF)
    assert "SKU004" not in set(r.recs.sku_id)
    assert "SKU004" in set(r.excluded.SKU_ID)
    assert "SKU004" not in pd.concat(r.draft_pos.values())["sku_id"].tolist()


def test_perishable_high_demand_floor_breaks_cap_goes_to_review(data):
    d = single(data, "SKU004", on_hand=50)
    s = d["weekly_sales"]
    s.loc[s.Week >= "2026-W39", "Units_Sold"] = 140
    x = row(run_plan(d, AS_OF), "SKU004")
    assert x.primary_flag == "STOCKOUT_RISK" and "HIGH_DEMAND" in x.tags
    assert x.action == "REVIEW"
