"""Explanations: deterministic template first; optional LLM rewording that is verified against engine facts."""
from __future__ import annotations

import re

import numpy as np


def _n(x, d=1):
    return f"{x:.{d}f}"


def build_facts(r, cfg) -> dict:
    return dict(
        sku=r["sku_id"], flag=r["primary_flag"], tags=list(r["tags"]), action=r["rule_action"],
        cover=r["cover_weeks"], min_w=r["min_weeks"], max_w=r["max_weeks"], demand=r["weekly_demand"],
        uplift=r["festival_uplift"], trend=r["trend"], rec_q=r["recommended_qty"], mult=r["multiples"],
        brief_q=r["brief_literal_qty"], rec_cov=r["recommended_cover_after"], brief_cov=r["brief_literal_cover_after"],
        to_below=r["weeks_until_below_max"], review=list(r["review_reasons"]), std=r["standard_qty"],
        lead=r["lead_time_weeks"], uw=r["urgency_weeks"],
    )


def template_explanation(f: dict) -> str:
    parts = []
    c = f["cover"]
    if f["flag"] == "STOCKOUT_RISK":
        s = f"Cover is {_n(c)} weeks against a {f['min_w']:g}-week minimum"
        if f["uplift"] and f["uplift"] != 1:
            s += f"; festival uplift raised weekly demand to {_n(f['demand'], 0)}"
        parts.append(s + ".")
    elif f["flag"] == "OVERSTOCK_RISK":
        parts.append(f"Cover is {_n(c)} weeks against a {f['max_w']:g}-week maximum; cover would take about "
                     f"{_n(f['to_below'])} weeks to fall back to the maximum with no receipts.")
    elif c is not None and not (isinstance(c, float) and np.isnan(c)):
        parts.append(f"Cover is {_n(c)} weeks, inside the {f['min_w']:g} to {f['max_w']:g}-week range.")
    else:
        parts.append("There is no sales history to compute cover.")
    if "HIGH_DEMAND" in f["tags"]:
        parts.append(f"Last-4-week sales are {_n(f['trend'] * 100, 0)}% above the 12-week average"
                     + (", so the order is sized at no less than 1.5x standard." if f["flag"] == "STOCKOUT_RISK" else "."))
    if f["action"] == "ORDER" and not np.isnan(f["rec_q"]):
        parts.append(f"Recommended order: {int(f['mult'])} x {int(f['std'])} = {int(f['rec_q'])} units, bringing cover to "
                     f"{_n(f['rec_cov'])} weeks. The literal brief quantity of {int(f['brief_q'])} would only reach {_n(f['brief_cov'])} weeks.")
    if f["action"] == "PAUSE ORDER":
        parts.append("Rule says pause the next order.")
    if f["review"]:
        parts.append("Needs a human: " + "; ".join(f["review"]) + ".")
    return " ".join(parts)


_NUM = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return set(_NUM.findall(text))


def allowed_numbers(f: dict, template: str) -> set[str]:
    nums = _numbers(template)
    for k, v in f.items():
        if isinstance(v, (int, float, np.floating, np.integer)) and not (isinstance(v, float) and np.isnan(v)):
            for d in (0, 1, 2):
                nums.add(f"{v:.{d}f}")
            nums.add(str(int(v)) if float(v).is_integer() else str(v))
    return nums


def reword(f: dict, template: str, api_key: str | None = None) -> tuple[str, str]:
    """Return (text, source). Falls back to the template on any problem or number mismatch."""
    from . import llm
    if llm.provider() is None:
        return template, "template"
    text = llm.complete("Reword the explanation in plain procurement language. Do not add facts, numbers, recommendations or SKUs that are not in the text. "
                        "Keep every number exactly as written. Output only the reworded text.", template, 300)
    if text is None:
        return template, "template (AI unavailable)"
    if not _numbers(text) <= allowed_numbers(f, template):
        return template, "template (AI output failed number check)"
    return text, llm.provider()
