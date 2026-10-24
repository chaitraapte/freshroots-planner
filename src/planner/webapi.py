"""HTTP API for the dashboard. Thin: every number comes from the engine; every write goes through store.py."""
from __future__ import annotations

import hmac
import io
import json
import math
import os
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from . import db, explain, llm, outputs, scenario, store
from .config import DEFAULT_CONFIG, merged, validate_config
from .csvio import load_csv_dir
from .engine import GateStop, run_plan
from .limitations import LIMITATIONS

ROOT = Path(__file__).resolve().parents[2]
app = FastAPI(title="Freshroots Demand Planning Copilot", docs_url=None, redoc_url=None)

_engine = None
_awake = False
_cache: dict = {}
_cfg_warn: dict = {}
CACHE_TTL = 5.0


# ------------------------------------------------------------------ helpers
def clean(obj):
    """JSON-safe: NaN -> None, numpy -> python, DataFrame -> records."""
    if isinstance(obj, pd.DataFrame):
        return json.loads(obj.to_json(orient="records", date_format="iso"))
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        return None if np.isnan(obj) or np.isinf(obj) else float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, date)):
        return obj.isoformat()
    return obj


def use_csv(request: Request) -> bool:
    return (db.get_secret("STORAGE") or "").lower() == "csv" or not db.get_secret("DATABASE_URL") \
        or request.headers.get("x-mode", "").lower() == "csv"


def get_engine():
    global _engine, _awake
    if _engine is None:
        _engine = db.make_engine(db.get_secret("DATABASE_URL"))
    if not _awake:
        try:
            db.ensure_awake(_engine)
        except db.DatabaseUnavailable as e:
            raise HTTPException(503, {"waking": True, "message": "The database is waking up or unreachable. " + db.redact(e)})
        _awake = True
    return _engine


def require_write(request: Request):
    if use_csv(request):
        raise HTTPException(403, "Offline CSV mode is read-only.")
    code = db.get_secret("APP_PASSCODE")
    if not code:
        raise HTTPException(403, "Writes are disabled: set APP_PASSCODE on the server to enable them.")
    given = request.headers.get("x-passcode", "")
    if not hmac.compare_digest(given.encode(), code.encode()):
        raise HTTPException(401, "Wrong or missing passcode.")


def load_all(request: Request):
    """data, config, config_id, as_of, mode. Short TTL cache; any write clears it."""
    csv = use_csv(request)
    key = "csv" if csv else "db"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    if csv:
        data = load_csv_dir(ROOT / "data")
        out = (data, merged(), None, date.fromisoformat(data["current_inventory"]["As_Of_Date"].max()), "csv")
    else:
        with get_engine().connect() as conn:
            data = store.load_data(conn)
            cfg, cid = store.latest_config(conn)
        problems = validate_config(cfg)
        _cfg_warn["db"] = problems
        if problems:  # a bad saved config must never take the dashboard down: fall back to defaults and say so
            cfg = merged()
        out = (data, cfg, cid, pd.Timestamp(data["current_inventory"]["As_Of_Date"].max()).date(), "db")
    _cache[key] = (time.time(), out)
    return out


def bust():
    _cache.clear()


def gate_json(g):
    return {"stops": [vars(f) for f in g.stops], "warnings": [vars(f) for f in g.warnings],
            "info": [vars(f) for f in g.findings if f.level == "info"]}


def rec_records(recs: pd.DataFrame) -> list:
    r = recs.copy()
    r["tags"] = r["tags"].map(list)
    return clean(r)


def bad(msg, code=400):
    raise HTTPException(code, msg)


@app.exception_handler(store.ChangeRefused)
async def refused(request, exc):
    return JSONResponse({"detail": str(exc), "refused": True}, status_code=422)


@app.exception_handler(HTTPException)
async def http_exc(request, exc):
    d = exc.detail
    return JSONResponse(d if isinstance(d, dict) else {"detail": d}, status_code=exc.status_code)


@app.exception_handler(GateStop)
async def gate_stop_exc(request, exc):
    return JSONResponse({"detail": "The data-quality check stopped this: " + exc.gate.summary(), "refused": True}, status_code=422)


@app.exception_handler(Exception)
async def any_exc(request, exc):
    # full detail goes to the server log only (redacted); the browser never sees SQL or internals
    print("ERROR", request.url.path, type(exc).__name__, db.redact(exc)[:500], file=sys.stderr, flush=True)
    return JSONResponse({"detail": f"Something went wrong on the server and nothing was changed ({type(exc).__name__})."}, status_code=500)


def num(v, lo, hi, label, whole=False):
    """Validate a number from the browser: finite, in range, optionally whole."""
    if isinstance(v, bool) or v is None or v == "":
        raise store.ChangeRefused(f"{label}: enter a number.")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise store.ChangeRefused(f"{label}: enter a number.")
    if not math.isfinite(f) or not lo <= f <= hi or (whole and f != int(f)):
        raise store.ChangeRefused(f"{label} must be {'a whole number ' if whole else ''}between {lo:g} and {hi:g}.")
    return int(f) if whole else f


# ------------------------------------------------------------------ read endpoints
@app.get("/api/plan")
def plan(request: Request):
    data, cfg, cid, as_of, mode = load_all(request)
    base = {"config_problems": _cfg_warn.get(mode) or [], "mode": mode, "as_of": as_of.isoformat(), "writes_enabled": mode == "db" and bool(db.get_secret("APP_PASSCODE")),
            "server_time": time.time()}
    try:
        res = run_plan(data, as_of, cfg)
    except GateStop as e:
        return clean({**base, "gate_stop": True, "gate": gate_json(e.gate), "recs": [], "exceptions": [], "draft_pos": {}, "excluded": []})
    latest, stale, decided = None, None, {}
    if mode == "db":
        with get_engine().connect() as conn:
            latest = store.latest_run(conn)
            decided = store.decisions_for_run(conn, latest["run_id"]) if latest else {}
        stale = latest is None or latest["data_fingerprint"] != res.meta["fingerprint"] or latest["config_version_id"] != cid
    M = res.matrix
    weeks = res.meta["weeks"]
    series = {s: [None if np.isnan(v) else float(v) for v in M.loc[s, weeks].values] for s in res.recs.sku_id}
    return clean({**base, "gate_stop": False, "gate": gate_json(res.gate), "festival_month": res.meta["festival_month"],
                  "fingerprint": res.meta["fingerprint"], "config": res.meta["config"], "latest_run": latest, "stale": stale, "decisions": decided,
                  "recs": rec_records(res.recs), "exceptions": res.exceptions, "excluded": res.excluded,
                  "draft_pos": {d: df for d, df in res.draft_pos.items()}, "weeks": weeks, "series": series})


@app.get("/api/meta")
def meta(request: Request):
    data, cfg, *_ = load_all(request)
    return clean({"assumptions": outputs.assumptions_list(cfg, data), "limitations": LIMITATIONS,
                  "llm_available": llm.provider() is not None, "llm_provider": llm.provider()})


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/download/{name:path}")
def download(name: str, request: Request):
    data, cfg, cid, as_of, mode = load_all(request)
    res = run_plan(data, as_of, cfg)
    if name == "action_list.csv":
        body = outputs._flat(res.recs[outputs.ACTION_COLS]).to_csv(index=False)
    elif name == "exceptions.csv":
        body = res.exceptions.to_csv(index=False)
    elif name == "run_report.md":
        body = outputs.report_markdown(res, outputs.assumptions_list(res.meta["config"], data))
    elif name.startswith("approved/") and name.endswith(".csv"):
        if mode != "db":
            bad("Approved orders need the database (offline mode is read-only).", 403)
        d = name[len("approved/"):-4]
        if d not in res.draft_pos:
            bad("No draft PO for that distributor.", 404)
        with get_engine().connect() as conn:
            latest = store.latest_run(conn)
            decided = store.decisions_for_run(conn, latest["run_id"]) if latest else {}
        if latest is None or latest["data_fingerprint"] != res.meta["fingerprint"] or latest["config_version_id"] != cid:
            bad("The saved run is out of date. Re-run the planner, then approve again.", 409)
        lines = res.draft_pos[d].copy()
        lines["decision"] = lines["sku_id"].map(lambda k: (decided.get(k) or {}).get("human_decision", ""))
        lines["draft_quantity"] = lines["quantity"]
        lines["approved_quantity"] = [int((decided[k].get("human_qty") if decided[k]["human_decision"] == "Change quantity" else q) or 0)
                                      if k in decided and decided[k]["human_decision"] in ("Accept", "Change quantity") else 0
                                      for k, q in zip(lines["sku_id"], lines["quantity"])]
        lines["decided_by"] = lines["sku_id"].map(lambda k: (decided.get(k) or {}).get("decided_by", ""))
        lines["reason"] = lines["sku_id"].map(lambda k: (decided.get(k) or {}).get("reason") or "")
        lines = lines[lines["approved_quantity"] > 0]
        if lines.empty:
            bad("No lines are approved for this distributor yet.", 404)
        lines["status"] = "APPROVED by a human - the tool never places orders"
        body = lines[["distributor_id", "distributor_name", "sku_id", "sku_name", "draft_quantity", "approved_quantity", "decision", "decided_by", "reason", "status"]].to_csv(index=False)
    elif name.startswith("po/") and name.endswith(".csv"):
        d = name[3:-4]
        if d not in res.draft_pos:
            bad("No draft PO for that distributor.", 404)
        body = res.draft_pos[d].to_csv(index=False)
    else:
        bad("Unknown file.", 404)
    return Response(body, media_type="text/csv" if name.endswith(".csv") else "text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{(name[len("approved/"):-4] + "_approved.csv") if name.startswith("approved/") else name.split("/")[-1]}"'})


# ------------------------------------------------------------------ scenario lab
@app.post("/api/scenario")
def scenario_run(request: Request, body: dict = Body(...)):
    data, cfg, cid, as_of, mode = load_all(request)
    base = run_plan(data, as_of, cfg).recs.set_index("sku_id")
    sku = body.get("sku")
    if sku not in base.index:
        bad("Unknown SKU.")
    scen, scfg = data, cfg
    if body.get("preset"):
        if body["preset"] not in scenario.PRESETS.values():
            bad("Unknown preset.")
        scen = scenario.apply_preset(data, sku, body["preset"], float(base.loc[sku, "avg12"]))
    else:
        c = body.get("custom", {})
        last_n = num(c.get("last_n", 4), 1, 52, "Last N weeks", whole=True)
        mult = num(c.get("multiplier", 1.0), 0, 10, "Demand multiplier")
        scen = scenario.scale_recent_sales(data, sku, last_n, mult)
        if c.get("on_hand") not in (None, ""):
            scen = scenario.set_on_hand(scen, sku, num(c["on_hand"], 0, 1_000_000_000, "On-hand", whole=True))
        if c.get("lead_time") not in (None, ""):
            scen = scenario.set_lead_time(scen, sku, num(c["lead_time"], 0, 52, "Restock time"))
        if c.get("festival_off"):
            scfg = merged({**cfg, "festival_uplift_default": 1.0, "festival_uplift_by_category": {}})
    after = run_plan(scen, as_of, scfg).recs.set_index("sku_id").loc[sku]
    fields = ["primary_flag", "tags", "action", "cover_weeks", "weekly_demand", "trend", "recommended_qty", "brief_literal_qty",
              "urgency_tier", "stability", "review_reasons"]
    pick = lambda r: clean({k: (list(r[k]) if k in ("tags", "review_reasons") else r[k]) for k in fields})  # noqa: E731
    return {"before": pick(base.loc[sku]), "after": pick(after)}


@app.get("/api/scenario/presets")
def presets():
    return [{"label": k, "key": v} for k, v in scenario.PRESETS.items()]


@app.post("/api/festival")
def festival(request: Request, body: dict = Body(default={})):
    data, cfg, cid, as_of, mode = load_all(request)
    lift = scenario.festival_lift(data, as_of)
    if lift.empty:
        return {"available": False}
    cat = lift.groupby("Category").agg(median_lift=("lift", "median"), skus=("SKU_ID", "count")).reset_index()
    ov = {**cfg.get("festival_uplift_by_category", {}), **{str(k): num(v, 0.5, 3, f"Uplift for {k}") for k, v in (body.get("overrides") or {}).items()}}
    base = run_plan(data, as_of, cfg).recs.set_index("sku_id")
    what = run_plan(data, as_of, merged({**cfg, "festival_uplift_by_category": ov})).recs.set_index("sku_id")
    cmp = pd.DataFrame({"sku_id": base.index, "sku_name": base.sku_name.values, "category": base.category.values,
                        "configured_flag": base.primary_flag.values, "whatif_flag": what.primary_flag.values,
                        "whatif_action": what.action.values, "whatif_qty": what.recommended_qty.values})
    return clean({"available": True, "configured_default": cfg["festival_uplift_default"], "overrides": ov,
                  "festival_weeks": lift.festival_weeks.iloc[0], "baseline_weeks": lift.baseline_weeks.iloc[0],
                  "median_all": lift["lift"].median(), "above_20": list(lift[lift["lift"] > 0.2].SKU_ID),
                  "categories": cat, "compare": cmp})


@app.post("/api/ask")
def ask_about_sku(request: Request, body: dict = Body(...)):
    data, cfg, cid, as_of, mode = load_all(request)
    res = run_plan(data, as_of, cfg)
    r = res.recs.set_index("sku_id")
    sku = body.get("sku")
    if sku not in r.index:
        bad("Unknown SKU.")
    row = r.loc[sku]
    keep = ["sku_name", "category", "perishability", "distributor_id", "on_hand", "open_po_units", "standard_qty", "last4_avg", "avg12", "trend", "festival_uplift",
            "weekly_demand", "cover_weeks", "min_weeks", "max_weeks", "lead_time_weeks", "lead_time_source", "primary_flag", "tags", "rule_action", "action",
            "brief_literal_qty", "brief_literal_cover_after", "recommended_qty", "multiples", "recommended_cover_after", "weeks_until_below_max",
            "urgency_tier", "urgency_weeks", "stability", "review_reasons", "reasons", "explanation"]
    facts = clean({k: (list(row[k]) if isinstance(row[k], (list, tuple, np.ndarray)) else row[k]) for k in keep})
    wd, pos, std = float(row["weekly_demand"] or 0), float(row["on_hand"] + row["open_po_units"]), float(row["standard_qty"])
    if wd > 0:  # what each whole multiple of the standard pack would do to cover, so "why not two?" can be answered from engine numbers
        facts["order_options"] = [{"multiples": k, "units": int(k * std), "cover_weeks_after": round((pos + k * std) / wd, 1),
                                   "inside_healthy_range": bool(row["min_weeks"] <= (pos + k * std) / wd <= row["max_weeks"])} for k in range(1, 6)]
    facts["recent_weekly_sales"] = clean([None if np.isnan(v) else float(v) for v in res.matrix.loc[sku, res.meta["weeks"]].values])
    return llm.ask(facts, body.get("question", ""))


@app.post("/api/explain")
def explain_one(request: Request, body: dict = Body(...)):
    data, cfg, cid, as_of, mode = load_all(request)
    r = run_plan(data, as_of, cfg).recs.set_index("sku_id")
    sku = body.get("sku")
    if sku not in r.index:
        bad("Unknown SKU.")
    row = r.loc[sku].copy()
    row["sku_id"] = sku
    text, source = explain.reword(explain.build_facts(row, cfg), row["explanation"])
    return {"text": text, "source": source}


# ------------------------------------------------------------------ runs and decisions
@app.post("/api/run")
def rerun(request: Request):
    require_write(request)
    bust()
    data, cfg, cid, as_of, mode = load_all(request)
    res = run_plan(data, as_of, cfg)
    with get_engine().begin() as conn:
        rid = store.save_run(conn, res, cid)
    bust()
    return {"run_id": rid}


@app.post("/api/decisions")
def decide(request: Request, body: dict = Body(...)):
    require_write(request)
    run_id, sku = body.get("run_id"), body.get("sku_id")
    with get_engine().begin() as conn:
        rec = store.recommendation_for(conn, run_id, sku)
        if rec is None:
            bad("That SKU is not in that run.", 404)
        qty = body.get("qty")
        did = store.log_decision(conn, run_id, sku, rec["primary_flag"], rec["recommended_qty"], body.get("decision"),
                                 None if qty in (None, "") else float(qty), body.get("reason"), body.get("by") or "")
    return {"id": did}


@app.get("/api/shadow")
def shadow(request: Request):
    if use_csv(request):
        bad("The decision log needs a database connection.", 403)
    with get_engine().connect() as conn:
        s = store.shadow_stats(conn)
        log = store.decisions_table(conn).drop(columns=["run_id_text"])
    log["run_id"] = log["run_id"].astype(str)
    return clean({**s, "log": log})


# ------------------------------------------------------------------ config
@app.get("/api/config")
def config_get(request: Request):
    data, cfg, cid, as_of, mode = load_all(request)
    hist = []
    if mode == "db":
        with get_engine().connect() as conn:
            h = store.config_history(conn)
        hist = clean(h.drop(columns=["config_json"]))
    return {"config": clean(cfg), "defaults": clean(DEFAULT_CONFIG), "history": hist, "mode": mode}


@app.post("/api/config")
def config_save(request: Request, body: dict = Body(...)):
    require_write(request)
    note = (body.get("note") or "").strip()
    if not note:
        bad("A note is required.")
    data, cfg, cid, as_of, mode = load_all(request)
    if body.get("reset"):
        new = merged()
    else:
        changes = body.get("changes") or {}
        allowed = {"festival_uplift_default", "high_demand_trend", "stockout_weeks", "overstock_weeks", "target_cover_weeks",
                   "cv_threshold", "lead_time_weeks", "festival_uplift_by_category", "mad_floor_pct"}
        if set(changes) - allowed:
            bad("Not an editable setting: " + ", ".join(sorted(set(changes) - allowed)))
        new = merged({**cfg, **changes})
    problems = validate_config(new)
    if problems:
        raise store.ChangeRefused("Not saved: " + "; ".join(problems) + ".")
    with get_engine().begin() as conn:
        v = store.save_config_version(conn, new, note)
    bust()
    return {"version": v}


@app.post("/api/auth")
def auth_check(request: Request):
    require_write(request)
    return {"ok": True}


# ------------------------------------------------------------------ SKU manager
def _sales_df(text_: str | None):
    if not text_ or not text_.strip():
        return None
    try:
        return pd.read_csv(io.StringIO(text_))
    except Exception as e:  # noqa: BLE001
        raise store.ChangeRefused(f"Could not read the sales CSV: {e}")


def _change_args(body):
    oh = body.get("onhand")
    onhand = None
    if oh and oh.get("units") not in (None, ""):
        try:
            d = date.fromisoformat(str(oh.get("date") or ""))
        except ValueError:
            raise store.ChangeRefused("Pick a valid as-of date for the stock count.")
        onhand = (oh["units"], d)
    return dict(action=body.get("action"), sku_id=(body.get("sku_id") or "").strip(), fields=body.get("fields") or {},
                sales=_sales_df(body.get("sales_csv")), onhand=onhand)


@app.get("/api/skus")
def skus(request: Request):
    if use_csv(request):
        data, *_ = load_all(request)
        m = data["sku_master"]
        hist = data["weekly_sales"].groupby("SKU_ID").size()
        oh = data["current_inventory"].groupby("SKU_ID")["Units_On_Hand"].sum()
        t = pd.DataFrame({"sku_id": m.SKU_ID, "sku_name": m.SKU_Name, "category": m.Category, "perishability": m.Perishability,
                          "standard_reorder_qty": m.Standard_Reorder_Qty, "distributor_id": m.Distributor_ID, "status": "Active",
                          "on_hand": m.SKU_ID.map(oh), "weeks_of_history": m.SKU_ID.map(hist).fillna(0)})
        return clean({"skus": t, "distributors": data["distributor_master"][["Distributor_ID", "Distributor_Name"]].rename(
            columns={"Distributor_ID": "distributor_id", "Distributor_Name": "distributor_name"}), "last_undoable": None})
    with get_engine().connect() as conn:
        t = store.sku_table(conn)
        d = pd.read_sql("SELECT distributor_id, distributor_name FROM distributors ORDER BY 1", conn)
        last = store.last_undoable(conn)
    return clean({"skus": t, "distributors": d, "last_undoable": last})


@app.post("/api/skus/preview")
def sku_preview(request: Request, body: dict = Body(...)):
    require_write(request)
    data, cfg, cid, as_of, mode = load_all(request)
    a = _change_args(body)
    with get_engine().connect() as conn:
        p = store.preview_change(conn, a["action"], a["sku_id"], a["fields"], a["sales"], a["onhand"], as_of, cfg)
    return clean({"diff": p["diff"], "gate_warnings": [f.message for f in p["gate"].warnings]})


@app.post("/api/skus/commit")
def sku_commit(request: Request, body: dict = Body(...)):
    require_write(request)
    data, cfg, cid, as_of, mode = load_all(request)
    a = _change_args(body)
    with get_engine().begin() as conn:
        cid_ = store.apply_change(conn, a["action"], a["sku_id"], body.get("by") or "", a["fields"], a["sales"], a["onhand"], as_of, cfg)
    bust()
    return {"change_id": cid_}


@app.post("/api/skus/undo")
def sku_undo(request: Request, body: dict = Body(default={})):
    require_write(request)
    with get_engine().begin() as conn:
        uid = store.undo_last(conn, body.get("by") or "")
    bust()
    return {"undo_id": uid}


@app.get("/api/skus/changes")
def sku_changes(request: Request):
    if use_csv(request):
        return []
    with get_engine().connect() as conn:
        return clean(store.changes_log(conn))


def _bulk_rows(csv_text: str):
    if not (csv_text or "").strip():
        raise store.ChangeRefused("Paste or upload a CSV first.")
    try:
        df = pd.read_csv(io.StringIO(csv_text))
    except Exception:  # noqa: BLE001
        raise store.ChangeRefused("That doesn't look like a CSV file.")
    need = ["SKU_ID", "SKU_Name", "Category", "Perishability", "Standard_Reorder_Qty", "Distributor_ID", "Units_On_Hand", "As_Of_Date"]
    miss = [c for c in need if c not in df.columns]
    if miss:
        raise store.ChangeRefused(f"Bulk CSV is missing columns: {miss}")
    return df


@app.post("/api/skus/bulk-preview")
def bulk_preview(request: Request, body: dict = Body(...)):
    require_write(request)
    data, cfg, cid, as_of, mode = load_all(request)
    df = _bulk_rows(body.get("csv", ""))
    out = []
    seen = set()
    with get_engine().connect() as conn:
        for _, r in df.iterrows():
            sid = str(r.SKU_ID).strip()
            try:
                if sid in seen:
                    raise store.ChangeRefused(f"SKU_ID {sid} appears twice in the upload.")
                seen.add(sid)
                f = dict(sku_name=r.SKU_Name, category=r.Category, perishability=r.Perishability, standard_reorder_qty=r.Standard_Reorder_Qty,
                         unit_of_measure=r.get("Unit_of_Measure", "Units"), distributor_id=r.Distributor_ID)
                store.preview_change(conn, "add", sid, f, None, (int(r.Units_On_Hand), pd.to_datetime(r.As_Of_Date).date()), as_of, cfg)
                out.append({"sku_id": sid, "ok": True, "message": "ok"})
            except Exception as e:  # noqa: BLE001
                out.append({"sku_id": sid, "ok": False, "message": str(e)})
    return out


@app.post("/api/skus/bulk-commit")
def bulk_commit(request: Request, body: dict = Body(...)):
    require_write(request)
    data, cfg, cid, as_of, mode = load_all(request)
    df = _bulk_rows(body.get("csv", ""))
    results = []
    for _, r in df.iterrows():
        sid = str(r.SKU_ID).strip()
        try:
            f = dict(sku_name=r.SKU_Name, category=r.Category, perishability=r.Perishability, standard_reorder_qty=r.Standard_Reorder_Qty,
                     unit_of_measure=r.get("Unit_of_Measure", "Units"), distributor_id=r.Distributor_ID)
            with get_engine().begin() as conn:  # one transaction per SKU
                store.apply_change(conn, "add", sid, body.get("by") or "", f, None, (int(r.Units_On_Hand), pd.to_datetime(r.As_Of_Date).date()), as_of, cfg)
            results.append({"sku_id": sid, "ok": True})
        except Exception as e:  # noqa: BLE001
            results.append({"sku_id": sid, "ok": False, "message": db.redact(e)})
    bust()
    return results


# ------------------------------------------------------------------ static files (local dev only; Vercel serves /public from its CDN)
_MEDIA = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2", ".json": "application/json"}


@app.get("/{path:path}")
def static_site(path: str):
    root = (ROOT / "public").resolve()
    target = (root / path).resolve()
    if root not in target.parents and target != root:  # never serve anything outside /public
        raise HTTPException(404, "Not found")
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        raise HTTPException(404, "Not found")
    return Response(target.read_bytes(), media_type=_MEDIA.get(target.suffix, "application/octet-stream"))
