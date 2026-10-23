"""Freshroots Demand Planning Copilot. Everything shown is a DRAFT a human approves or overrides."""
import io
import os
import sys
from datetime import date
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from planner import db, explain, outputs, scenario, store  # noqa: E402
from planner.config import DEFAULT_CONFIG, merged  # noqa: E402
from planner.csvio import load_csv_dir  # noqa: E402
from planner.engine import GateStop, run_plan  # noqa: E402

st.set_page_config(page_title="Freshroots Demand Planning Copilot", layout="wide")

PAGES = ["Action list", "Draft POs", "Exceptions", "Scenario lab", "SKU manager", "Decisions & shadow mode",
         "Data quality", "Assumptions & limitations", "Rules & config"]


# ------------------------------------------------------------------ storage mode
def offline() -> bool:
    return (db.get_secret("STORAGE") or "").lower() == "csv" or not db.get_secret("DATABASE_URL")


@st.cache_resource
def get_engine():
    return db.make_engine(db.get_secret("DATABASE_URL"))


def connect_db():
    """Friendly cold-start handling for Neon; returns engine or None (and sets an error message)."""
    eng = get_engine()
    try:
        with st.spinner("Waking the database (Neon may take a few seconds after being idle)..."):
            db.ensure_awake(eng)
        return eng
    except db.DatabaseUnavailable as e:
        st.session_state["db_error"] = str(e)
        return None


def load_all():
    """Returns data, config, config_id, as_of."""
    if st.session_state.get("mode") == "csv":
        data = load_csv_dir(ROOT / "data")
        as_of = date.fromisoformat(data["current_inventory"]["As_Of_Date"].max())
        return data, merged(st.session_state.get("cfg_override")), None, as_of
    eng = get_engine()
    with eng.connect() as conn:
        data = store.load_data(conn)
        cfg, cid = store.latest_config(conn)
    as_of = data["current_inventory"]["As_Of_Date"].max().date()
    return data, cfg, cid, as_of


def init_state():
    if "mode" in st.session_state:
        return
    st.session_state["mode"] = "csv" if offline() else "db"
    if st.session_state["mode"] == "db" and connect_db() is None:
        st.session_state["mode"] = "csv"
        st.session_state["fell_back"] = True
    rerun_planner(save=True)


def rerun_planner(save: bool):
    try:
        data, cfg, cid, as_of = load_all()
        res = run_plan(data, as_of, cfg)
        st.session_state.update(data=data, cfg=cfg, cfg_id=cid, as_of=as_of, res=res, stale=False, gate_stop=None, run_id=None)
        outputs.write_outputs(res, ROOT / "outputs", outputs.assumptions_list(res.meta["config"], data))
        if save and st.session_state["mode"] == "db":
            with get_engine().begin() as conn:
                st.session_state["run_id"] = store.save_run(conn, res, cid)
    except GateStop as e:
        st.session_state["gate_stop"] = e.gate
    except Exception as e:  # noqa: BLE001
        st.session_state["fatal"] = db.redact(e)


def header(title):
    st.title(title)
    mode = st.session_state["mode"]
    if mode == "db":
        st.success("Connected to Neon (system of record)", icon="🟢")
    else:
        msg = "Offline CSV mode: read-only. The SKU manager and decision log are disabled."
        if st.session_state.get("fell_back"):
            msg += " Could not reach Neon: " + st.session_state.get("db_error", "")
        st.warning(msg, icon="📄")
    if st.session_state.get("stale"):
        st.error("Master data changed since this action list was produced. The list below is STALE until you click Re-run planner.")
    if st.session_state.get("gate_stop"):
        st.error("Data-quality gate stopped the run: " + st.session_state["gate_stop"].summary())
    gate = st.session_state.get("res") and st.session_state["res"].gate
    if gate:
        for f in gate.warnings:
            st.warning(f.message)
    st.caption("All output is a DRAFT. The tool applies Raghav's documented rules; it does not forecast and never places orders.")


# ------------------------------------------------------------------ small helpers
def fmt(x, d=1):
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:,.{d}f}"


def sparkline(res, sku_id, row):
    M = res.matrix
    s = M.loc[sku_id].dropna()
    df = pd.DataFrame({"week": s.index, "units": s.values})
    base = alt.Chart(df).encode(x=alt.X("week:N", axis=alt.Axis(labelAngle=-90, title=None)), y=alt.Y("units:Q", title=None, scale=alt.Scale(zero=False)))
    line = base.mark_line(point=True)
    rules = alt.Chart(pd.DataFrame({"v": [row.last4_avg, row.avg12], "k": ["4-week avg", "12-week avg"]})).mark_rule(strokeDash=[4, 3]).encode(
        y="v:Q", color=alt.Color("k:N", legend=alt.Legend(orient="bottom", title=None)))
    return (line + rules).properties(height=130)


def badge(flag):
    return {"STOCKOUT_RISK": "🔴 STOCKOUT_RISK", "OVERSTOCK_RISK": "🟠 OVERSTOCK_RISK", "OK": "🟢 OK"}.get(flag, flag)


def sku_expander(res, r, allow_decision):
    title = f"{r.sku_id} - {r.sku_name} | {badge(r.primary_flag)} {' + HIGH_DEMAND' if 'HIGH_DEMAND' in r.tags else ''} | {r.action} | {r.stability}"
    with st.expander(title):
        c1, c2 = st.columns([3, 2])
        with c1:
            st.markdown(f"**Why:** {'; '.join(r.reasons) or 'No rule fired.'}")
            facts = explain.build_facts(r, st.session_state["cfg"])
            text, source = r.explanation, "template"
            if st.session_state.get("use_llm"):
                text, source = explain.reword(facts, r.explanation)
            st.write(text)
            st.caption(f"Explanation source: {source}")
            if r.review_reasons:
                st.warning("Needs a human: " + "; ".join(r.review_reasons))
            m = pd.DataFrame({
                "Metric": ["On hand", "Open PO units", "Last-4-week average", "12-week average", "Trend", "Festival uplift", "Weekly demand used",
                           "Cover (weeks)", "Allowed cover (weeks)", "Lead time (weeks, " + r.lead_time_source + ")", "Urgency (cover - lead time)",
                           "Brief literal qty", "Cover after literal qty", "Recommended qty", "Cover after recommended"],
                "Value": [fmt(r.on_hand, 0), fmt(r.open_po_units, 0), fmt(r.last4_avg), fmt(r.avg12), "n/a" if np.isnan(r.trend) else f"{r.trend * 100:+.1f}%",
                          fmt(r.festival_uplift, 2), fmt(r.weekly_demand), fmt(r.cover_weeks), f"{r.min_weeks:g} to {r.max_weeks:g}", fmt(r.lead_time_weeks, 0),
                          f"{fmt(r.urgency_weeks)} ({r.urgency_tier or '-'})", fmt(r.brief_literal_qty, 0), fmt(r.brief_literal_cover_after),
                          f"{fmt(r.recommended_qty, 0)} ({fmt(r.multiples, 0)} x {fmt(r.standard_qty, 0)})", fmt(r.recommended_cover_after)]})
            st.dataframe(m, hide_index=True, width="stretch")
        with c2:
            st.altair_chart(sparkline(res, r.sku_id, r), width="stretch")
        if allow_decision:
            decision_form(r)


def decision_form(r):
    run_id = st.session_state.get("run_id")
    if not run_id:
        return
    with st.form(f"dec_{r.sku_id}"):
        st.markdown("**Your decision on this draft line**")
        c = st.columns([1, 1, 2, 1])
        dec = c[0].selectbox("Decision", ["Accept", "Change quantity", "Reject", "Hold"], key=f"d_{r.sku_id}")
        qty = c[1].number_input("New quantity", min_value=0, value=int(r.recommended_qty) if not np.isnan(r.recommended_qty) else 0, key=f"q_{r.sku_id}")
        why = c[2].text_input("Reason (required unless Accept)", key=f"w_{r.sku_id}")
        by = c[3].text_input("Decided by", key=f"b_{r.sku_id}")
        if st.form_submit_button("Log decision"):
            try:
                with get_engine().begin() as conn:
                    store.log_decision(conn, run_id, r.sku_id, r.primary_flag, None if np.isnan(r.recommended_qty) else float(r.recommended_qty),
                                       dec, float(qty), why, by)
                st.success("Decision logged.")
            except store.ChangeRefused as e:
                st.error(str(e))


# ------------------------------------------------------------------ pages
def page_actions():
    header("Action list")
    res = st.session_state.get("res")
    c0, c1 = st.columns([1, 4])
    if c0.button("Re-run planner", type="primary"):
        rerun_planner(save=True)
        st.rerun()
    st.session_state["use_llm"] = c1.toggle("Reword explanations with Claude (optional; numbers are verified, falls back to template)",
                                            value=False, disabled=not db.get_secret("ANTHROPIC_API_KEY"))
    if res is None:
        return
    r = res.recs
    f = st.columns(4)
    flag = f[0].multiselect("Flag", sorted(r.primary_flag.unique()))
    urg = f[1].multiselect("Urgency", [u for u in ["CRITICAL", "HIGH", "MEDIUM", "LOW"] if u in set(r.urgency_tier)])
    dist = f[2].multiselect("Distributor", sorted(r.distributor_id.unique()))
    act = f[3].multiselect("Action", sorted(r.action.unique()))
    v = r.copy()
    if flag: v = v[v.primary_flag.isin(flag)]
    if urg: v = v[v.urgency_tier.isin(urg)]
    if dist: v = v[v.distributor_id.isin(dist)]
    if act: v = v[v.action.isin(act)]
    m = st.columns(4)
    m[0].metric("Stockout risk", int((r.primary_flag == "STOCKOUT_RISK").sum()))
    m[1].metric("Overstock risk", int((r.primary_flag == "OVERSTOCK_RISK").sum()))
    m[2].metric("Routed to a human", int(r.needs_review.sum()))
    m[3].metric("Unstable verdicts", int((r.stability == "Unstable").sum()))
    show = v[["sku_id", "sku_name", "distributor_id", "primary_flag", "tags", "action", "urgency_tier", "urgency_weeks", "cover_weeks",
              "brief_literal_qty", "recommended_qty", "stability"]].copy()
    show["tags"] = show["tags"].map(", ".join)
    st.dataframe(show, hide_index=True, width="stretch")
    st.subheader("Details by SKU")
    for _, row in v[(v.primary_flag != "OK") | v.needs_review | (v.rule_action != "NONE")].iterrows():
        sku_expander(res, row, allow_decision=st.session_state["mode"] == "db")
    with st.expander("SKUs with no flag"):
        for _, row in v[(v.primary_flag == "OK") & ~v.needs_review & (v.rule_action == "NONE")].iterrows():
            sku_expander(res, row, allow_decision=False)


def page_pos():
    header("Draft purchase orders")
    res = st.session_state.get("res")
    if res is None:
        return
    st.caption("Lines marked REVIEW or OK are not included. Grouping key is Distributor_ID (assumption; see Assumptions & limitations).")
    if not res.draft_pos:
        st.info("No order lines are ready. Lines that need review appear on the Action list and Exceptions pages.")
    for d, df in res.draft_pos.items():
        st.subheader(f"{d} - {df['distributor_name'].iloc[0]}")
        st.markdown("**DRAFT - requires human approval**")
        st.dataframe(df[["sku_id", "sku_name", "multiples", "quantity", "reason"]], hide_index=True, width="stretch")
        st.download_button(f"Download {d}.csv", df.to_csv(index=False), file_name=f"{d}.csv", mime="text/csv", key=f"dl_{d}")
    held = res.recs[(res.recs.rule_action == "ORDER") & res.recs.needs_review]
    if len(held):
        st.subheader("Order lines held back for review")
        st.dataframe(held[["sku_id", "sku_name", "distributor_id", "recommended_qty", "review_reasons"]].assign(
            review_reasons=held.review_reasons.map("; ".join)), hide_index=True, width="stretch")


def page_exceptions():
    header("Exceptions queue")
    res = st.session_state.get("res")
    if res is None:
        return
    st.caption("The rules cannot safely handle these. A human decides; the tool does not guess.")
    st.dataframe(res.exceptions, hide_index=True, width="stretch") if len(res.exceptions) else st.info("No SKU hit an exception rule.")
    uns = res.recs[res.recs.stability == "Unstable"]
    if len(uns):
        st.subheader("Verdict depends on how the rules are interpreted")
        st.dataframe(uns[["sku_id", "sku_name", "primary_flag"]].assign(readings=uns.review_reasons.map(
            lambda x: "; ".join(y for y in x if "interpreted" in y))), hide_index=True, width="stretch")


def page_scenario():
    header("Scenario lab")
    st.caption("Scenarios run in memory only and never write to the database.")
    if st.session_state.get("res") is None:
        return
    data, res, as_of, cfg = st.session_state["data"], st.session_state["res"], st.session_state["as_of"], st.session_state["cfg"]
    sku = st.selectbox("SKU", list(res.recs.sku_id), format_func=lambda s: f"{s} - {res.recs.set_index('sku_id').loc[s, 'sku_name']}")
    base_row = res.recs.set_index("sku_id").loc[sku]
    mode = st.radio("Scenario", ["Preset", "Custom"], horizontal=True)
    scen, scen_cfg = data, dict(cfg)
    if mode == "Preset":
        p = st.selectbox("Preset", list(scenario.PRESETS))
        scen = scenario.apply_preset(data, sku, scenario.PRESETS[p], base_row.avg12)
    else:
        c = st.columns(4)
        n = c[0].number_input("Apply to last N weeks", 1, 16, 4)
        mult = c[1].number_input("Demand multiplier", 0.0, 5.0, 1.0, 0.05)
        oh = c[2].number_input("On-hand", 0, 10_000_000, int(base_row.on_hand))
        lead = c[3].number_input("Lead time (weeks)", 0.0, 52.0, float(base_row.lead_time_weeks), 0.5)
        fest_off = st.checkbox("Turn festival uplift off")
        scen = scenario.scale_recent_sales(data, sku, int(n), mult)
        scen = scenario.set_on_hand(scen, sku, oh)
        scen = scenario.set_lead_time(scen, sku, lead)
        if fest_off:
            scen_cfg = merged({**cfg, "festival_uplift_default": 1.0, "festival_uplift_by_category": {}})
    try:
        after = run_plan(scen, as_of, scen_cfg).recs.set_index("sku_id").loc[sku]
    except GateStop as e:
        st.error(e.gate.summary())
        return
    rows = [("Primary flag", "primary_flag", None), ("Tags", "tags", None), ("Action", "action", None), ("Cover (weeks)", "cover_weeks", 1),
            ("Weekly demand", "weekly_demand", 1), ("Trend", "trend", 3), ("Recommended qty", "recommended_qty", 0),
            ("Brief literal qty", "brief_literal_qty", 0), ("Urgency", "urgency_tier", None), ("Stability", "stability", None)]
    out = []
    for label, k, d in rows:
        a, b = base_row[k], after[k]
        if k == "tags":
            a, b = ", ".join(a) or "-", ", ".join(b) or "-"
        elif d is not None:
            a, b = fmt(a, d), fmt(b, d)
        out.append({"": label, "Before": a, "After": b})
    st.dataframe(pd.DataFrame(out), hide_index=True, width="stretch")
    if len(after.review_reasons):
        st.warning("After scenario, needs a human: " + "; ".join(after.review_reasons))

    st.divider()
    st.subheader("Festival uplift: configured vs what the data shows")
    st.warning("Three festival weeks is a signal, not evidence. The 25% uplift is Raghav's rule; the data-led lift is shown only so you can judge it.")
    lift = scenario.festival_lift(data, as_of)
    if len(lift):
        cat = lift.groupby("Category").agg(data_led_median_lift=("lift", "median"), skus=("SKU_ID", "count")).reset_index()
        st.caption(f"Festival weeks {lift.festival_weeks.iloc[0]} vs non-festival weeks {lift.baseline_weeks.iloc[0]}. "
                   f"Median across all SKUs: {lift['lift'].median() * 100:+.1f}%. SKUs above +20%: {', '.join(lift[lift['lift'] > .2].SKU_ID) or 'none'}.")
        ov = dict(cfg.get("festival_uplift_by_category", {}))
        cols = st.columns(len(cat))
        for i, rr in cat.iterrows():
            ov[rr.Category] = cols[i].number_input(f"{rr.Category} uplift (data: {rr.data_led_median_lift * 100:+.0f}%)", 1.0, 2.0,
                                                   float(ov.get(rr.Category, cfg["festival_uplift_default"])), 0.05, key=f"up_{rr.Category}")
        what_if = run_plan(data, as_of, merged({**cfg, "festival_uplift_by_category": ov})).recs.set_index("sku_id")
        cmp = pd.DataFrame({"Configured flag": res.recs.set_index("sku_id").primary_flag, "With your uplifts": what_if.primary_flag,
                            "Action": what_if.action, "Recommended qty": what_if.recommended_qty})
        cmp["Changed"] = np.where(cmp["Configured flag"] != cmp["With your uplifts"], "changed", "")
        st.dataframe(cmp, width="stretch")
        st.caption("This is a what-if only. To change the live uplift, save a new version on the Rules & config page.")
    else:
        st.info("No festival-flagged weeks in the data.")


def page_sku_manager():
    header("SKU manager")
    if st.session_state["mode"] != "db":
        st.error("Offline CSV mode is read-only. The SKU manager needs a Neon connection.")
        return
    eng = get_engine()
    with eng.connect() as conn:
        tbl = store.sku_table(conn)
        dists = pd.read_sql("SELECT distributor_id, distributor_name FROM distributors ORDER BY 1", conn)
        last = store.last_undoable(conn)
    who = st.text_input("Your name (recorded on every change)", key="who")

    def commit(preview_fn, apply_fn, label):
        """preview -> diff -> explicit confirm -> one transaction."""
        try:
            with eng.connect() as conn:
                p = preview_fn(conn)
        except store.ChangeRefused as e:
            st.error("Refused, nothing written: " + str(e))
            return
        st.markdown("**Preview (nothing is written yet)**")
        st.json(p["diff"], expanded=True)
        st.success("Data-quality gate dry run on the proposed state: passed" + (f" (warnings: {p['gate'].summary()})" if p["gate"].warnings else ""))
        if st.button(f"Confirm: {label}", type="primary", key=f"confirm_{label}"):
            try:
                with eng.begin() as conn:
                    apply_fn(conn)
                st.session_state["stale"] = True
                st.success("Done in one transaction. Re-run the planner to refresh the action list.")
                st.rerun()
            except store.ChangeRefused as e:
                st.error("Refused, nothing written: " + str(e))
            except Exception as e:  # noqa: BLE001
                st.error("The change failed and was rolled back completely: " + db.redact(e))

    tabs = st.tabs(["All SKUs", "Add", "Edit / retire", "Append sales", "Bulk import", "Permanently delete", "Undo & change log"])
    with tabs[0]:
        c = st.columns(4)
        s1 = c[0].multiselect("Status", ["Active", "Discontinued"])
        s2 = c[1].multiselect("Category", sorted(tbl.category.unique()))
        s3 = c[2].multiselect("Distributor", sorted(tbl.distributor_id.unique()))
        short = c[3].checkbox("Short history (under 12 weeks)")
        v = tbl
        if s1: v = v[v.status.isin(s1)]
        if s2: v = v[v.category.isin(s2)]
        if s3: v = v[v.distributor_id.isin(s3)]
        if short: v = v[v.weeks_of_history < 12]
        st.dataframe(v, hide_index=True, width="stretch")
    with tabs[1]:
        with st.form("add"):
            c = st.columns(3)
            sid = c[0].text_input("SKU_ID")
            name = c[1].text_input("Name")
            cat = c[2].text_input("Category")
            c = st.columns(4)
            per = c[0].selectbox("Perishability", ["Low", "High"])
            qty = c[1].number_input("Standard reorder qty", 0, 10_000_000, 100)
            uom = c[2].text_input("Unit", "Units")
            dist = c[3].selectbox("Distributor", dists.distributor_id)
            c = st.columns(2)
            oh = c[0].number_input("Initial on-hand", 0, 10_000_000, 0)
            asof = c[1].date_input("As-of date", st.session_state["as_of"])
            up = st.file_uploader("Weekly sales CSV (SKU_ID, Week, Units_Sold), optional", type="csv")
            paste = st.text_area("...or paste CSV rows (with header), optional")
            go = st.form_submit_button("Preview")
        if go:
            sales = None
            try:
                if up is not None:
                    sales = pd.read_csv(up)
                elif paste.strip():
                    sales = pd.read_csv(io.StringIO(paste))
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not read the sales data: {e}")
            st.session_state["add_args"] = dict(action="add", sku_id=sid, fields=dict(sku_name=name, category=cat, perishability=per, standard_reorder_qty=qty,
                                                unit_of_measure=uom, distributor_id=dist), sales=sales, onhand=(int(oh), asof))
        if "add_args" in st.session_state:
            a = st.session_state["add_args"]
            commit(lambda c: store.preview_change(c, a["action"], a["sku_id"], a["fields"], a["sales"], a["onhand"], st.session_state["as_of"], st.session_state["cfg"]),
                   lambda c: store.apply_change(c, a["action"], a["sku_id"], who, a["fields"], a["sales"], a["onhand"], st.session_state["as_of"], st.session_state["cfg"]),
                   f"add {a['sku_id']}")
            if not a["sales"] is not None:
                st.caption("With no sales upload, the new SKU lands in Exceptions as 'no history'.")
    with tabs[2]:
        sid = st.selectbox("SKU", tbl.sku_id, key="edit_sku")
        cur = tbl[tbl.sku_id == sid].iloc[0]
        with st.form("edit"):
            c = st.columns(3)
            name = c[0].text_input("Name", cur.sku_name)
            cat = c[1].text_input("Category", cur.category)
            per = c[2].selectbox("Perishability", ["Low", "High"], index=["Low", "High"].index(cur.perishability))
            c = st.columns(4)
            qty = c[0].number_input("Standard reorder qty", 0, 10_000_000, int(cur.standard_reorder_qty))
            dist = c[1].selectbox("Distributor", dists.distributor_id, index=list(dists.distributor_id).index(cur.distributor_id))
            status = c[2].selectbox("Status", ["Active", "Discontinued"], index=["Active", "Discontinued"].index(cur.status))
            oh = c[3].number_input("On-hand (new snapshot)", 0, 10_000_000, int(cur.on_hand) if pd.notna(cur.on_hand) else 0)
            asof = st.date_input("Snapshot as-of date", st.session_state["as_of"])
            st.caption("Existing sales history is not editable here. Retire = status Discontinued (history kept).")
            go = st.form_submit_button("Preview edit")
        if go:
            fields = dict(sku_name=name, category=cat, perishability=per, standard_reorder_qty=qty, distributor_id=dist, status=status)
            st.session_state["edit_args"] = dict(action="edit", sku_id=sid, fields=fields,
                                                 onhand=(int(oh), asof) if (pd.isna(cur.on_hand) or int(oh) != int(cur.on_hand)) else None)
        if "edit_args" in st.session_state and st.session_state["edit_args"]["sku_id"] == sid:
            a = st.session_state["edit_args"]
            commit(lambda c: store.preview_change(c, a["action"], a["sku_id"], a["fields"], None, a["onhand"], st.session_state["as_of"], st.session_state["cfg"]),
                   lambda c: store.apply_change(c, a["action"], a["sku_id"], who, a["fields"], None, a["onhand"], st.session_state["as_of"], st.session_state["cfg"]),
                   f"edit {sid}")
        if cur.status == "Active" and st.button(f"Retire {sid} (status = Discontinued, history kept)"):
            st.session_state["retire_sid"] = sid
        if st.session_state.get("retire_sid") == sid:
            commit(lambda c: store.preview_change(c, "retire", sid, None, None, None, st.session_state["as_of"], st.session_state["cfg"]),
                   lambda c: store.apply_change(c, "retire", sid, who, None, None, None, st.session_state["as_of"], st.session_state["cfg"]),
                   f"retire {sid}")
    with tabs[3]:
        sid = st.selectbox("SKU", tbl.sku_id, key="app_sku")
        up = st.file_uploader("New weeks (SKU_ID, Week, Units_Sold). Existing weeks cannot be overwritten.", type="csv", key="app_up")
        if up is not None:
            sales = pd.read_csv(up)
            commit(lambda c: store.preview_change(c, "append_sales", sid, None, sales, None, st.session_state["as_of"], st.session_state["cfg"]),
                   lambda c: store.apply_change(c, "append_sales", sid, who, None, sales, None, st.session_state["as_of"], st.session_state["cfg"]),
                   f"append sales to {sid}")
    with tabs[4]:
        st.caption("Upload a CSV of new SKUs: SKU_ID, SKU_Name, Category, Perishability, Standard_Reorder_Qty, Unit_of_Measure, Distributor_ID, "
                   "Units_On_Hand, As_Of_Date. Each row is validated before anything is written; valid rows are written one transaction per SKU.")
        up = st.file_uploader("New SKUs CSV", type="csv", key="bulk")
        if up is not None:
            bulk = pd.read_csv(up)
            report, ok_rows = [], []
            for _, r in bulk.iterrows():
                try:
                    f = dict(sku_name=r.SKU_Name, category=r.Category, perishability=r.Perishability, standard_reorder_qty=r.Standard_Reorder_Qty,
                             unit_of_measure=r.get("Unit_of_Measure", "Units"), distributor_id=r.Distributor_ID)
                    oh = (int(r.Units_On_Hand), pd.to_datetime(r.As_Of_Date).date())
                    with eng.connect() as conn:
                        store.preview_change(conn, "add", str(r.SKU_ID), f, None, oh, st.session_state["as_of"], st.session_state["cfg"])
                    report.append({"SKU_ID": r.SKU_ID, "result": "ok"}); ok_rows.append((str(r.SKU_ID), f, oh))
                except Exception as e:  # noqa: BLE001
                    report.append({"SKU_ID": r.SKU_ID, "result": "REFUSED: " + str(e)})
            st.dataframe(pd.DataFrame(report), hide_index=True, width="stretch")
            if ok_rows and st.button(f"Write the {len(ok_rows)} valid SKU(s)", type="primary"):
                for sid, f, oh in ok_rows:
                    try:
                        with eng.begin() as conn:
                            store.apply_change(conn, "add", sid, who, f, None, oh, st.session_state["as_of"], st.session_state["cfg"])
                    except Exception as e:  # noqa: BLE001
                        st.error(f"{sid}: {db.redact(e)}")
                st.session_state["stale"] = True
                st.rerun()
    with tabs[5]:
        st.error("Permanent delete removes the SKU and its sales, inventory and optional-table rows. Recommendation and decision history is kept. "
                 "Retire is usually what you want.")
        sid = st.selectbox("SKU to delete", tbl.sku_id, key="del_sku")
        try:
            with eng.connect() as conn:
                p = store.preview_change(conn, "delete", sid, None, None, None, st.session_state["as_of"], st.session_state["cfg"])
            st.write("Rows that would be removed:", p["diff"]["rows_to_remove"])
            typed = st.text_input(f"Type {sid} to confirm")
            if typed == sid and st.button("Permanently delete", type="primary"):
                with eng.begin() as conn:
                    store.apply_change(conn, "delete", sid, who, None, None, None, st.session_state["as_of"], st.session_state["cfg"])
                st.session_state["stale"] = True
                st.rerun()
        except store.ChangeRefused as e:
            st.error("Refused, nothing written: " + str(e))
    with tabs[6]:
        if last:
            st.write(f"Most recent change: #{last['id']} {last['action']} {last['sku_id']} by {last['changed_by']} at {last['changed_at']}")
            if st.button("Undo this change"):
                try:
                    with eng.begin() as conn:
                        store.undo_last(conn, who or "unknown")
                    st.session_state["stale"] = True
                    st.rerun()
                except store.ChangeRefused as e:
                    st.error(str(e))
        else:
            st.info("No change to undo.")
        with eng.connect() as conn:
            st.dataframe(store.changes_log(conn), hide_index=True, width="stretch")


def page_decisions():
    header("Decisions & shadow mode")
    if st.session_state["mode"] != "db":
        st.error("The decision log needs a Neon connection (offline mode is read-only).")
        return
    with get_engine().connect() as conn:
        s = store.shadow_stats(conn)
        dec = store.decisions_table(conn)
    st.caption("Shadow mode: the tool recommends, the human decides. Agreement = decisions accepted without change.")
    if s["empty"]:
        st.info("No decisions logged yet. Accept, change, reject or hold lines on the Action list and this page will fill in.")
        return
    c = st.columns(2)
    c[0].metric("Decisions logged", s["total"])
    c[1].metric("Agreement rate", f"{s['agreement'] * 100:.0f}%")
    st.subheader("Overrides by reason")
    st.dataframe(s["overrides"], hide_index=True, width="stretch") if len(s["overrides"]) else st.info("No overrides yet.")
    st.subheader("Agreement by run")
    st.dataframe(s["by_run"], hide_index=True, width="stretch")
    st.subheader("Decision log")
    st.dataframe(dec.drop(columns=["run_id_text"]), hide_index=True, width="stretch")


def page_quality():
    header("Data quality")
    res = st.session_state.get("res")
    gate = res.gate if res else st.session_state.get("gate_stop")
    if gate:
        for f in gate.findings:
            {"stop": st.error, "warn": st.warning, "info": st.info}[f.level](f"{f.code}: {f.message}")
        if not gate.findings:
            st.success("No findings.")
    if res is not None:
        st.subheader("Excluded: discontinued")
        st.dataframe(res.excluded, hide_index=True, width="stretch") if len(res.excluded) else st.caption("None.")
        st.subheader("Rows used")
        d = st.session_state["data"]
        st.dataframe(pd.DataFrame({"table": list(d), "rows": [len(v) for v in d.values()]}), hide_index=True)
        st.caption(f"Data fingerprint: {res.meta['fingerprint']}")


def page_assumptions():
    header("Assumptions & limitations")
    cfg = st.session_state.get("cfg", DEFAULT_CONFIG)
    data = st.session_state.get("data", {})
    st.subheader("Assumptions in use (all editable on Rules & config)")
    for a in outputs.assumptions_list(cfg, data):
        st.markdown(f"- {a}")
    st.markdown("""
- ISO weeks. A week belongs to the month of its Thursday; the festival flag is looked up per week from the calendar. The uplift for a run uses the as-of date's month.
- 12-week average includes the last 4 weeks. The trend test uses raw sales (the uplift cancels in the ratio).
- Distributor_ID is a PO grouping key. In the case brief distributors are customers; here each SKU maps to one, and two sit outside Freshroots' stated territory (Karnataka, Gujarat).
- Rule 1 says HIGH_DEMAND means reorder at 1.5x standard. This tool applies 1.5x only when stock is also below its minimum. A high-demand SKU with healthy cover is tagged WATCH, not ordered.
- Order sizing uses whole multiples of the standard quantity that restore cover to the target range. The literal brief quantity is shown beside it because it often does not restore cover.
- Exceptions: history under 12 weeks, zero or missing weeks, coefficient of variation above the threshold, weeks beyond 3 scaled MADs of a trailing 8-week median (MAD floored at 10% of the median), zero/negative on-hand, inactive distributor, no history.
    """)
    st.subheader("Limitations")
    from planner.limitations import LIMITATIONS
    for item in LIMITATIONS:
        st.markdown(f"- {item}")
    st.markdown("- The tool never places orders. Every output is a draft. The optional Claude layer only rewords an explanation the engine produced; "
                "any number not in the engine's facts makes the text fall back to the template.")


def page_config():
    header("Rules & config")
    cfg = st.session_state.get("cfg", DEFAULT_CONFIG)
    st.json(cfg, expanded=False)
    if st.session_state["mode"] != "db":
        st.info("Offline mode: config edits last for this session only and are not saved.")
    with st.form("cfg"):
        c = st.columns(4)
        up = c[0].number_input("Festival uplift (all categories)", 1.0, 2.0, float(cfg["festival_uplift_default"]), 0.05)
        hd = c[1].number_input("High-demand trend threshold", 0.0, 1.0, float(cfg["high_demand_trend"]), 0.05)
        so = c[2].number_input("Stockout below (weeks)", 1.0, 52.0, float(cfg["stockout_weeks"]), 0.5)
        ov = c[3].number_input("Overstock above (weeks)", 1.0, 104.0, float(cfg["overstock_weeks"]), 0.5)
        c = st.columns(4)
        tgt = c[0].number_input("Target cover after order (weeks)", 1.0, 52.0, float(cfg["target_cover_weeks"]), 0.5)
        cv = c[1].number_input("CV threshold", 0.0, 2.0, float(cfg["cv_threshold"]), 0.05)
        ll = c[2].number_input("Lead time, non-perishable (weeks)", 0.0, 52.0, float(cfg["lead_time_weeks"]["Low"]), 0.5)
        lh = c[3].number_input("Lead time, perishable (weeks)", 0.0, 52.0, float(cfg["lead_time_weeks"]["High"]), 0.5)
        note = st.text_input("Note (why this change)")
        go = st.form_submit_button("Save as new version")
    if go:
        if not note.strip():
            st.error("A note is required.")
        else:
            new = merged({**cfg, "festival_uplift_default": up, "high_demand_trend": hd, "stockout_weeks": so, "overstock_weeks": ov,
                          "target_cover_weeks": tgt, "cv_threshold": cv, "lead_time_weeks": {"Low": ll, "High": lh}})
            if st.session_state["mode"] == "db":
                with get_engine().begin() as conn:
                    store.save_config_version(conn, new, note)
            else:
                st.session_state["cfg_override"] = new
            st.session_state["stale"] = True
            st.success("Saved. Click Re-run planner on the Action list to apply it.")
    if st.session_state["mode"] == "db":
        with get_engine().connect() as conn:
            h = store.config_history(conn)
        st.subheader("Version history")
        st.dataframe(h.drop(columns=["config_json"]) if len(h) else h, hide_index=True, width="stretch") if len(h) else st.caption("No saved versions; defaults in use.")


# ------------------------------------------------------------------ main
init_state()
if st.session_state.get("fatal"):
    st.error("Something went wrong: " + st.session_state["fatal"])
page = st.sidebar.radio("Page", PAGES)
st.sidebar.caption("Mode: " + ("Neon" if st.session_state["mode"] == "db" else "offline CSV"))
{"Action list": page_actions, "Draft POs": page_pos, "Exceptions": page_exceptions, "Scenario lab": page_scenario,
 "SKU manager": page_sku_manager, "Decisions & shadow mode": page_decisions, "Data quality": page_quality,
 "Assumptions & limitations": page_assumptions, "Rules & config": page_config}[page]()
