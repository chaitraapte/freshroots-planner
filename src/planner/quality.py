"""Data-quality gate. Runs before any calculation, and in dry-run on proposed changes."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd


@dataclass
class Finding:
    level: str  # "stop" | "warn" | "info"
    code: str
    message: str
    ids: list = field(default_factory=list)


@dataclass
class GateResult:
    findings: list[Finding]

    @property
    def stops(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "stop"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.stops

    def summary(self) -> str:
        return "; ".join(f.message for f in self.findings) or "no findings"


def valid_week(w) -> bool:
    try:
        y, wk = str(w).split("-W")
        date.fromisocalendar(int(y), int(wk), 1)
        return True
    except Exception:  # noqa: BLE001
        return False


def _ids(series, limit: int = 10) -> list:
    return sorted({str(x) for x in series})[:limit]


def run_gate(data: dict[str, pd.DataFrame], run_date: date, config: dict) -> GateResult:
    sku = data["sku_master"]
    sales = data["weekly_sales"]
    inv = data["current_inventory"]
    dist = data["distributor_master"]
    out: list[Finding] = []

    def stop(code, msg, ids=()):
        out.append(Finding("stop", code, msg, list(ids)))

    known = set(sku["SKU_ID"])
    dup_master = sku[sku["SKU_ID"].duplicated()]
    if len(dup_master):
        stop("duplicate_master", f"Duplicate SKU_ID rows in sku_master: {_ids(dup_master['SKU_ID'])}", _ids(dup_master["SKU_ID"]))

    unk = set(sales["SKU_ID"]) - known
    unk |= set(inv["SKU_ID"]) - known
    if unk:
        stop("unknown_sku", f"Unknown SKU IDs (not in sku_master): {sorted(unk)[:10]}", sorted(unk))

    badw = sales[~sales["Week"].map(valid_week)]
    if len(badw):
        stop("invalid_week", f"{len(badw)} sales rows have a week that is not a real ISO week, e.g. {sorted(set(map(str, badw['Week'])))[:5]}", _ids(badw["SKU_ID"]))

    dups = sales[sales.duplicated(["SKU_ID", "Week"], keep=False)]
    if len(dups):
        stop("duplicate_sales", f"{len(dups)} duplicate SKU-week sales rows, e.g. {_ids(dups['SKU_ID'])}", _ids(dups["SKU_ID"]))

    neg = sales[pd.to_numeric(sales["Units_Sold"], errors="coerce").fillna(-1) < 0]
    if len(neg):
        stop("negative_sales", f"{len(neg)} sales rows with negative or non-numeric Units_Sold, e.g. {_ids(neg['SKU_ID'])}", _ids(neg["SKU_ID"]))
    badq = sku[pd.to_numeric(sku["Standard_Reorder_Qty"], errors="coerce").fillna(0) <= 0]
    if len(badq):
        stop("bad_standard_qty", f"Standard_Reorder_Qty must be positive: {_ids(badq['SKU_ID'])}", _ids(badq["SKU_ID"]))

    status = sku["Status"] if "Status" in sku else pd.Series("Active", index=sku.index)
    active_ids = set(sku.loc[status != "Discontinued", "SKU_ID"])
    miss_inv = active_ids - set(inv["SKU_ID"])
    if miss_inv:
        stop("missing_inventory", f"Active SKUs with no inventory row: {sorted(miss_inv)[:10]}", sorted(miss_inv))

    bad_dist = sku[~sku["Distributor_ID"].isin(set(dist["Distributor_ID"]))]
    if len(bad_dist):
        stop("unknown_distributor", f"SKUs whose Distributor_ID is not in distributors: {_ids(bad_dist['SKU_ID'])}", _ids(bad_dist["SKU_ID"]))

    if len(inv):
        latest = pd.to_datetime(inv["As_Of_Date"]).max().date()
        age = (run_date - latest).days
        if age > config["stale_inventory_days"]:
            out.append(Finding("warn", "stale_inventory", f"Inventory snapshot is {age} days older than the run date ({latest}).", []))

    disc = sorted(set(sku["SKU_ID"]) - active_ids)
    if disc:
        out.append(Finding("info", "discontinued", f"Excluded: discontinued: {disc}", disc))
    return GateResult(out)
