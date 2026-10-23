from __future__ import annotations

from pathlib import Path

import pandas as pd

REQUIRED = {"weekly_sales": "weekly_sales.csv", "sku_master": "sku_master.csv", "distributor_master": "distributor_master.csv",
            "current_inventory": "current_inventory.csv", "calendar": "calendar.csv"}
OPTIONAL = {"lead_times": "lead_times.csv", "open_pos": "open_pos.csv", "shelf_life": "shelf_life.csv", "unit_cost": "unit_cost.csv"}


def load_csv_dir(path) -> dict[str, pd.DataFrame]:
    p = Path(path)
    data = {k: pd.read_csv(p / f) for k, f in REQUIRED.items()}
    for k, f in OPTIONAL.items():
        if (p / f).exists():
            data[k] = pd.read_csv(p / f)
    return data
