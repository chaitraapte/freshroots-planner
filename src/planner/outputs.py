"""Files written on every run: action list, exceptions, draft POs, one-page report."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ACTION_COLS = ["sku_id", "sku_name", "distributor_id", "primary_flag", "tags", "action", "rule_action", "urgency_tier", "urgency_weeks",
               "cover_weeks", "weekly_demand", "on_hand", "standard_qty", "brief_literal_qty", "brief_literal_cover_after",
               "recommended_qty", "multiples", "recommended_cover_after", "weeks_until_below_max", "stability", "needs_review",
               "review_reasons", "explanation"]


def _flat(df):
    df = df.copy()
    for c in df.columns:
        if df[c].map(lambda x: isinstance(x, (list, tuple))).any():
            df[c] = df[c].map(lambda x: "; ".join(map(str, x)) if isinstance(x, (list, tuple)) else x)
    return df


def report_markdown(res, assumptions: list[str]) -> str:
    r = res.recs
    m = res.meta
    lines = [f"# Freshroots demand planning run - as of {m['as_of']}", "",
             "**Everything below is a DRAFT. The tool does not forecast and does not place orders; a human approves or overrides each line.**", "",
             f"Festival month uplift applied: {'yes' if m['festival_month'] else 'no'}. Data fingerprint: `{m['fingerprint']}`.", "",
             "## Counts by flag", ""]
    for f, n in r["primary_flag"].value_counts().items():
        lines.append(f"- {f}: {n}")
    lines.append(f"- HIGH_DEMAND tag: {int(r['tags'].map(lambda t: 'HIGH_DEMAND' in t).sum())}")
    lines.append(f"- Routed to a human (REVIEW): {int(r['needs_review'].sum())}")
    lines += ["", "## Most urgent", ""]
    top = r[r["rule_action"] == "ORDER"].head(5)
    for _, x in top.iterrows():
        lines.append(f"- {x.sku_id} {x.sku_name}: {x.urgency_tier}, cover {x.cover_weeks:.1f} wk, "
                     f"recommend {int(x.recommended_qty) if not np.isnan(x.recommended_qty) else 'n/a'} units"
                     f"{' (needs review)' if x.needs_review else ''}")
    if top.empty:
        lines.append("- none")
    lines += ["", "## Brief rule vs sizing that restores cover", "",
              "| SKU | Brief literal qty | Cover after | Recommended qty | Cover after |", "|---|---|---|---|---|"]
    for _, x in r[r["rule_action"] == "ORDER"].iterrows():
        rq = "n/a" if np.isnan(x.recommended_qty) else f"{int(x.recommended_qty)}"
        rc = "n/a" if np.isnan(x.recommended_cover_after) else f"{x.recommended_cover_after:.1f} wk"
        lines.append(f"| {x.sku_id} | {int(x.brief_literal_qty)} | {x.brief_literal_cover_after:.1f} wk | {rq} | {rc} |")
    lines += ["", "## Unstable verdicts (depend on how the rules are read)", ""]
    uns = r[r["stability"] == "Unstable"]
    lines += [f"- {x.sku_id}: {'; '.join(y for y in x.review_reasons if 'interpreted' in y)}" for _, x in uns.iterrows()] or ["- none"]
    lines += ["", "## Exceptions queue", ""]
    lines += [f"- {x.sku_id}: {x.reason}" for _, x in res.exceptions.iterrows()] or ["- none"]
    lines += ["", "## Data warnings", ""]
    lines += [f"- [{f.level}] {f.message}" for f in res.gate.findings] or ["- none"]
    if len(res.excluded):
        lines += ["", "Excluded: discontinued: " + ", ".join(res.excluded["SKU_ID"])]
    lines += ["", "## Assumptions used", ""] + [f"- {a}" for a in assumptions]
    return "\n".join(lines) + "\n"


def write_outputs(res, out_dir, assumptions: list[str]) -> Path:
    out = Path(out_dir)
    (out / "draft_pos").mkdir(parents=True, exist_ok=True)
    for old in (out / "draft_pos").glob("*.csv"):
        old.unlink()
    _flat(res.recs[ACTION_COLS]).to_csv(out / "action_list.csv", index=False)
    res.exceptions.to_csv(out / "exceptions.csv", index=False)
    for d, df in res.draft_pos.items():
        df.to_csv(out / "draft_pos" / f"{d}.csv", index=False)
    (out / "run_report.md").write_text(report_markdown(res, assumptions))
    return out


def assumptions_list(cfg: dict, data: dict) -> list[str]:
    a = [f"Lead time defaults: {cfg['lead_time_weeks']['Low']} weeks non-perishable, {cfg['lead_time_weeks']['High']} week perishable"
         + ("" if "lead_times" not in data else " (supplied values override per SKU)") + ".",
         f"Shelf life default for High perishability: {cfg['shelf_life_days_high']} days"
         + ("" if "shelf_life" not in data else " (supplied values cap the maximum cover)") + ".",
         "Open POs: " + ("supplied." if "open_pos" in data else "none supplied, assumed zero."),
         "Unit cost: " + ("supplied." if "unit_cost" in data else "unknown, so money-at-risk shows n/a."),
         f"Festival uplift {cfg['festival_uplift_default']:.2f} (per-category overrides: {cfg['festival_uplift_by_category'] or 'none'}).",
         f"Target cover after a stockout order: {cfg['target_cover_weeks']} weeks (non-perishable); perishable aims at {cfg['perishable_target_weeks']} weeks.",
         "Distributor_ID is treated as the PO grouping key (the party an order is placed with); in the case brief distributors are customers, "
         "and two here sit outside Maharashtra/Goa."]
    return a
