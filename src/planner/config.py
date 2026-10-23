"""Editable assumptions. Every value here is shown on the Assumptions & limitations page."""
from __future__ import annotations

import copy

DEFAULT_CONFIG: dict = {
    "short_window_weeks": 4,
    "long_window_weeks": 12,
    "high_demand_trend": 0.20,
    "high_demand_multiplier": 1.5,
    "stockout_weeks": 6,
    "overstock_weeks": 14,
    "perishable_min_weeks": 3,
    "perishable_max_weeks": 4,
    "perishable_target_weeks": 3.5,
    "target_cover_weeks": 8,
    "overstock_review_trend": 0.10,
    "festival_uplift_default": 1.25,
    "festival_uplift_by_category": {},
    "lead_time_weeks": {"Low": 2, "High": 1},
    "shelf_life_days_high": 28,
    "watch_margin_weeks": 2,
    "min_history_weeks": 12,
    "cv_threshold": 0.30,
    "mad_threshold": 3.0,
    "mad_window_weeks": 8,
    "mad_floor_pct": 0.10,
    "stale_inventory_days": 2,
    "max_multiples": 100,
}


def merged(overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for k, v in (overrides or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def validate_config(cfg: dict) -> list[str]:
    """Human-readable problems with a config; empty list means it is safe to run."""
    out = []
    rng = {"festival_uplift_default": (0.5, 3), "high_demand_trend": (0, 5), "stockout_weeks": (0.5, 52), "overstock_weeks": (1, 104),
           "target_cover_weeks": (0.5, 104), "cv_threshold": (0, 5), "mad_floor_pct": (0, 1), "overstock_review_trend": (0, 5),
           "perishable_min_weeks": (0.1, 52), "perishable_max_weeks": (0.1, 52), "perishable_target_weeks": (0.1, 52), "watch_margin_weeks": (0, 52)}
    label = {"festival_uplift_default": "Festival uplift", "high_demand_trend": "Demand-surge trigger", "stockout_weeks": "Running low below (weeks)",
             "overstock_weeks": "Overstocked above (weeks)", "target_cover_weeks": "Target cover after an order (weeks)", "cv_threshold": "Volatility limit",
             "mad_floor_pct": "Outlier floor", "overstock_review_trend": "Overstock review trend", "perishable_min_weeks": "Perishable minimum (weeks)",
             "perishable_max_weeks": "Perishable maximum (weeks)", "perishable_target_weeks": "Perishable target (weeks)", "watch_margin_weeks": "Watch margin (weeks)"}
    for k, (lo, hi) in rng.items():
        v = cfg.get(k)
        if not _num(v) or not lo <= v <= hi:
            out.append(f"{label.get(k, k)} must be a number between {lo:g} and {hi:g}")
    for k, v in (cfg.get("lead_time_weeks") or {}).items():
        if not _num(v) or not 0 <= v <= 52:
            out.append(f"Restock time ({'normal' if k == 'Low' else 'perishable'}) must be a number between 0 and 52 weeks")
    for k, v in (cfg.get("festival_uplift_by_category") or {}).items():
        if not _num(v) or not 0.5 <= v <= 3:
            out.append(f"festival uplift for {k} must be between 0.5 and 3")
    if not out:
        if cfg["stockout_weeks"] >= cfg["overstock_weeks"]:
            out.append("'Running low below' must be smaller than 'Overstocked above'")
        if not cfg["stockout_weeks"] <= cfg["target_cover_weeks"] <= cfg["overstock_weeks"]:
            out.append("Target cover after an order must sit between the running-low and overstocked limits")
        if cfg["perishable_min_weeks"] >= cfg["perishable_max_weeks"]:
            out.append("perishable minimum must be smaller than perishable maximum")
    return out
