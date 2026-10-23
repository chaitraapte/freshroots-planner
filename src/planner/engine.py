"""Deterministic planning engine. Pure functions over DataFrames; never touches the database."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import merged
from .explain import build_facts, template_explanation
from .isoweeks import iso_monday, week_range, week_thursday
from .quality import GateResult, run_gate

READINGS = {
    "base": "base reading",
    "prior12": "12-week average taken from the 12 weeks before the last 4",
    "no_uplift": "festival uplift off",
    "last4_base": "last-4-week average as the demand base",
}
URGENCY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "": 4}


@dataclass
class PlanResult:
    recs: pd.DataFrame
    gate: GateResult
    exceptions: pd.DataFrame
    draft_pos: dict
    excluded: pd.DataFrame
    meta: dict
    matrix: pd.DataFrame | None = None


def build_matrix(sales: pd.DataFrame, sku_ids: list, as_of: date) -> pd.DataFrame:
    """SKU x contiguous ISO weeks (NaN = no row). A week still in progress at as_of is not demand yet, so it is ignored."""
    if sales.empty:
        return pd.DataFrame(index=sku_ids, columns=[], dtype=float)
    s = sales[["SKU_ID", "Week", "Units_Sold"]].copy()
    s["_mon"] = s["Week"].map(iso_monday)
    s = s[s["_mon"] + timedelta(days=7) <= as_of]
    if s.empty:
        return pd.DataFrame(index=sku_ids, columns=[], dtype=float)
    labels = week_range(s["_mon"].min(), s["_mon"].max())
    wide = s.pivot_table(index="SKU_ID", columns="Week", values="Units_Sold", aggfunc="sum")
    return wide.reindex(index=sku_ids, columns=labels).astype(float)


def festival_flag(calendar: pd.DataFrame, d: date) -> bool:
    row = calendar[(calendar["Year"] == d.year) & (calendar["Month"] == d.month)]
    if row.empty:
        return False
    return str(row["Is_Festival_Month"].iloc[0]).upper() == "TRUE"


def _pad(M: pd.DataFrame, n: int) -> pd.DataFrame:
    if M.shape[1] >= n:
        return M
    pad = pd.DataFrame(np.nan, index=M.index, columns=[f"_pad{i}" for i in range(n - M.shape[1])])
    return pd.concat([pad, M], axis=1)


def primary_flag(pos, wd, min_t, max_t):
    cover = np.where(wd > 0, pos / np.where(wd > 0, wd, 1), np.inf)
    flag = np.where(~(wd > 0), "OK", np.where(cover < min_t, "STOCKOUT_RISK", np.where(cover > max_t, "OVERSTOCK_RISK", "OK")))
    return cover, flag


def _exceptions(M, master, inv_onhand, dist_status, cfg) -> dict[str, list[str]]:
    """Reasons each SKU needs a human (the undocumented 15-20%)."""
    reasons = {s: [] for s in master.index}
    n_hist = M.notna().sum(axis=1)
    last12 = M.iloc[:, -cfg["long_window_weeks"]:]
    first_idx = M.notna().values.argmax(axis=1)
    for i, s in enumerate(master.index):
        if n_hist.iloc[i] == 0:
            reasons[s].append("no history (no sales rows at all)")
            continue
        if n_hist.iloc[i] < cfg["min_history_weeks"]:
            reasons[s].append(f"history is {int(n_hist.iloc[i])} weeks, shorter than {cfg['min_history_weeks']}")
        row = M.iloc[i].values
        seen = row[first_idx[i]:]
        if np.isnan(seen).any():
            reasons[s].append(f"{int(np.isnan(seen).sum())} missing week(s) in history")
        if (row[~np.isnan(row)] == 0).any():
            reasons[s].append("zero-sales week(s) in history (possible stockout-censored demand)")
    # coefficient of variation over the last 12 weeks
    mean = last12.mean(axis=1)
    cv = last12.std(axis=1, ddof=1) / mean.replace(0, np.nan)
    for s in master.index[(cv > cfg["cv_threshold"]).values]:
        reasons[s].append(f"weekly coefficient of variation {cv[s]:.2f} exceeds {cfg['cv_threshold']:.2f}")
    # MAD outliers against a trailing rolling median
    w = cfg["mad_window_weeks"]
    for i, s in enumerate(master.index):
        row = M.iloc[i]
        vals = row.values
        n = len(vals)
        bad = []
        for t in range(max(n - cfg["long_window_weeks"], 0), n):
            if np.isnan(vals[t]):
                continue
            prev = vals[max(0, t - w):t]
            prev = prev[~np.isnan(prev)]
            if len(prev) < 4:
                continue
            med = np.median(prev)
            mad = 1.4826 * np.median(np.abs(prev - med))
            mad = max(mad, cfg["mad_floor_pct"] * med, 1.0)
            if abs(vals[t] - med) > cfg["mad_threshold"] * mad:
                bad.append(M.columns[t])
        if bad:
            reasons[s].append(f"week(s) {', '.join(bad)} more than {cfg['mad_threshold']:g} MADs from the rolling median")
    for s, oh in inv_onhand.items():
        if oh <= 0:
            reasons[s].append("on-hand is zero or negative (possible stockout-censored demand)")
    for s in master.index:
        if dist_status.get(s) != "Active":
            reasons[s].append(f"distributor is not Active ({dist_status.get(s)})")
    return reasons


def run_plan(data: dict[str, pd.DataFrame], as_of: date, config: dict | None = None,
             run_date: date | None = None, enforce_gate: bool = True) -> PlanResult:
    cfg = merged(config)
    run_date = run_date or as_of
    sku = data["sku_master"].copy()
    if "Status" not in sku:
        sku["Status"] = "Active"
    gate = run_gate({**data, "sku_master": sku}, run_date, cfg)
    if enforce_gate and not gate.ok:
        raise GateStop(gate)

    excluded = sku[sku["Status"] == "Discontinued"][["SKU_ID", "SKU_Name"]].assign(reason="excluded: discontinued")
    master = sku[sku["Status"] != "Discontinued"].set_index("SKU_ID")
    ids = list(master.index)

    inv = data["current_inventory"].copy()
    inv["As_Of_Date"] = pd.to_datetime(inv["As_Of_Date"])
    inv = inv.sort_values("As_Of_Date").groupby("SKU_ID").agg(on_hand=("Units_On_Hand", "sum"), inv_date=("As_Of_Date", "max"))
    on_hand = inv["on_hand"].reindex(ids).astype(float)

    open_po = data.get("open_pos")
    po_units = pd.Series(0.0, index=ids)
    if open_po is not None and len(open_po):
        po_units = open_po.groupby("SKU_ID")["Open_PO_Units"].sum().reindex(ids).fillna(0.0)
    lead_df = data.get("lead_times")
    lead_default = master["Perishability"].map(cfg["lead_time_weeks"]).astype(float)
    lead = lead_default.copy()
    lead_src = pd.Series("default", index=ids)
    if lead_df is not None and len(lead_df):
        sup = lead_df.set_index("SKU_ID")["Lead_Time_Weeks"].reindex(ids)
        lead = sup.fillna(lead_default)
        lead_src[sup.notna()] = "supplied"
    shelf = data.get("shelf_life")
    unit_cost = data.get("unit_cost")

    M = build_matrix(data["weekly_sales"][data["weekly_sales"]["SKU_ID"].isin(ids)], ids, as_of)
    M = _pad(M, 16)
    short, long_ = cfg["short_window_weeks"], cfg["long_window_weeks"]
    last4 = M.iloc[:, -short:].mean(axis=1)
    avg12 = M.iloc[:, -long_:].mean(axis=1)
    prior = M.iloc[:, -(long_ + short):-short].mean(axis=1)
    prior = prior.fillna(avg12)
    trend = last4 / avg12.where(avg12 > 0) - 1

    is_fest = festival_flag(data["calendar"], as_of)
    uplift = master["Category"].map(lambda c: cfg["festival_uplift_by_category"].get(c, cfg["festival_uplift_default"])).astype(float)
    uplift = uplift if is_fest else pd.Series(1.0, index=ids)

    perish = (master["Perishability"] == "High").values
    min_t = np.where(perish, cfg["perishable_min_weeks"], cfg["stockout_weeks"]).astype(float)
    max_t = np.where(perish, cfg["perishable_max_weeks"], cfg["overstock_weeks"]).astype(float)
    if shelf is not None and len(shelf):
        sl = shelf.set_index("SKU_ID")["Shelf_Life_Days"].reindex(ids)
        cap = (sl / 7.0).values
        max_t = np.where(perish & ~np.isnan(cap), np.minimum(max_t, cap), max_t)
    shelf_note = pd.Series("default" if shelf is None else "supplied", index=ids)

    pos = (on_hand + po_units).values
    wd = (avg12 * uplift).values
    cover, flag = primary_flag(pos, wd, min_t, max_t)

    readings = {
        "base": flag,
        "prior12": primary_flag(pos, (prior * uplift).values, min_t, max_t)[1],
        "no_uplift": primary_flag(pos, avg12.values, min_t, max_t)[1],
        "last4_base": primary_flag(pos, (last4 * uplift).values, min_t, max_t)[1],
    }
    disagree = [[READINGS[k] + f" -> {readings[k][i]}" for k in readings if k != "base" and readings[k][i] != flag[i]]
                for i in range(len(ids))]
    unstable = np.array([len(d) > 0 for d in disagree])

    trend_v = trend.values
    high_demand = np.nan_to_num(trend_v, nan=-1) > cfg["high_demand_trend"]
    std = master["Standard_Reorder_Qty"].astype(float).values
    hd_mult = cfg["high_demand_multiplier"]

    # Order sizing, vectorised over SKUs and k = 1..max_multiples
    K = np.arange(1, cfg["max_multiples"] + 1)
    wd_safe = np.where(wd > 0, wd, np.nan)
    cover_k = (pos[:, None] + K[None, :] * std[:, None]) / wd_safe[:, None]
    kmin = np.where(high_demand, int(np.ceil(hd_mult)), 1)
    valid = (K[None, :] >= kmin[:, None])
    tgt = cfg["perishable_target_weeks"]
    ok_np = (cover_k >= cfg["target_cover_weeks"]) & (cover_k <= max_t[:, None]) & valid
    ok_p = (cover_k >= min_t[:, None]) & (cover_k <= max_t[:, None]) & valid
    ok = np.where(perish[:, None], ok_p, ok_np)
    score = np.where(perish[:, None], np.abs(cover_k - tgt), K[None, :].astype(float))
    score = np.where(ok, score, np.inf)
    best = score.argmin(axis=1)
    fits = np.isfinite(score.min(axis=1))
    k_rec = np.where(fits, K[best], 0)

    rows = []
    for i, s in enumerate(ids):
        pf = flag[i]
        tags = ["HIGH_DEMAND"] if high_demand[i] else []
        reasons, review_reasons = [], []
        rule_action, urgency, uw = "NONE", "", np.nan
        brief_q = rec_q = k = np.nan
        brief_cov = rec_cov = np.nan
        to_below = np.nan
        c = cover[i]
        if pf == "STOCKOUT_RISK":
            rule_action = "ORDER"
            brief_q = std[i] * (hd_mult if high_demand[i] else 1.0)
            brief_cov = (pos[i] + brief_q) / wd[i]
            reasons.append(f"cover {c:.1f} weeks is below the {min_t[i]:g}-week minimum")
            if fits[i]:
                k = int(k_rec[i]); rec_q = k * std[i]; rec_cov = cover_k[i, k - 1]
            else:
                review_reasons.append("no whole multiple of the standard quantity brings cover inside the allowed range"
                                      + (" while keeping the 1.5x high-demand floor" if high_demand[i] else ""))
        elif pf == "OVERSTOCK_RISK":
            rule_action = "PAUSE ORDER"
            reasons.append(f"cover {c:.1f} weeks is above the {max_t[i]:g}-week maximum")
            to_below = c - max_t[i]
            if np.nan_to_num(trend_v[i], nan=-1) >= cfg["overstock_review_trend"]:
                review_reasons.append("overstock flag but demand is rising")
            if high_demand[i]:
                review_reasons.append("HIGH_DEMAND combined with OVERSTOCK_RISK")
        elif high_demand[i]:
            rule_action = "WATCH"
            reasons.append("demand is rising fast but cover is inside the allowed range")
        if np.isfinite(c):
            uw = c - lead.iloc[i]
        if rule_action == "ORDER":
            urgency = "CRITICAL" if uw <= 0 else "HIGH"
        elif np.isfinite(c) and c < min_t[i] + cfg["watch_margin_weeks"] and pf == "OK":
            urgency = "MEDIUM"
        elif pf == "OK":
            urgency = "LOW"
        if unstable[i]:
            review_reasons.append("verdict depends on how the rules are interpreted: " + "; ".join(disagree[i]))
        rows.append(dict(
            sku_id=s, sku_name=master.iloc[i]["SKU_Name"], category=master.iloc[i]["Category"],
            perishability=master.iloc[i]["Perishability"], distributor_id=master.iloc[i]["Distributor_ID"],
            standard_qty=std[i], on_hand=on_hand.iloc[i], open_po_units=po_units.iloc[i],
            last4_avg=last4.iloc[i], avg12=avg12.iloc[i], trend=trend_v[i],
            festival_uplift=uplift.iloc[i], weekly_demand=wd[i], cover_weeks=c if np.isfinite(c) else np.nan,
            min_weeks=min_t[i], max_weeks=max_t[i], lead_time_weeks=lead.iloc[i], lead_time_source=lead_src.iloc[i],
            primary_flag=pf, tags=tags, rule_action=rule_action,
            brief_literal_qty=brief_q, brief_literal_cover_after=brief_cov,
            recommended_qty=rec_q, multiples=k, recommended_cover_after=rec_cov,
            weeks_until_below_max=to_below,
            urgency_tier=urgency, urgency_weeks=uw,
            stability="Unstable" if unstable[i] else "Stable",
            reasons=reasons, review_reasons=review_reasons,
        ))
    recs = pd.DataFrame(rows)

    exc = _exceptions(M.loc[ids], master, on_hand, master["Distributor_ID"].map(
        data["distributor_master"].set_index("Distributor_ID")["Status"]).to_dict(), cfg)
    recs["exception_reasons"] = recs["sku_id"].map(exc)
    recs["review_reasons"] = recs.apply(lambda r: r["review_reasons"] + r["exception_reasons"], axis=1)
    recs["needs_review"] = recs["review_reasons"].map(bool)
    recs["action"] = np.where(recs["needs_review"], "REVIEW", recs["rule_action"])
    recs["explanation"] = recs.apply(lambda r: template_explanation(build_facts(r, cfg)), axis=1)
    if unit_cost is not None and len(unit_cost):
        uc = unit_cost.set_index("SKU_ID")["Unit_Cost_INR"]
        recs["order_value_inr"] = recs["recommended_qty"] * recs["sku_id"].map(uc)
    else:
        recs["order_value_inr"] = np.nan
    recs["_u"] = recs["urgency_tier"].map(URGENCY_ORDER)
    recs = recs.sort_values(["_u", "urgency_weeks", "sku_id"], na_position="last").drop(columns="_u").reset_index(drop=True)

    exceptions = recs[recs["exception_reasons"].map(bool)][["sku_id", "sku_name", "distributor_id", "exception_reasons"]].copy()
    exceptions["reason"] = exceptions["exception_reasons"].map("; ".join)
    exceptions = exceptions.drop(columns="exception_reasons")

    pos_ = build_draft_pos(recs, data["distributor_master"])
    meta = dict(as_of=str(as_of), run_date=str(run_date), festival_month=is_fest, config=cfg,
                weeks=[c for c in M.columns if not str(c).startswith("_pad")],
                fingerprint=fingerprint(data))
    return PlanResult(recs, gate, exceptions, pos_, excluded, meta, M)


def build_draft_pos(recs: pd.DataFrame, dist: pd.DataFrame) -> dict:
    lines = recs[(recs["action"] == "ORDER") & (~recs["needs_review"]) & (recs["recommended_qty"] > 0)]
    out = {}
    names = dist.set_index("Distributor_ID")["Distributor_Name"].to_dict()
    for d, g in lines.groupby("distributor_id"):
        df = pd.DataFrame({
            "distributor_id": d, "distributor_name": names.get(d, ""),
            "sku_id": g["sku_id"], "sku_name": g["sku_name"], "multiples": g["multiples"].astype(int),
            "quantity": g["recommended_qty"].astype(int), "reason": g["reasons"].map("; ".join),
            "status": "DRAFT - requires human approval"})
        out[d] = df.reset_index(drop=True)
    return out


def fingerprint(data: dict) -> str:
    import hashlib
    h = hashlib.sha256()
    for k in sorted(data):
        df = data[k]
        if df is None:
            continue
        h.update(k.encode())
        h.update(df.sort_values(list(df.columns)).reset_index(drop=True).to_csv(index=False).encode())
    return h.hexdigest()[:16]


class GateStop(Exception):
    def __init__(self, gate: GateResult):
        self.gate = gate
        super().__init__("Data-quality gate stopped the run: " + gate.summary())
