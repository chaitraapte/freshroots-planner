CREATE TABLE distributors (
    distributor_id   text PRIMARY KEY,
    distributor_name text NOT NULL,
    region           text,
    status           text NOT NULL DEFAULT 'Active'
);

CREATE TABLE skus (
    sku_id              text PRIMARY KEY,
    sku_name            text NOT NULL,
    category            text NOT NULL,
    perishability       text NOT NULL CHECK (perishability IN ('High', 'Low')),
    standard_reorder_qty integer NOT NULL CHECK (standard_reorder_qty > 0),
    unit_of_measure     text NOT NULL DEFAULT 'Units',
    distributor_id      text NOT NULL REFERENCES distributors (distributor_id),
    status              text NOT NULL DEFAULT 'Active' CHECK (status IN ('Active', 'Discontinued')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX skus_distributor_idx ON skus (distributor_id);
CREATE INDEX skus_status_idx ON skus (status);

CREATE TABLE weekly_sales (
    sku_id     text NOT NULL REFERENCES skus (sku_id),
    iso_week   text NOT NULL,
    units_sold integer NOT NULL CHECK (units_sold >= 0),
    PRIMARY KEY (sku_id, iso_week)
);
CREATE INDEX weekly_sales_week_idx ON weekly_sales (iso_week);

CREATE TABLE inventory_snapshots (
    sku_id        text NOT NULL REFERENCES skus (sku_id),
    location      text NOT NULL,
    units_on_hand integer NOT NULL,
    as_of_date    date NOT NULL,
    PRIMARY KEY (sku_id, location, as_of_date)
);
CREATE INDEX inventory_latest_idx ON inventory_snapshots (sku_id, as_of_date DESC);

CREATE TABLE calendar (
    year             integer NOT NULL,
    month            integer NOT NULL CHECK (month BETWEEN 1 AND 12),
    is_festival_month boolean NOT NULL,
    PRIMARY KEY (year, month)
);

CREATE TABLE lead_times (
    sku_id          text PRIMARY KEY REFERENCES skus (sku_id),
    lead_time_weeks numeric NOT NULL CHECK (lead_time_weeks >= 0)
);
CREATE TABLE open_pos (
    sku_id                text NOT NULL REFERENCES skus (sku_id),
    open_po_units         integer NOT NULL CHECK (open_po_units >= 0),
    expected_arrival_date date NOT NULL,
    PRIMARY KEY (sku_id, expected_arrival_date)
);
CREATE TABLE shelf_life (
    sku_id          text PRIMARY KEY REFERENCES skus (sku_id),
    shelf_life_days integer NOT NULL CHECK (shelf_life_days > 0)
);
CREATE TABLE unit_costs (
    sku_id        text PRIMARY KEY REFERENCES skus (sku_id),
    unit_cost_inr numeric NOT NULL CHECK (unit_cost_inr >= 0)
);

CREATE TABLE config_versions (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    config_json jsonb NOT NULL,
    note        text
);

CREATE TABLE runs (
    run_id            uuid PRIMARY KEY,
    started_at        timestamptz NOT NULL DEFAULT now(),
    as_of_date        date NOT NULL,
    config_version_id bigint REFERENCES config_versions (id),
    params            jsonb,
    data_fingerprint  text,
    status            text NOT NULL DEFAULT 'completed'
);

-- No foreign key to skus: history survives SKU deletion.
CREATE TABLE recommendations (
    run_id           uuid NOT NULL REFERENCES runs (run_id),
    sku_id           text NOT NULL,
    distributor_id   text,
    primary_flag     text NOT NULL,
    tags             jsonb NOT NULL DEFAULT '[]',
    weekly_demand    numeric,
    cover_weeks      numeric,
    brief_literal_qty numeric,
    recommended_qty  numeric,
    multiples        integer,
    urgency_tier     text,
    urgency_weeks    numeric,
    action           text,
    needs_review     boolean NOT NULL DEFAULT false,
    stability        text,
    reasons          jsonb,
    explanation      text,
    PRIMARY KEY (run_id, sku_id)
);

-- No foreign key to skus. A reason is mandatory whenever the human is not a plain Accept.
CREATE TABLE decisions (
    id             bigserial PRIMARY KEY,
    run_id         uuid NOT NULL REFERENCES runs (run_id),
    sku_id         text NOT NULL,
    tool_flag      text,
    tool_qty       numeric,
    human_decision text NOT NULL CHECK (human_decision IN ('Accept', 'Change quantity', 'Reject', 'Hold')),
    human_qty      numeric,
    reason         text,
    decided_by     text NOT NULL,
    decided_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (human_decision = 'Accept' OR length(btrim(coalesce(reason, ''))) > 0)
);
CREATE INDEX decisions_run_idx ON decisions (run_id);

CREATE TABLE sku_changes (
    id            bigserial PRIMARY KEY,
    changed_at    timestamptz NOT NULL DEFAULT now(),
    changed_by    text NOT NULL,
    action        text NOT NULL,
    sku_id        text NOT NULL,
    before        jsonb,
    after         jsonb,
    rows_affected jsonb
);
CREATE INDEX sku_changes_time_idx ON sku_changes (changed_at DESC, id DESC);
