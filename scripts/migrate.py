"""Apply SQL migrations. Uses DATABASE_URL_DIRECT if set (non-pooled), else DATABASE_URL."""
import sys

import _common  # noqa: F401
from _common import ROOT
from planner import db, store

if __name__ == "__main__":
    url = db.get_secret("DATABASE_URL_DIRECT") or db.get_secret("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL (or DATABASE_URL_DIRECT) in .env")
    eng = db.make_engine(url)
    try:
        db.ensure_awake(eng, on_wait=lambda n: print(f"Waking the database (attempt {n})..."))
        with eng.begin() as conn:
            applied = store.apply_migrations(conn, ROOT / "migrations")
    except Exception as e:  # noqa: BLE001
        sys.exit("Migration failed: " + db.redact(e))
    print("Applied:", applied or "nothing new")
