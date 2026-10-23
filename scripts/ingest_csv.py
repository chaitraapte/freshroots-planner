"""Validate CSVs with the data-quality gate, then idempotently upsert into Postgres. Never deletes rows."""
import argparse
import sys
from datetime import date

import _common  # noqa: F401
from planner import db, store
from planner.config import merged
from planner.csvio import load_csv_dir
from planner.quality import run_gate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--dry-run", action="store_true", help="do everything, then roll back")
    ap.add_argument("--run-date", default=None, help="ISO date for the stale-inventory check (default: latest snapshot date)")
    a = ap.parse_args()
    data = load_csv_dir(a.folder)
    latest = data["current_inventory"]["As_Of_Date"].max()
    gate = run_gate(data, date.fromisoformat(a.run_date or latest), merged())
    for f in gate.findings:
        print(f"[{f.level}] {f.message}")
    if not gate.ok:
        sys.exit("Data-quality gate stopped the ingest. Nothing was written.")
    url = db.get_secret("DATABASE_URL_DIRECT") or db.get_secret("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL in .env")
    eng = db.make_engine(url)
    try:
        db.ensure_awake(eng, on_wait=lambda n: print(f"Waking the database (attempt {n})..."))
        with eng.connect() as conn:
            tx = conn.begin()
            rep = store.ingest(conn, data)
            tx.rollback() if a.dry_run else tx.commit()
    except Exception as e:  # noqa: BLE001
        sys.exit("Ingest failed: " + db.redact(e))
    print("\nDRY RUN (nothing written)" if a.dry_run else "\nIngested")
    print(f"{'table':22}{'inserted':>10}{'updated':>10}{'unchanged':>11}")
    for t, (i, u, n) in rep.items():
        print(f"{t:22}{i:>10}{u:>10}{n:>11}")


if __name__ == "__main__":
    main()
