"""End-to-end write test: starts the real server against a real Postgres database and exercises every write path over HTTP
(decisions, SKU manager add/edit/append/retire/delete/undo/bulk, config save/reset, approved PO, AI helper).

Run it ONLY against a disposable database: it migrates, loads the sample CSVs and writes test data into the default schema.
    TEST_DATABASE_URL (from .env) is used; it must differ from DATABASE_URL. Prints only PASS/FAIL lines, never connection strings.
    python scripts/e2e_check.py
"""
import json, os, re, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from planner import db  # noqa: E402  (loads .env and refuses to run if TEST_DATABASE_URL is missing or equals DATABASE_URL)

TEST_URL = db.require_test_url()
env = dict(os.environ, DATABASE_URL=TEST_URL, DATABASE_URL_DIRECT=TEST_URL, APP_PASSCODE="123456", STORAGE="")
PY = sys.executable
OUT_DIR = str(Path(os.environ.get("TMPDIR", "/tmp")) / "freshroots_e2e_out")
PORT = 8768
U = f"http://127.0.0.1:{PORT}"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if not ok and detail else ""), flush=True)


def call(method, path, body=None, pw="123456"):
    h = {"content-type": "application/json"}
    if pw:
        h["x-passcode"] = pw
    req = urllib.request.Request(U + path, data=None if body is None else json.dumps(body).encode(), method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if "json" in r.headers.get("content-type", "") else raw.decode())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode()


def sh(*args):
    r = subprocess.run([PY, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    return r.returncode, (r.stdout + r.stderr)


t0 = time.time()
for step in (["scripts/migrate.py"], ["scripts/ingest_csv.py", "data"], ["scripts/run_plan.py", "--out", OUT_DIR]):
    code, out = sh(*step)
    check("script " + step[0].split("/")[-1] + " exits cleanly", code == 0, out[-200:])
code, out = sh("scripts/ingest_csv.py", "data")
check("re-ingest changes nothing (all unchanged)", code == 0 and re.search(r"skus\s+0\s+0\s+15", out) and re.search(r"weekly_sales\s+0\s+0\s+240", out), out[-300:])

srv = subprocess.Popen([PY, "-m", "uvicorn", "planner.webapi:app", "--app-dir", "src", "--port", str(PORT)], cwd=ROOT, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
try:
    for _ in range(40):
        try:
            urllib.request.urlopen(U + "/api/health", timeout=2); break
        except Exception:
            time.sleep(1)
    s, plan = call("GET", "/api/plan")
    check("plan reads from Neon (db mode, 15 SKUs, run saved, not stale)", s == 200 and plan["mode"] == "db" and len(plan["recs"]) == 15 and plan["stale"] is False, str(plan)[:200] if s != 200 else "")
    rid = plan["latest_run"]["run_id"]
    D = lambda sku, dec, **k: call("POST", "/api/decisions", {"run_id": rid, "sku_id": sku, "decision": dec, "by": "e2e", **k})
    check("unlock check right/wrong passcode", call("POST", "/api/auth")[0] == 200 and call("POST", "/api/auth", pw="bad")[0] == 401)
    check("approve SKU011", D("SKU011", "Accept")[0] == 200)
    check("change SKU004 to 200 without reason refused", D("SKU004", "Change quantity", qty=200)[0] == 422)
    check("change SKU004 to 200 with reason", D("SKU004", "Change quantity", qty=200, reason="Supplier MOQ")[0] == 200)
    check("reject SKU013 without reason refused", D("SKU013", "Reject")[0] == 422)
    check("reject SKU013 with reason", D("SKU013", "Reject", reason="On allocation")[0] == 200)
    check("hold SKU003 with reason", D("SKU003", "Hold", reason="Waiting for quote")[0] == 200)
    check("approve pause SKU006", D("SKU006", "Accept")[0] == 200)
    check("go with rules SKU001 (review item)", D("SKU001", "Accept")[0] == 200)
    check("decision for unknown SKU -> 404", D("SKU999", "Accept")[0] == 404)
    s, plan = call("GET", "/api/plan")
    dec = plan["decisions"]
    check("decisions map has 6 SKUs with correct kinds", len(dec) == 6 and dec["SKU004"]["human_qty"] == 200 and dec["SKU013"]["human_decision"] == "Reject" and dec["SKU003"]["human_decision"] == "Hold")
    s, sh_ = call("GET", "/api/shadow")
    check("scoreboard: 6 decisions, 50% agreement (3 accepted / 3 overrides)", sh_["total"] == 6 and abs(sh_["agreement"] - 0.5) < 1e-9, str(sh_)[:150])
    s, csv_ = call("GET", "/api/download/approved/DIST002.csv")
    check("approved PO DIST002: SKU004 at 200 only (SKU003 on hold excluded)", s == 200 and "SKU004" in csv_ and ",200," in csv_ and "SKU003" not in csv_, str(csv_)[:200])
    s, csv_ = call("GET", "/api/download/approved/DIST001.csv")
    check("approved PO DIST001: nothing approved (SKU013 rejected) -> 404", s == 404)

    # ---- SKU manager
    NEW = {"sku_name": "E2E Test Biscuit", "category": "Snack", "perishability": "Low", "standard_reorder_qty": 100, "distributor_id": "DIST001"}
    add = {"action": "add", "sku_id": "SKU700", "by": "e2e", "fields": NEW, "onhand": {"units": 250, "date": "2026-10-19"},
           "sales_csv": "SKU_ID,Week,Units_Sold\n" + "\n".join(f"SKU700,2026-W{w},{50 + w % 5}" for w in range(31, 43))}
    s, pv = call("POST", "/api/skus/preview", add)
    check("add SKU700 preview shows 12 sales rows", s == 200 and pv["diff"]["sales_rows_added"] == 12, str(pv)[:150])
    s, _ = call("GET", "/api/skus"); n0 = len(_["skus"])
    check("preview wrote nothing", n0 == 15)
    check("add SKU700 commit", call("POST", "/api/skus/commit", add)[0] == 200)
    check("duplicate add refused", call("POST", "/api/skus/commit", add)[0] == 422)
    s, k = call("GET", "/api/skus"); row = [x for x in k["skus"] if x["sku_id"] == "SKU700"]
    check("SKU700 in list with 12 weeks and on-hand 250", len(k["skus"]) == 16 and row and row[0]["weeks_of_history"] == 12 and row[0]["on_hand"] == 250)
    s, plan = call("GET", "/api/plan")
    check("plan is stale after the change, SKU700 included", plan["stale"] is True and any(r["sku_id"] == "SKU700" for r in plan["recs"]))
    check("approved PO blocked while stale (409)", call("GET", "/api/download/approved/DIST002.csv")[0] == 409)
    check("re-run planner saves a new run", call("POST", "/api/run")[0] == 200)
    s, plan = call("GET", "/api/plan"); check("re-run clears stale", plan["stale"] is False)
    check("edit SKU700 (rename + qty 120 + on-hand 300)", call("POST", "/api/skus/commit", {"action": "edit", "sku_id": "SKU700", "by": "e2e", "fields": {"sku_name": "E2E Renamed", "standard_reorder_qty": 120}, "onhand": {"units": 300, "date": "2026-10-20"}})[0] == 200)
    check("append sales W43 to SKU700", call("POST", "/api/skus/commit", {"action": "append_sales", "sku_id": "SKU700", "by": "e2e", "sales_csv": "SKU_ID,Week,Units_Sold\nSKU700,2026-W43,55"})[0] == 200)
    check("append duplicate week refused", call("POST", "/api/skus/commit", {"action": "append_sales", "sku_id": "SKU700", "by": "e2e", "sales_csv": "SKU_ID,Week,Units_Sold\nSKU700,2026-W43,55"})[0] == 422)
    check("retire SKU700", call("POST", "/api/skus/commit", {"action": "retire", "sku_id": "SKU700", "by": "e2e"})[0] == 200)
    s, plan = call("GET", "/api/plan"); check("retired SKU excluded from plan", not any(r["sku_id"] == "SKU700" for r in plan["recs"]) and any(e["SKU_ID"] == "SKU700" for e in plan["excluded"]))
    s, ch = call("GET", "/api/skus/changes"); check("change log has 4 entries so far", len(ch) == 4, str(len(ch)))
    check("undo retire", call("POST", "/api/skus/undo", {"by": "e2e"})[0] == 200)
    s, k = call("GET", "/api/skus"); st = [x for x in k["skus"] if x["sku_id"] == "SKU700"][0]["status"]
    check("SKU700 active again after undo", st == "Active")
    check("delete SKU700 permanently", call("POST", "/api/skus/commit", {"action": "delete", "sku_id": "SKU700", "by": "e2e"})[0] == 200)
    s, k = call("GET", "/api/skus"); check("SKU700 gone", len(k["skus"]) == 15)
    check("undo delete restores it with all 13 weeks", call("POST", "/api/skus/undo", {"by": "e2e"})[0] == 200)
    s, k = call("GET", "/api/skus"); row = [x for x in k["skus"] if x["sku_id"] == "SKU700"]
    check("restored SKU700: 13 weeks, on-hand 300, renamed", row and row[0]["weeks_of_history"] == 13 and row[0]["on_hand"] == 300 and row[0]["sku_name"] == "E2E Renamed", str(row)[:150])
    bulk = "SKU_ID,SKU_Name,Category,Perishability,Standard_Reorder_Qty,Unit_of_Measure,Distributor_ID,Units_On_Hand,As_Of_Date\nSKU801,Bulk A,Snack,Low,50,Units,DIST002,10,2026-10-19\nSKU802,Bad,Snack,Low,0,Units,DIST002,10,2026-10-19\n"
    s, rep = call("POST", "/api/skus/bulk-preview", {"csv": bulk}); check("bulk preview: 1 ok, 1 refused", s == 200 and [r["ok"] for r in rep] == [True, False], str(rep)[:150])
    s, rep = call("POST", "/api/skus/bulk-commit", {"csv": bulk, "by": "e2e"}); check("bulk commit writes only the valid row", s == 200 and [r["ok"] for r in rep] == [True, False])
    s, k = call("GET", "/api/skus"); check("SKU801 present, SKU802 absent", any(x["sku_id"] == "SKU801" for x in k["skus"]) and not any(x["sku_id"] == "SKU802" for x in k["skus"]))
    check("poison week refused", call("POST", "/api/skus/commit", {"action": "append_sales", "sku_id": "SKU700", "by": "e2e", "sales_csv": "SKU_ID,Week,Units_Sold\nSKU700,2026-W99,5"})[0] == 422)
    check("plan still healthy", call("GET", "/api/plan")[0] == 200)

    # ---- config
    check("config: invalid refused", call("POST", "/api/config", {"changes": {"festival_uplift_default": 99}, "note": "x"})[0] == 422)
    check("config: note required", call("POST", "/api/config", {"changes": {"festival_uplift_default": 1.1}, "note": ""})[0] == 400)
    check("config: valid save", call("POST", "/api/config", {"changes": {"festival_uplift_default": 1.1}, "note": "e2e: uplift 1.10"})[0] == 200)
    s, c = call("GET", "/api/config"); check("config history has the version, uplift now 1.1", c["config"]["festival_uplift_default"] == 1.1 and len(c["history"]) == 1)
    s, plan = call("GET", "/api/plan"); check("stale after config change", plan["stale"] is True)
    check("config: reset to defaults", call("POST", "/api/config", {"reset": True, "note": "e2e reset"})[0] == 200)
    s, c = call("GET", "/api/config"); check("uplift back to 1.25 (2 versions logged)", c["config"]["festival_uplift_default"] == 1.25 and len(c["history"]) == 2)
    check("re-run planner", call("POST", "/api/run")[0] == 200)
    s, plan = call("GET", "/api/plan"); check("final plan not stale", plan["stale"] is False)

    # ---- reads that depend on the DB, plus AI
    s, a = call("POST", "/api/ask", {"sku": "SKU004", "question": "Why this quantity?"}); check("AI ask works against Neon-backed facts", s == 200 and a["ok"], str(a)[:160])
    s, sc = call("POST", "/api/scenario", {"sku": "SKU005", "preset": "stockdrop"}); check("scenario runs", s == 200 and sc["after"]["primary_flag"] == "STOCKOUT_RISK")
    s, fe = call("POST", "/api/festival", {}); check("festival panel", s == 200 and fe["available"])
finally:
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except Exception:
        srv.kill()
passed = sum(results)
print(f"\nE2E on real Neon (test branch): {passed}/{len(results)} checks passed in {round(time.time()-t0)}s")
sys.exit(0 if passed == len(results) else 1)
