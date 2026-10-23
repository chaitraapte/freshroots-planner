from datetime import date

import pandas as pd

from planner.csvio import load_csv_dir
from planner.engine import run_plan
from planner.scenario import apply_preset, festival_lift

AS_OF = date(2026, 10, 19)
DATA = load_csv_dir("data")


def test_scenarios_do_not_mutate_input_and_change_flags():
    before = DATA["weekly_sales"].copy()
    d = apply_preset(DATA, "SKU005", "stockdrop", 60)
    pd.testing.assert_frame_equal(DATA["weekly_sales"], before)
    assert run_plan(d, AS_OF).recs.set_index("sku_id").loc["SKU005", "primary_flag"] == "STOCKOUT_RISK"


def test_launch_preset_lands_in_exceptions():
    r = run_plan(apply_preset(DATA, "SKU005", "launch", 60), AS_OF)
    assert "SKU005" in set(r.exceptions.sku_id)


def test_festival_lift_panel_numbers():
    lift = festival_lift(DATA, AS_OF)
    print(lift.sort_values("lift"))
    assert abs(lift["lift"].median() - 0.03) < 0.03
    assert (lift[lift["lift"] > 0.20]["SKU_ID"] == "SKU001").all()
