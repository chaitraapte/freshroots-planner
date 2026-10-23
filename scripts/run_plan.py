"""Run the planner. --csv reads data/ directly (offline); otherwise reads Neon and stores the run."""
import argparse
import sys
from datetime import date

import _common  # noqa: F401
from _common import ROOT
from planner import db, outputs, store
from planner.config import merged
from planner.csvio import load_csv_dir
from planner.engine import GateStop, run_plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", metavar="DIR", help="offline mode: read CSVs, store nothing")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--out", default=str(ROOT / "outputs"))
    a = ap.parse_args()
    try:
        if a.csv:
            data, cfg, cfg_id, eng = load_csv_dir(a.csv), merged(), None, None
            as_of = date.fromisoformat(a.as_of or data["current_inventory"]["As_Of_Date"].max())
        else:
            eng = db.make_engine(db.get_secret("DATABASE_URL"))
            db.ensure_awake(eng, on_wait=lambda n: print(f"Waking the database (attempt {n})..."))
            with eng.connect() as conn:
                data = store.load_data(conn)
                cfg, cfg_id = store.latest_config(conn)
            as_of = date.fromisoformat(a.as_of) if a.as_of else data["current_inventory"]["As_Of_Date"].max().date()
        res = run_plan(data, as_of, cfg)
    except GateStop as e:
        sys.exit(str(e))
    except Exception as e:  # noqa: BLE001
        sys.exit("Run failed: " + db.redact(e))
    outputs.write_outputs(res, a.out, outputs.assumptions_list(res.meta["config"], data))
    if eng is not None:
        with eng.begin() as conn:
            rid = store.save_run(conn, res, cfg_id)
        print("Run stored:", rid)
    print(f"Wrote {a.out}/ (action_list.csv, exceptions.csv, draft_pos/, run_report.md)")


if __name__ == "__main__":
    main()
