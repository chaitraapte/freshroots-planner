"""In-memory scenarios for the Scenario lab. Nothing here writes anywhere."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import build_matrix, festival_flag
from .isoweeks import week_thursday


def _copy(data):
    return {k: v.copy() for k, v in data.items()}


def scale_recent_sales(data, sku_id, last_n, factor):
    d = _copy(data)
    s = d["weekly_sales"]
    weeks = sorted(s.loc[s.SKU_ID == sku_id, "Week"].unique())[-last_n:]
    m = (s.SKU_ID == sku_id) & s.Week.isin(weeks)
    s.loc[m, "Units_Sold"] = (s.loc[m, "Units_Sold"] * factor).round().astype(int)
    return d


def set_on_hand(data, sku_id, units):
    d = _copy(data)
    i = d["current_inventory"]
    i.loc[i.SKU_ID == sku_id, "Units_On_Hand"] = int(units)
    return d


def truncate_history(data, sku_id, keep_weeks):
    d = _copy(data)
    s = d["weekly_sales"]
    keep = sorted(s.loc[s.SKU_ID == sku_id, "Week"].unique())[-keep_weeks:]
    d["weekly_sales"] = s[(s.SKU_ID != sku_id) | s.Week.isin(keep)]
    return d


def set_lead_time(data, sku_id, weeks):
    d = _copy(data)
    lt = d.get("lead_times", pd.DataFrame(columns=["SKU_ID", "Lead_Time_Weeks"]))
    lt = lt[lt.SKU_ID != sku_id]
    d["lead_times"] = pd.concat([lt, pd.DataFrame([{"SKU_ID": sku_id, "Lead_Time_Weeks": float(weeks)}])], ignore_index=True)
    return d


def set_perishability(data, sku_id, value):
    d = _copy(data)
    m = d["sku_master"]
    m.loc[m.SKU_ID == sku_id, "Perishability"] = value
    return d


PRESETS = {
    "Festival surge (+40% in the last 3 weeks)": "surge",
    "Demand crash (-30% in the last 4 weeks)": "crash",
    "Stock drop to 2 weeks of cover": "stockdrop",
    "New launch (history cut to 6 weeks)": "launch",
    "Perishable overstock": "perish_over",
}


def apply_preset(data, sku_id, preset, base_weekly_demand):
    if preset == "surge":
        return scale_recent_sales(data, sku_id, 3, 1.4)
    if preset == "crash":
        return scale_recent_sales(data, sku_id, 4, 0.7)
    if preset == "stockdrop":
        return set_on_hand(data, sku_id, round(2 * base_weekly_demand))
    if preset == "launch":
        return truncate_history(data, sku_id, 6)
    if preset == "perish_over":
        return set_on_hand(set_perishability(data, sku_id, "High"), sku_id, round(6 * base_weekly_demand))
    raise ValueError(preset)


def festival_lift(data, as_of) -> pd.DataFrame:
    """Data-led festival lift per SKU: festival-flagged weeks vs non-festival weeks before them."""
    ids = list(data["sku_master"]["SKU_ID"])
    M = build_matrix(data["weekly_sales"], ids, as_of)
    cal = data["calendar"]
    fest = [bool(festival_flag(cal, week_thursday(w))) for w in M.columns]
    fest = np.array(fest)
    if not fest.any() or fest.all():
        return pd.DataFrame(columns=["SKU_ID", "Category", "lift", "festival_weeks", "baseline_weeks"])
    first = int(np.argmax(fest))
    base_cols = [c for c, f in zip(M.columns[:first], fest[:first]) if not f]
    fest_cols = [c for c, f in zip(M.columns, fest) if f]
    lift = M[fest_cols].mean(axis=1) / M[base_cols].mean(axis=1) - 1
    out = pd.DataFrame({"SKU_ID": ids, "lift": lift.values})
    out["Category"] = out["SKU_ID"].map(data["sku_master"].set_index("SKU_ID")["Category"])
    out["festival_weeks"] = f"{fest_cols[0]} to {fest_cols[-1]}"
    out["baseline_weeks"] = f"{base_cols[0]} to {base_cols[-1]}"
    return out
