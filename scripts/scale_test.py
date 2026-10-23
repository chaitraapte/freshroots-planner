"""Scale proof: synthetic 400 SKUs x 156 weeks x 1,200 distributors (and 10x), timing the engine. Synthetic data only; no database."""
import argparse
import time
from datetime import date, timedelta

import _common  # noqa: F401
import numpy as np
import pandas as pd
from planner.engine import run_plan
from planner.isoweeks import iso_label


def synth(n_sku, n_weeks, n_dist, seed=7, as_of=date(2026, 10, 19)):
    rng = np.random.default_rng(seed)
    last_mon = as_of - timedelta(days=as_of.weekday() + 7)
    weeks = [iso_label(last_mon - timedelta(weeks=i)) for i in range(n_weeks)][::-1]
    ids = [f"S{i:05d}" for i in range(n_sku)]
    base = rng.lognormal(4, 0.8, n_sku)
    units = rng.poisson(np.outer(base, np.ones(n_weeks)) * rng.uniform(0.9, 1.1, (n_sku, n_weeks)))
    sales = pd.DataFrame({"SKU_ID": np.repeat(ids, n_weeks), "Week": np.tile(weeks, n_sku), "Units_Sold": units.ravel()})
    dist = pd.DataFrame({"Distributor_ID": [f"D{i:04d}" for i in range(n_dist)], "Distributor_Name": "x", "Region": "Maharashtra", "Status": "Active"})
    sku = pd.DataFrame({"SKU_ID": ids, "SKU_Name": ids, "Category": rng.choice(["Snack", "Staple", "Beverage"], n_sku),
                        "Perishability": rng.choice(["High", "Low"], n_sku, p=[.2, .8]),
                        "Standard_Reorder_Qty": np.maximum(10, (base * rng.integers(1, 4, n_sku)).astype(int)), "Unit_of_Measure": "Units",
                        "Distributor_ID": rng.choice(dist.Distributor_ID, n_sku)})
    inv = pd.DataFrame({"SKU_ID": ids, "Location": "W", "Units_On_Hand": (base * rng.uniform(1, 20, n_sku)).astype(int), "As_Of_Date": str(as_of)})
    cal = pd.DataFrame([{"Year": y, "Month": m, "Year_Month": f"{y}-{m:02d}", "Is_Festival_Month": "TRUE" if m in (3, 10, 11) else "FALSE"}
                        for y in range(2023, 2028) for m in range(1, 13)])
    return {"weekly_sales": sales, "sku_master": sku, "distributor_master": dist, "current_inventory": inv, "calendar": cal}, as_of


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skus", type=int, nargs="*", default=[400, 4000])
    a = ap.parse_args()
    print(f"{'SKUs':>6} {'weeks':>6} {'dists':>6} {'sales rows':>11} {'seconds':>8}")
    for n in a.skus:
        data, as_of = synth(n, 156, 1200)
        t = time.perf_counter()
        res = run_plan(data, as_of)
        dt = time.perf_counter() - t
        print(f"{n:>6} {156:>6} {1200:>6} {len(data['weekly_sales']):>11,} {dt:>8.2f}   flags: {res.recs.primary_flag.value_counts().to_dict()}")


if __name__ == "__main__":
    main()
