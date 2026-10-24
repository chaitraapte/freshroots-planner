# Freshroots Demand Planning Copilot (pilot)

Applies Raghav Kulkarni's documented reorder rules to SAP exports, sizes orders so cover is actually restored, and routes everything the rules cannot handle to a human. **Every output is a DRAFT.** The tool never places orders, does not forecast, and makes no claim about savings or accuracy. The engine is deterministic; an optional LLM may only reword an explanation the engine already produced.

## Dashboard and Vercel

The home page (`/`) is a React + Tailwind + shadcn landing page whose source is in `landing/`; it is built into `public/` and committed, so Vercel needs no build step. After editing it run `cd landing && npm install && npm run build`. The dashboard (`/dashboard/`) is static HTML/JS in `public/dashboard/` talking to a FastAPI function (`api/index.py`) that reuses the engine and `store.py` unchanged. It recomputes from Neon on every load and refreshes itself every 30 seconds, so it is always as current as the database.

- **Writes need a passcode.** The URL has no login. Every write endpoint (re-run, decisions, SKU manager, config) requires the `x-passcode` header to match the `APP_PASSCODE` environment variable; if that variable is unset, writes are disabled and the dashboard is read-only. This is a pilot gate, not authentication.
- **Deploy:** `vercel` from this folder. Set `DATABASE_URL` (pooled Neon string) and `APP_PASSCODE` as Vercel environment variables, optionally `DATABASE_URL_DIRECT` is not needed at runtime. Do not set `TEST_DATABASE_URL` there. Run migrate/ingest from your machine.
- **Staleness:** the app compares the data fingerprint and config version against the latest saved run, so "stale" works across serverless instances without any shared state.
- **Cold starts:** Neon and the function may take several seconds when idle; the dashboard shows "waking the database" and retries.

## Setup

1. Create a Neon project, plus a second branch named `test`.
2. Copy `.env.example` to `.env` and fill in `DATABASE_URL` (pooled), `DATABASE_URL_DIRECT` (non-pooled) and `TEST_DATABASE_URL` (the `test` branch, non-pooled, different from `DATABASE_URL`).
3. Run migrate, ingest, then the app, as below. Tests are further down.

## Quick start

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in Neon strings and APP_PASSCODE (never commit .env)
.venv/bin/python scripts/migrate.py
.venv/bin/python scripts/ingest_csv.py data/ --dry-run
.venv/bin/python scripts/ingest_csv.py data/
.venv/bin/uvicorn planner.webapi:app --app-dir src --port 8765   # then open http://localhost:8765 (home page) or http://localhost:8765/dashboard/ (dashboard)
```

Offline demo (no internet, read-only; SKU manager and decision log disabled): `STORAGE=csv .venv/bin/uvicorn planner.webapi:app --app-dir src --port 8765   # then open http://localhost:8765 (home page) or http://localhost:8765/dashboard/ (dashboard)`, or `scripts/run_plan.py --csv data`.


## Layout

- `src/planner/` engine (pure DataFrame functions: `engine.py`, `quality.py`, `explain.py`, `scenario.py`, `outputs.py`); `store.py` and `db.py` are the only modules that touch Postgres.
- `migrations/` plain SQL, applied by `scripts/migrate.py` (tracked in `schema_migrations`).
- `scripts/` migrate, ingest_csv, run_plan, scale_test. `public/dashboard/` the live HTML dashboard (no build step), `landing/` the React home page (builds into `public/`), `src/planner/webapi.py` + `api/index.py` its FastAPI backend, `legacy/` the earlier Streamlit version (not deployed). `tests/`.
- Each run writes `outputs/action_list.csv`, `exceptions.csv`, `draft_pos/<distributor>.csv`, `run_report.md`, and (with Neon) a `runs` row plus `recommendations`.

## Tests

```bash
.venv/bin/python -m pytest tests/test_engine.py tests/test_scenario.py tests/test_app_smoke.py   # no database
TEST_DATABASE_URL=<Neon "test" branch> .venv/bin/python -m pytest tests/test_db.py
```

DB tests refuse to run if `TEST_DATABASE_URL` is missing or equals `DATABASE_URL`; each session works in its own schema, dropped afterwards.

## Assumptions

| Assumption | Value (editable on Rules & config) |
|---|---|
| PO grouping key | Distributor_ID (in the case brief distributors are customers; here each SKU maps to one, and two sit outside Maharashtra/Goa) |
| Lead time | 2 weeks non-perishable, 1 week perishable |
| Shelf life | 28 days for High perishability |
| Open POs | zero unless supplied |
| Unit cost | unknown, money-at-risk shows n/a |
| Festival uplift | 1.25 for all categories in a festival month (Mar, Oct, Nov) |
| Weeks | ISO weeks; a week belongs to the month of its Thursday |
| Target cover after a stockout order | 8 weeks (perishable: 3.5) |

## Limitations

- It automates Raghav's rules; it is not a forecast and has no machine learning.
- 16 weeks of history means no seasonality can be learned and the festival uplift cannot be fitted.
- Lead time, shelf life and open POs are assumptions; money at risk is unknown without unit cost.
- Sales understate demand when stock runs out.
- SKU-level and single-warehouse; no distributor-level early warning (sales are not given by distributor).
- Data enters by CSV ingest, not a live SAP/Zoho connection.
- The data looks cleaner than real SAP exports will be.
- Single-user app with no login or permissions; fine for a pilot, not for production.
- No evidence yet that it outperforms the human planner; shadow mode exists to produce that evidence.
- Neon free tier may pause when idle, so the first request can be slow.

Nothing here claims savings or forecast accuracy.

## Things worth knowing

- **Distributor_ID is the PO grouping key.** In the case brief distributors are customers; here each SKU maps to exactly one, and DIST003/DIST004 sit in Karnataka/Gujarat, outside Freshroots' stated territory.
- **Defaults, all labelled and editable:** lead time 2 weeks (1 for perishable), shelf life 28 days for High perishability, open POs zero, unit cost unknown (money-at-risk shows n/a).
- **HIGH_DEMAND with healthy cover** is tagged WATCH rather than ordered at 1.5x; the 1.5x floor applies when stock is also below its minimum.
- **Sizing** uses whole multiples of the standard quantity. The literal brief quantity is shown next to it (e.g. SKU003 stays at ~6.0 weeks, SKU013 at ~5.3 weeks).
- **Scale:** `scripts/scale_test.py` generates synthetic 400 SKUs x 156 weeks x 1,200 distributors (and 4,000 SKUs); the engine runs in well under a second and about 2.5 s respectively on a laptop.
