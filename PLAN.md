# Build plan

The pasted brief ended mid-section 10, so section 11 (phases) was not available. Phases below are my own.

Layout: `src/planner/` (pure engine + store.py), `migrations/`, `scripts/` (migrate, ingest_csv, run_plan, scale_test),
`app/` (Streamlit), `tests/`, `data/`, `outputs/`.

Order: 1 engine + tests on DataFrames -> 2 Neon storage, migrations, ingest, SKU manager logic, DB tests
-> 3 Streamlit app -> 4 outputs, scale test, README.

Risks: no Neon credentials supplied (DB tests need TEST_DATABASE_URL); brief numbers checked against real CSVs,
mismatches are reported, never patched into tests; section 10/11 text missing.
