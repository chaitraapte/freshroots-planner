"""Storage layer (Postgres). The engine never imports this module."""
from __future__ import annotations

import json
import math
import re
import uuid
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import text

from .config import merged
from .quality import run_gate, valid_week

SKU_CHILD_TABLES = ["weekly_sales", "inventory_snapshots", "lead_times", "open_pos", "shelf_life", "unit_costs"]
EDIT_FIELDS = ["sku_name", "category", "perishability", "standard_reorder_qty", "distributor_id", "status"]


ACTIONS = {"add", "edit", "retire", "delete", "append_sales"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
MAX_INT = 1_000_000_000


def _whole(v, lo, hi, label):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ChangeRefused(f"{label} must be a whole number.")
    if not math.isfinite(f) or f != int(f) or not lo <= f <= hi:
        raise ChangeRefused(f"{label} must be a whole number between {lo:,} and {hi:,}.")
    return int(f)


class ChangeRefused(Exception):
    """A proposed change was refused before anything was written."""


def _py(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if np.isnan(v) else float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, float) and np.isnan(v):
        return None
    if v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        return v.date()
    return v


def _records(df: pd.DataFrame) -> list[dict]:
    return [{k: _py(v) for k, v in r.items()} for r in df.to_dict("records")]


# ------------------------------------------------------------------ migrations
def apply_migrations(conn, migrations_dir) -> list[str]:
    from pathlib import Path
    conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"))
    done = {r[0] for r in conn.execute(text("SELECT filename FROM schema_migrations"))}
    applied = []
    for f in sorted(Path(migrations_dir).glob("*.sql")):
        if f.name in done:
            continue
        conn.exec_driver_sql(f.read_text())
        conn.execute(text("INSERT INTO schema_migrations (filename) VALUES (:f)"), {"f": f.name})
        applied.append(f.name)
    return applied


# ------------------------------------------------------------------ loading
def load_data(conn) -> dict[str, pd.DataFrame]:
    """Read the system of record into the same CSV-shaped frames the engine uses."""
    q = lambda s: pd.read_sql(text(s), conn)  # noqa: E731
    d = {
        "sku_master": q("SELECT sku_id AS \"SKU_ID\", sku_name AS \"SKU_Name\", category AS \"Category\", perishability AS \"Perishability\", "
                        "standard_reorder_qty AS \"Standard_Reorder_Qty\", unit_of_measure AS \"Unit_of_Measure\", distributor_id AS \"Distributor_ID\", "
                        "status AS \"Status\" FROM skus ORDER BY sku_id"),
        "distributor_master": q("SELECT distributor_id AS \"Distributor_ID\", distributor_name AS \"Distributor_Name\", region AS \"Region\", status AS \"Status\" FROM distributors"),
        "weekly_sales": q("SELECT sku_id AS \"SKU_ID\", iso_week AS \"Week\", units_sold AS \"Units_Sold\" FROM weekly_sales"),
        "current_inventory": q("SELECT sku_id AS \"SKU_ID\", location AS \"Location\", units_on_hand AS \"Units_On_Hand\", as_of_date AS \"As_Of_Date\" FROM inventory_snapshots"),
        "calendar": q("SELECT year AS \"Year\", month AS \"Month\", to_char(make_date(year, month, 1), 'YYYY-MM') AS \"Year_Month\", "
                      "CASE WHEN is_festival_month THEN 'TRUE' ELSE 'FALSE' END AS \"Is_Festival_Month\" FROM calendar"),
    }
    # latest snapshot per SKU+location only
    inv = d["current_inventory"]
    if len(inv):
        inv["As_Of_Date"] = pd.to_datetime(inv["As_Of_Date"])
        idx = inv.groupby(["SKU_ID", "Location"])["As_Of_Date"].transform("max") == inv["As_Of_Date"]
        d["current_inventory"] = inv[idx].reset_index(drop=True)
    for key, sql in {
        "lead_times": "SELECT sku_id AS \"SKU_ID\", lead_time_weeks::float AS \"Lead_Time_Weeks\" FROM lead_times",
        "open_pos": "SELECT sku_id AS \"SKU_ID\", open_po_units AS \"Open_PO_Units\", expected_arrival_date AS \"Expected_Arrival_Date\" FROM open_pos",
        "shelf_life": "SELECT sku_id AS \"SKU_ID\", shelf_life_days AS \"Shelf_Life_Days\" FROM shelf_life",
        "unit_cost": "SELECT sku_id AS \"SKU_ID\", unit_cost_inr::float AS \"Unit_Cost_INR\" FROM unit_costs",
    }.items():
        df = q(sql)
        if len(df):
            d[key] = df
    return d


# ------------------------------------------------------------------ ingest (idempotent upserts)
def _upsert(conn, table, cols, pk, rows, update_cols=None, extra_set=""):
    """Returns (inserted, updated, unchanged). Never deletes."""
    if not rows:
        return 0, 0, 0
    update_cols = cols if update_cols is None else update_cols
    non_pk = [c for c in update_cols if c not in pk]
    collist = ", ".join(cols)
    vals = ", ".join(f":{c}" for c in cols)
    if non_pk:
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_pk) + extra_set
        diff = " OR ".join(f"{table}.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in non_pk)
        sql = (f"INSERT INTO {table} ({collist}) VALUES ({vals}) ON CONFLICT ({', '.join(pk)}) "
               f"DO UPDATE SET {sets} WHERE {diff} RETURNING (xmax = 0) AS inserted")
    else:
        sql = f"INSERT INTO {table} ({collist}) VALUES ({vals}) ON CONFLICT ({', '.join(pk)}) DO NOTHING RETURNING true AS inserted"
    ins = upd = 0
    # one network round trip per batch (psycopg pipeline), same transaction as the caller
    import re
    pg_sql = re.sub(r":(\w+)", r"%(\1)s", sql.replace("::", "\0")).replace("\0", "::")
    cur = conn.connection.driver_connection.cursor()
    cur.executemany(pg_sql, rows, returning=True)
    while True:
        res = cur.fetchone()
        if res is not None:
            if res[0]:
                ins += 1
            else:
                upd += 1
        if not cur.nextset():
            break
    return ins, upd, len(rows) - ins - upd


def ingest(conn, data: dict[str, pd.DataFrame]) -> dict[str, tuple[int, int, int]]:
    rep = {}
    d = data["distributor_master"]
    rep["distributors"] = _upsert(conn, "distributors", ["distributor_id", "distributor_name", "region", "status"], ["distributor_id"],
                                  _records(d.rename(columns={"Distributor_ID": "distributor_id", "Distributor_Name": "distributor_name", "Region": "region", "Status": "status"})))
    s = data["sku_master"].rename(columns={"SKU_ID": "sku_id", "SKU_Name": "sku_name", "Category": "category", "Perishability": "perishability",
                                           "Standard_Reorder_Qty": "standard_reorder_qty", "Unit_of_Measure": "unit_of_measure", "Distributor_ID": "distributor_id"})
    cols = ["sku_id", "sku_name", "category", "perishability", "standard_reorder_qty", "unit_of_measure", "distributor_id"]
    # status is not in the CSV: never overwrite an existing status (a retired SKU stays retired)
    rep["skus"] = _upsert(conn, "skus", cols, ["sku_id"], _records(s[cols]), extra_set=", updated_at = now()")
    w = data["weekly_sales"].rename(columns={"SKU_ID": "sku_id", "Week": "iso_week", "Units_Sold": "units_sold"})
    rep["weekly_sales"] = _upsert(conn, "weekly_sales", ["sku_id", "iso_week", "units_sold"], ["sku_id", "iso_week"], _records(w))
    i = data["current_inventory"].rename(columns={"SKU_ID": "sku_id", "Location": "location", "Units_On_Hand": "units_on_hand", "As_Of_Date": "as_of_date"})
    i["as_of_date"] = pd.to_datetime(i["as_of_date"]).dt.date
    rep["inventory_snapshots"] = _upsert(conn, "inventory_snapshots", ["sku_id", "location", "units_on_hand", "as_of_date"], ["sku_id", "location", "as_of_date"], _records(i))
    c = data["calendar"].copy()
    c["is_festival_month"] = c["Is_Festival_Month"].astype(str).str.upper() == "TRUE"
    c = c.rename(columns={"Year": "year", "Month": "month"})
    rep["calendar"] = _upsert(conn, "calendar", ["year", "month", "is_festival_month"], ["year", "month"], _records(c[["year", "month", "is_festival_month"]]))
    opt = {
        "lead_times": ("lead_times", {"SKU_ID": "sku_id", "Lead_Time_Weeks": "lead_time_weeks"}, ["sku_id", "lead_time_weeks"], ["sku_id"]),
        "open_pos": ("open_pos", {"SKU_ID": "sku_id", "Open_PO_Units": "open_po_units", "Expected_Arrival_Date": "expected_arrival_date"},
                     ["sku_id", "open_po_units", "expected_arrival_date"], ["sku_id", "expected_arrival_date"]),
        "shelf_life": ("shelf_life", {"SKU_ID": "sku_id", "Shelf_Life_Days": "shelf_life_days"}, ["sku_id", "shelf_life_days"], ["sku_id"]),
        "unit_cost": ("unit_costs", {"SKU_ID": "sku_id", "Unit_Cost_INR": "unit_cost_inr"}, ["sku_id", "unit_cost_inr"], ["sku_id"]),
    }
    for key, (table, ren, cols_, pk) in opt.items():
        if key in data and len(data[key]):
            df = data[key].rename(columns=ren)
            if "expected_arrival_date" in df:
                df["expected_arrival_date"] = pd.to_datetime(df["expected_arrival_date"]).dt.date
            rep[table] = _upsert(conn, table, cols_, pk, _records(df[cols_]))
    return rep


# ------------------------------------------------------------------ config, runs, recommendations
def save_config_version(conn, cfg: dict, note: str) -> int:
    return conn.execute(text("INSERT INTO config_versions (config_json, note) VALUES (CAST(:c AS jsonb), :n) RETURNING id"),
                        {"c": json.dumps(cfg), "n": note}).scalar()


def latest_config(conn) -> tuple[dict, int | None]:
    r = conn.execute(text("SELECT id, config_json FROM config_versions ORDER BY id DESC LIMIT 1")).fetchone()
    if r is None:
        return merged(), None
    return merged(r[1]), r[0]


def config_history(conn) -> pd.DataFrame:
    return pd.read_sql(text("SELECT id, created_at, note, config_json FROM config_versions ORDER BY id DESC"), conn)


def save_run(conn, result, config_version_id: int | None, params: dict | None = None) -> str:
    run_id = str(uuid.uuid4())
    meta = result.meta
    conn.execute(text("INSERT INTO runs (run_id, as_of_date, config_version_id, params, data_fingerprint, status) "
                      "VALUES (:r, :a, :c, CAST(:p AS jsonb), :f, 'completed')"),
                 {"r": run_id, "a": date.fromisoformat(meta["as_of"]), "c": config_version_id,
                  "p": json.dumps({**(params or {}), "festival_month": meta["festival_month"]}), "f": meta["fingerprint"]})
    rows = []
    for _, r in result.recs.iterrows():
        rows.append(dict(run_id=run_id, sku_id=r.sku_id, distributor_id=r.distributor_id, primary_flag=r.primary_flag,
                         tags=json.dumps(list(r.tags)), weekly_demand=_py(r.weekly_demand), cover_weeks=_py(r.cover_weeks),
                         brief_literal_qty=_py(r.brief_literal_qty), recommended_qty=_py(r.recommended_qty),
                         multiples=None if pd.isna(r.multiples) else int(r.multiples), urgency_tier=r.urgency_tier or None,
                         urgency_weeks=_py(r.urgency_weeks), action=r.action, needs_review=bool(r.needs_review),
                         stability=r.stability, reasons=json.dumps(list(r.reasons) + list(r.review_reasons)), explanation=r.explanation))
    if rows:
        conn.execute(text(
            "INSERT INTO recommendations (run_id, sku_id, distributor_id, primary_flag, tags, weekly_demand, cover_weeks, brief_literal_qty, "
            "recommended_qty, multiples, urgency_tier, urgency_weeks, action, needs_review, stability, reasons, explanation) VALUES "
            "(:run_id, :sku_id, :distributor_id, :primary_flag, CAST(:tags AS jsonb), :weekly_demand, :cover_weeks, :brief_literal_qty, "
            ":recommended_qty, :multiples, :urgency_tier, :urgency_weeks, :action, :needs_review, :stability, CAST(:reasons AS jsonb), :explanation)"), rows)
    return run_id


def list_runs(conn) -> pd.DataFrame:
    return pd.read_sql(text("SELECT run_id::text AS run_id, started_at, as_of_date, config_version_id, data_fingerprint, status FROM runs ORDER BY started_at DESC"), conn)


# ------------------------------------------------------------------ decisions
def log_decision(conn, run_id: str, sku_id: str, tool_flag: str | None, tool_qty, human_decision: str,
                 human_qty, reason: str | None, decided_by: str) -> int:
    reason = (reason or "").strip()
    if human_decision not in ("Accept", "Change quantity", "Reject", "Hold"):
        raise ChangeRefused(f"Unknown decision '{human_decision}'.")
    if human_decision == "Change quantity":
        if human_qty is None or float(human_qty) < 0:
            raise ChangeRefused("Change quantity needs a quantity of zero or more.")
        if tool_qty is not None and float(human_qty) == float(tool_qty):
            raise ChangeRefused("The new quantity equals the tool's quantity; use Accept instead.")
    if human_decision != "Accept" and not reason:
        raise ChangeRefused("A reason is required when you change, reject or hold a recommendation.")
    if not decided_by.strip():
        raise ChangeRefused("Enter who is deciding.")
    return conn.execute(text(
        "INSERT INTO decisions (run_id, sku_id, tool_flag, tool_qty, human_decision, human_qty, reason, decided_by) "
        "VALUES (:r, :s, :f, :tq, :d, :hq, :why, :by) RETURNING id"),
        {"r": run_id, "s": sku_id, "f": tool_flag, "tq": _py(tool_qty), "d": human_decision,
         "hq": _py(human_qty) if human_decision == "Change quantity" else (_py(tool_qty) if human_decision == "Accept" else None),
         "why": reason or None, "by": decided_by.strip()}).scalar()


def decisions_table(conn) -> pd.DataFrame:
    return pd.read_sql(text("SELECT d.*, d.run_id::text AS run_id_text FROM decisions d ORDER BY decided_at DESC"), conn)


def shadow_stats(conn) -> dict:
    d = pd.read_sql(text("SELECT run_id::text AS run_id, sku_id, human_decision, tool_qty, human_qty, reason, decided_at FROM decisions"), conn)
    if d.empty:
        return {"empty": True}
    d["agree"] = d["human_decision"] == "Accept"
    by_run = d.groupby("run_id").agg(decisions=("agree", "size"), agreement=("agree", "mean"), first=("decided_at", "min")).sort_values("first").reset_index()
    over = d[~d["agree"]].groupby(["human_decision", "reason"]).size().reset_index(name="count").sort_values("count", ascending=False)
    return {"empty": False, "total": len(d), "agreement": float(d["agree"].mean()), "by_run": by_run, "overrides": over}


# ------------------------------------------------------------------ SKU manager
def _snapshot(conn, sku_id: str) -> dict:
    snap = {}
    r = conn.execute(text("SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) FROM skus t WHERE sku_id = :s"), {"s": sku_id}).scalar()
    snap["skus"] = r
    for t in SKU_CHILD_TABLES:
        snap[t] = conn.execute(text(f"SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) FROM {t} t WHERE sku_id = :s"), {"s": sku_id}).scalar()
    return snap


def _counts(snap: dict) -> dict:
    return {k: len(v) for k, v in snap.items() if len(v)}


def _restore_rows(conn, table: str, rows: list):
    if rows:
        conn.execute(text(f"INSERT INTO {table} SELECT * FROM json_populate_recordset(null::{table}, CAST(:j AS json))"), {"j": json.dumps(rows)})


def _delete_sku_rows(conn, sku_id: str):
    for t in SKU_CHILD_TABLES:
        conn.execute(text(f"DELETE FROM {t} WHERE sku_id = :s"), {"s": sku_id})
    conn.execute(text("DELETE FROM skus WHERE sku_id = :s"), {"s": sku_id})


def _validate_sales(df: pd.DataFrame, sku_id: str) -> pd.DataFrame:
    df = df.rename(columns=lambda c: c.strip())
    need = {"SKU_ID", "Week", "Units_Sold"}
    if not need <= set(df.columns):
        raise ChangeRefused(f"Sales upload needs columns {sorted(need)}.")
    if (df["SKU_ID"] != sku_id).any():
        raise ChangeRefused(f"Sales upload contains rows for other SKUs; only {sku_id} may be written.")
    if df.duplicated(["SKU_ID", "Week"]).any():
        raise ChangeRefused("Sales upload has duplicate SKU-week rows.")
    units = pd.to_numeric(df["Units_Sold"], errors="coerce")
    if (units.isna() | (units < 0)).any():
        raise ChangeRefused("Sales upload has negative or non-numeric Units_Sold.")
    if ((units % 1) != 0).any() or (units > MAX_INT).any():
        raise ChangeRefused("Units_Sold must be whole numbers (no decimals).")
    if not df["Week"].astype(str).str.fullmatch(r"\d{4}-W\d{2}").all() or not df["Week"].map(valid_week).all():
        raise ChangeRefused("Week must be a real ISO week like 2026-W42 (weeks run 01 to 52 or 53).")
    df = df.copy()
    df["Units_Sold"] = units.astype(int)
    return df


def _proposed(data: dict, action: str, sku_id: str, fields: dict | None, sales: pd.DataFrame | None, onhand) -> dict:
    """Apply the change to in-memory frames, for the dry-run gate."""
    d = {k: v.copy() for k, v in data.items()}
    m = d["sku_master"]
    if action == "add":
        row = {"SKU_ID": sku_id, "SKU_Name": fields["sku_name"], "Category": fields["category"], "Perishability": fields["perishability"],
               "Standard_Reorder_Qty": fields["standard_reorder_qty"], "Unit_of_Measure": fields.get("unit_of_measure", "Units"),
               "Distributor_ID": fields["distributor_id"], "Status": "Active"}
        d["sku_master"] = pd.concat([m, pd.DataFrame([row])], ignore_index=True)
        if sales is not None and len(sales):
            d["weekly_sales"] = pd.concat([d["weekly_sales"], sales[["SKU_ID", "Week", "Units_Sold"]]], ignore_index=True)
        d["current_inventory"] = pd.concat([d["current_inventory"], pd.DataFrame([{
            "SKU_ID": sku_id, "Location": fields.get("location", "Pune Central Warehouse"), "Units_On_Hand": onhand[0], "As_Of_Date": pd.Timestamp(onhand[1])}])], ignore_index=True)
    elif action in ("edit", "retire"):
        cmap = {"sku_name": "SKU_Name", "category": "Category", "perishability": "Perishability",
                "standard_reorder_qty": "Standard_Reorder_Qty", "distributor_id": "Distributor_ID", "status": "Status"}
        for k, v in (fields or {}).items():
            m.loc[m.SKU_ID == sku_id, cmap[k]] = v
        d["sku_master"] = m
    elif action == "append_sales":
        d["weekly_sales"] = pd.concat([d["weekly_sales"], sales[["SKU_ID", "Week", "Units_Sold"]]], ignore_index=True)
    elif action == "delete":
        d["sku_master"] = m[m.SKU_ID != sku_id]
        for k in ("weekly_sales", "current_inventory", "lead_times", "open_pos", "shelf_life", "unit_cost"):
            if k in d:
                d[k] = d[k][d[k].SKU_ID != sku_id]
    return d


def preview_change(conn, action: str, sku_id: str, fields: dict | None = None, sales: pd.DataFrame | None = None,
                   onhand: tuple | None = None, run_date: date | None = None, config: dict | None = None) -> dict:
    """Validate and describe a change without writing. Raises ChangeRefused if it must not be written."""
    cfg = merged(config)
    if action not in ACTIONS:
        raise ChangeRefused(f"Unknown action '{action}'.")
    sku_id = (sku_id or "").strip()
    if not sku_id:
        raise ChangeRefused("SKU_ID is required.")
    if action == "add" and not ID_RE.match(sku_id):
        raise ChangeRefused("SKU ID must be 1 to 32 letters, digits, '-' or '_' (no spaces).")
    exists = conn.execute(text("SELECT 1 FROM skus WHERE sku_id = :s"), {"s": sku_id}).fetchone() is not None
    dists = {r[0] for r in conn.execute(text("SELECT distributor_id FROM distributors"))}
    fields = dict(fields or {})
    if action == "add":
        if exists:
            raise ChangeRefused(f"SKU_ID {sku_id} already exists.")
        for k in ("sku_name", "category", "perishability", "standard_reorder_qty", "distributor_id"):
            if fields.get(k) in (None, ""):
                raise ChangeRefused(f"{k} is required.")
        if onhand is None or onhand[0] is None or onhand[1] is None:
            raise ChangeRefused("Initial on-hand and as-of date are required.")
        for k in ("sku_name", "category"):
            if not str(fields.get(k, "")).strip():
                raise ChangeRefused(f"{k} is required.")
    elif not exists:
        raise ChangeRefused(f"SKU_ID {sku_id} does not exist.")
    for k, lab, mx in (("sku_name", "Name", 120), ("category", "Category", 60), ("unit_of_measure", "Unit", 20)):
        if k in fields:
            fields[k] = str(fields[k]).strip()
            if not fields[k]:
                raise ChangeRefused(f"{lab} can't be blank.")
            if len(fields[k]) > mx:
                raise ChangeRefused(f"{lab} is too long (max {mx} characters).")
    if "standard_reorder_qty" in fields:
        try:
            fields["standard_reorder_qty"] = _whole(fields["standard_reorder_qty"], 1, MAX_INT, "Standard reorder quantity")
        except ChangeRefused:
            raise ChangeRefused("Standard reorder quantity must be a positive whole number (1 to 1,000,000,000).")
    if onhand is not None and onhand[0] is not None:
        onhand = (_whole(onhand[0], 0, MAX_INT, "On-hand"), onhand[1])
    if "perishability" in fields and fields["perishability"] not in ("High", "Low"):
        raise ChangeRefused("Perishability must be High or Low.")
    if "status" in fields and fields["status"] not in ("Active", "Discontinued"):
        raise ChangeRefused("Status must be Active or Discontinued.")
    if "distributor_id" in fields and fields["distributor_id"] not in dists:
        raise ChangeRefused(f"Unknown distributor {fields['distributor_id']}.")
    if sales is not None and len(sales):
        sales = _validate_sales(sales, sku_id)
        if action == "append_sales":
            have = {r[0] for r in conn.execute(text("SELECT iso_week FROM weekly_sales WHERE sku_id = :s"), {"s": sku_id})}
            clash = sorted(set(sales["Week"]) & have)
            if clash:
                raise ChangeRefused(f"History is not editable; these weeks already exist: {clash[:5]}")
    elif action == "append_sales":
        raise ChangeRefused("Upload at least one week of sales.")
    if action == "retire":
        fields = {"status": "Discontinued"}

    before = _snapshot(conn, sku_id) if exists else {}
    data = load_data(conn)
    proposed = _proposed(data, action, sku_id, fields, sales, onhand)
    gate = run_gate(proposed, run_date or date.today(), cfg)
    if not gate.ok:
        raise ChangeRefused("Data-quality gate would stop the run: " + gate.summary())
    # only gate problems that this change introduces matter; pre-existing ones are reported by the refusal above too.
    diff = {"action": action, "sku_id": sku_id, "fields": fields,
            "rows_before": _counts(before), "sales_rows_added": 0 if sales is None else len(sales),
            "rows_to_remove": _counts(before) if action == "delete" else {}}
    if action == "edit":
        cur = before["skus"][0]
        diff["changes"] = {k: (cur.get(k), v) for k, v in fields.items() if cur.get(k) != v}
    return {"diff": diff, "gate": gate, "sales": sales, "fields": fields, "onhand": onhand}


def apply_change(conn, action: str, sku_id: str, changed_by: str, fields: dict | None = None,
                 sales: pd.DataFrame | None = None, onhand: tuple | None = None,
                 run_date: date | None = None, config: dict | None = None, fault_hook=None) -> int:
    """Run inside the caller's transaction (`with engine.begin() as conn`): all or nothing."""
    if not (changed_by or "").strip():
        raise ChangeRefused("Enter who is making this change.")
    p = preview_change(conn, action, sku_id, fields, sales, onhand, run_date, config)
    fields, sales, onhand = p["fields"], p["sales"], p["onhand"]
    sku_id = sku_id.strip()
    before = _snapshot(conn, sku_id)
    affected: dict = {}
    hook = fault_hook or (lambda step: None)

    if action == "add":
        conn.execute(text("INSERT INTO skus (sku_id, sku_name, category, perishability, standard_reorder_qty, unit_of_measure, distributor_id) "
                          "VALUES (:sku_id, :sku_name, :category, :perishability, :standard_reorder_qty, :unit, :distributor_id)"),
                     {"sku_id": sku_id, "unit": fields.get("unit_of_measure", "Units"), **{k: fields[k] for k in
                      ("sku_name", "category", "perishability", "standard_reorder_qty", "distributor_id")}})
        affected["skus"] = 1
        hook("after_sku")
        conn.execute(text("INSERT INTO inventory_snapshots (sku_id, location, units_on_hand, as_of_date) VALUES (:s, :l, :u, :d)"),
                     {"s": sku_id, "l": fields.get("location", "Pune Central Warehouse"), "u": int(onhand[0]), "d": onhand[1]})
        affected["inventory_snapshots"] = 1
        hook("after_inventory")
        if sales is not None and len(sales):
            conn.execute(text("INSERT INTO weekly_sales (sku_id, iso_week, units_sold) VALUES (:s, :w, :u)"),
                         [{"s": sku_id, "w": r.Week, "u": int(r.Units_Sold)} for r in sales.itertuples()])
            affected["weekly_sales"] = len(sales)
    elif action in ("edit", "retire"):
        sets = {k: v for k, v in fields.items() if k in EDIT_FIELDS}
        if sets:
            conn.execute(text("UPDATE skus SET " + ", ".join(f"{k} = :{k}" for k in sets) + ", updated_at = now() WHERE sku_id = :sku_id"),
                         {**sets, "sku_id": sku_id})
            affected["skus"] = 1
        hook("after_sku")
        if onhand is not None and onhand[0] is not None:
            loc = fields.get("location") or "Pune Central Warehouse"
            conn.execute(text("INSERT INTO inventory_snapshots (sku_id, location, units_on_hand, as_of_date) VALUES (:s, :l, :u, :d) "
                              "ON CONFLICT (sku_id, location, as_of_date) DO UPDATE SET units_on_hand = EXCLUDED.units_on_hand"),
                         {"s": sku_id, "l": loc, "u": int(onhand[0]), "d": onhand[1]})
            affected["inventory_snapshots"] = 1
    elif action == "append_sales":
        conn.execute(text("INSERT INTO weekly_sales (sku_id, iso_week, units_sold) VALUES (:s, :w, :u)"),
                     [{"s": sku_id, "w": r.Week, "u": int(r.Units_Sold)} for r in sales.itertuples()])
        affected["weekly_sales"] = len(sales)
    elif action == "delete":
        for t in SKU_CHILD_TABLES + ["skus"]:
            n = conn.execute(text(f"DELETE FROM {t} WHERE sku_id = :s"), {"s": sku_id}).rowcount
            if n:
                affected[t] = n
            hook(f"after_{t}")
    after = _snapshot(conn, sku_id)
    cid = conn.execute(text("INSERT INTO sku_changes (changed_by, action, sku_id, before, after, rows_affected) "
                            "VALUES (:by, :a, :s, CAST(:b AS jsonb), CAST(:af AS jsonb), CAST(:r AS jsonb)) RETURNING id"),
                       {"by": changed_by.strip(), "a": action, "s": sku_id, "b": json.dumps(before), "af": json.dumps(after),
                        "r": json.dumps(affected)}).scalar()
    return cid


def last_undoable(conn) -> dict | None:
    r = conn.execute(text(
        "SELECT id, changed_at, changed_by, action, sku_id FROM sku_changes c WHERE action <> 'undo' AND NOT EXISTS "
        "(SELECT 1 FROM sku_changes u WHERE u.action = 'undo' AND (u.after->>'undoes')::bigint = c.id) ORDER BY id DESC LIMIT 1")).fetchone()
    return dict(r._mapping) if r else None


def undo_last(conn, changed_by: str) -> int:
    """Restore the most recent change from its before-image; the undo is itself logged."""
    last = last_undoable(conn)
    if last is None:
        raise ChangeRefused("Nothing to undo.")
    row = conn.execute(text("SELECT before, action FROM sku_changes WHERE id = :i"), {"i": last["id"]}).fetchone()
    before, action, sku_id = row[0], row[1], last["sku_id"]
    current = _snapshot(conn, sku_id)
    _delete_sku_rows(conn, sku_id)
    _restore_rows(conn, "skus", before.get("skus", []))
    for t in SKU_CHILD_TABLES:
        _restore_rows(conn, t, before.get(t, []))
    after = _snapshot(conn, sku_id)
    return conn.execute(text("INSERT INTO sku_changes (changed_by, action, sku_id, before, after, rows_affected) "
                             "VALUES (:by, 'undo', :s, CAST(:b AS jsonb), CAST(:af AS jsonb), CAST(:r AS jsonb)) RETURNING id"),
                        {"by": changed_by.strip() or "unknown", "s": sku_id, "b": json.dumps(current),
                         "af": json.dumps({**after, "undoes": last["id"]}), "r": json.dumps({"undid": action})}).scalar()


def changes_log(conn) -> pd.DataFrame:
    return pd.read_sql(text("SELECT id, changed_at, changed_by, action, sku_id, rows_affected FROM sku_changes ORDER BY id DESC"), conn)


def sku_table(conn) -> pd.DataFrame:
    return pd.read_sql(text(
        "SELECT s.sku_id, s.sku_name, s.category, s.perishability, s.standard_reorder_qty, s.distributor_id, s.status, "
        "(SELECT units_on_hand FROM inventory_snapshots i WHERE i.sku_id = s.sku_id ORDER BY as_of_date DESC LIMIT 1) AS on_hand, "
        "(SELECT count(*) FROM weekly_sales w WHERE w.sku_id = s.sku_id) AS weeks_of_history FROM skus s ORDER BY s.sku_id"), conn)


def latest_run(conn) -> dict | None:
    r = conn.execute(text("SELECT run_id::text, started_at, as_of_date, config_version_id, data_fingerprint FROM runs ORDER BY started_at DESC LIMIT 1")).fetchone()
    return dict(r._mapping) if r else None


def recommendation_for(conn, run_id: str, sku_id: str) -> dict | None:
    r = conn.execute(text("SELECT primary_flag, recommended_qty, action FROM recommendations WHERE run_id = CAST(:r AS uuid) AND sku_id = :s"),
                     {"r": run_id, "s": sku_id}).fetchone()
    return dict(r._mapping) if r else None


def decisions_for_run(conn, run_id: str) -> dict:
    """Latest decision per SKU for one run: {sku_id: {...}}."""
    df = pd.read_sql(text("SELECT DISTINCT ON (sku_id) sku_id, human_decision, human_qty, tool_qty, reason, decided_by, decided_at "
                          "FROM decisions WHERE run_id = CAST(:r AS uuid) ORDER BY sku_id, decided_at DESC, id DESC"), conn, params={"r": run_id})
    return {r["sku_id"]: {k: _py(v) if not isinstance(v, pd.Timestamp) else v.isoformat() for k, v in r.items() if k != "sku_id"} for r in df.to_dict("records")}
