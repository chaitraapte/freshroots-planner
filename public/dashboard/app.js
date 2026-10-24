"use strict";
/* Freshroots Demand Planning. Vanilla JS, no build. All text is inserted via textContent. */

const PAGES = [["today", "Today"], ["orders", "Orders"], ["whatif", "What-if"], ["skus", "SKUs"], ["scoreboard", "Scoreboard"], ["rules", "Rules & data"]];
const ALIAS = { actions: "today", pos: "orders", exceptions: "today", scenario: "whatif", decisions: "scoreboard", quality: "rules", assumptions: "rules", config: "rules" };
const S = {
  plan: null, csv: false, err: null, lastOk: null, sig: "", kpi: "", dist: "", q: "", open: new Set(), form: null,
  passcode: safe(() => sessionStorage.getItem("passcode")) || "", by: safe(() => localStorage.getItem("by")) || "", llm: false,
  skuView: "list", skuSel: null, afterUnlock: null, rt: 0,
};
function safe(f) { try { return f(); } catch (e) { return null; } }
function $(s) { return document.querySelector(s); }
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) { if (kid === null || kid === undefined || kid === false) continue; el.append(kid.nodeType ? kid : document.createTextNode(String(kid))); }
  return el;
}
const fmt = (n, d = 1) => (n === null || n === undefined ? "n/a" : Number(n).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));
const pct = (n) => (n === null || n === undefined ? "n/a" : (n >= 0 ? "+" : "") + (n * 100).toFixed(0) + "%");
const chip = (text, cls) => h("span", { class: "chip " + (cls || "") }, text);
const FLAG = { STOCKOUT_RISK: ["Running low", "red"], OVERSTOCK_RISK: ["Overstocked", "amber"], OK: ["Healthy", "green"] };
const flagChip = (f) => chip(...(FLAG[f] || [f, ""]));

/* ------------------------------------------------------------------ network */
async function api(path, opts = {}) {
  const headers = { "content-type": "application/json", "x-passcode": S.passcode };
  if (S.csv) headers["x-mode"] = "csv";
  const r = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
  const body = (r.headers.get("content-type") || "").includes("json") ? await r.json() : null;
  if (!r.ok) {
    const e = new Error((body && (body.message || body.detail)) || "Request failed (" + r.status + ")");
    e.status = r.status; e.waking = !!(body && body.waking); throw e;
  }
  return body;
}
const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b || {}) });
async function write(p, b) { // a write that asks for the passcode when needed
  try { return await post(p, b); } catch (e) { if (e.status === 401) { S.passcode = ""; safe(() => sessionStorage.removeItem("passcode")); paintHeader(); openUnlock("That passcode didn't work. Enter it again."); } throw e; }
}
async function download(name) {
  const r = await fetch("/api/download/" + name, { headers: S.csv ? { "x-mode": "csv" } : {} });
  if (!r.ok) { const j = await r.json().catch(() => ({})); return toast(j.detail || "Download failed.", true); }
  const a = h("a", { href: URL.createObjectURL(await r.blob()), download: name.startsWith("approved/") ? name.slice(9, -4) + "_approved.csv" : name.split("/").pop() });
  document.body.append(a); a.click(); a.remove();
}
function toast(msg, err) {
  const t = h("div", { class: "strip " + (err ? "err" : "ok"), style: "position:fixed;right:16px;bottom:16px;z-index:50;max-width:380px;box-shadow:0 6px 24px #0004;margin:0" }, msg);
  document.body.append(t); setTimeout(() => t.remove(), 4500);
}

/* ------------------------------------------------------------------ loading + header */
async function loadPlan(quiet) {
  try {
    const p = await api("/api/plan");
    const sig = JSON.stringify([p.fingerprint, p.stale, p.latest_run && p.latest_run.run_id, p.gate_stop, p.decisions]);
    const changed = sig !== S.sig;
    S.plan = p; S.err = null; S.lastOk = new Date(); S.sig = sig;
    paintHeader();
    if (!quiet || changed) render();
  } catch (e) {
    S.err = e; paintHeader();
    if (!S.plan) $("#page").replaceChildren();
    if (e.waking) setTimeout(() => loadPlan(false), 4000);
  }
}
function paintHeader() {
  const p = S.plan, offline = S.csv || (p && p.mode === "csv");
  const m = $("#mode"); m.className = "pill " + (offline ? "warn" : "ok"); m.textContent = offline ? "Offline CSV (read-only)" : "Neon";
  const l = $("#live");
  if (S.err) { l.className = "pill err"; l.textContent = S.err.waking ? "Waking database..." : "Disconnected"; }
  else { l.className = "pill"; l.replaceChildren(h("span", { class: "dot pulse", style: "color:var(--accent)" }), "Live " + (S.lastOk ? S.lastOk.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "")); }
  const lock = $("#lock"); lock.textContent = S.passcode ? "Edits on" + (S.by ? " · " + S.by : "") : "Unlock edits"; lock.className = "btn" + (S.passcode ? "" : " primary");
  lock.onclick = () => openUnlock();
  paintStrip();
}
function paintStrip() {
  const el = $("#strip"), p = S.plan; el.replaceChildren();
  const add = (cls, ...kids) => el.append(h("div", { class: "strip " + cls }, ...kids));
  if (S.err) return add(S.err.waking ? "info" : "err", S.err.waking ? "Waking the database (Neon sleeps when idle). Retrying..." : S.err.message,
    !S.csv && !S.err.waking && h("button", { class: "btn", onclick: () => { S.csv = true; loadPlan(); } }, "Use offline CSV"));
  if (!p) return;
  if (p.config_problems && p.config_problems.length) add("warn", "The saved settings are invalid (" + p.config_problems[0] + "), so defaults are being used. Fix them under Rules & data.");
  if (p.gate_stop) add("err", "Data-quality check stopped the run: " + p.gate.stops.map((f) => f.message).join("; "));
  if (S.csv) add("warn", "Offline CSV mode: read-only.", h("button", { class: "btn", onclick: () => { S.csv = false; loadPlan(); } }, "Try Neon"));
  if (p.mode === "db" && p.stale) add("warn", p.latest_run ? "Data or settings changed since the last saved run, so this list is out of date." : "No run saved yet. Save one to start approving orders.",
    p.writes_enabled && h("button", { class: "btn primary", onclick: rerun }, "Re-run planner"));
  if (p.mode === "db" && !p.writes_enabled) add("info", "Read-only: editing is switched off on this server.");
}
async function rerun() {
  if (!S.passcode) return openUnlock(null, rerun);
  try { await write("/api/run"); await loadPlan(); toast("Planner re-run and saved."); } catch (e) { toast(e.message, true); }
}
function openUnlock(msg, then) {
  S.afterUnlock = then || null;
  const d = $("#unlock");
  const pw = h("input", { type: "password", value: S.passcode, autocomplete: "off", placeholder: "Passcode" }), nm = h("input", { value: S.by, placeholder: "Your name (recorded on every change)" });
  const note = h("div", { class: "msg", style: "min-height:20px" }, msg || "");
  const save = async () => {
    if (!pw.value || !nm.value.trim()) { note.className = "msg err"; note.textContent = "Enter the passcode and your name."; return; }
    const old = S.passcode; S.passcode = pw.value;
    try { await post("/api/auth"); } catch (e) { S.passcode = old; note.className = "msg err"; note.textContent = e.status === 401 ? "That passcode didn't work." : e.message; return; }
    S.by = nm.value.trim(); safe(() => { sessionStorage.setItem("passcode", S.passcode); localStorage.setItem("by", S.by); });
    d.close(); paintHeader(); const f = S.afterUnlock; S.afterUnlock = null; f && f();
  };
  pw.addEventListener("keydown", (e) => e.key === "Enter" && save()); nm.addEventListener("keydown", (e) => e.key === "Enter" && save());
  d.replaceChildren(h("h2", {}, "Unlock edits"), h("p", { class: "fine" }, "Needed to approve orders, change SKUs and re-run the planner. Viewing never needs it."),
    h("div", { style: "display:grid;gap:10px;margin:12px 0 4px" }, pw, nm), note,
    h("div", { class: "row", style: "margin-top:8px" }, h("button", { class: "btn primary", onclick: save }, "Save"),
      h("button", { class: "btn quiet", onclick: () => { S.passcode = ""; safe(() => sessionStorage.removeItem("passcode")); d.close(); paintHeader(); } }, "Lock again"),
      h("button", { class: "btn quiet", onclick: () => d.close() }, "Cancel")));
  d.showModal();
}

/* ------------------------------------------------------------------ routing + shared bits */
function route() { const k = location.hash.slice(1) || "today"; return ALIAS[k] || k; }
function paintNav() { const cur = route(); $("#nav").replaceChildren(...PAGES.map(([k, t]) => h("a", { href: "#" + k, class: k === cur ? "active" : "" }, t))); }
function render() {
  paintNav();
  if (!S.plan) return;
  const my = ++S.rt, box = h("div"); // build off-screen; only the newest render may replace the page (stops duplicated content)
  const fn = { today, orders, whatif, skus, scoreboard, rules }[route()] || today;
  Promise.resolve(fn(box)).then(() => { if (my === S.rt) $("#page").replaceChildren(...box.childNodes); })
    .catch((e) => { if (my === S.rt) $("#page").replaceChildren(h("div", { class: "strip err" }, e.message)); });
}
function head(page, t, sub) { page.replaceChildren(h("h1", {}, t), h("p", { class: "sub" }, sub || "")); }
function table(cols, rows, opts = {}) {
  const body = h("tbody", {}, rows.map((r) => h("tr", { class: opts.onrow ? "click" + (opts.sel && opts.sel(r) ? " sel" : "") : "", onclick: opts.onrow ? () => opts.onrow(r) : null },
    cols.map((c) => h("td", { class: (c.num ? "num " : "") + (c.wrap ? "wrap" : "") }, c.render ? c.render(r) : r[c.key])))));
  return h("div", { class: "tbl" }, h("table", {}, h("thead", {}, h("tr", {}, cols.map((c) => h("th", { class: c.num ? "num" : "" }, c.label)))), body));
}
function empty(title, text) { return h("div", { class: "card empty" }, h("b", {}, title), text); }

/* cover gauge: where stock is now, the healthy zone, and where an order would take it */
function gauge(r, showAfter = true) {
  if (r.cover_weeks === null) return h("div", { class: "fine", style: "margin:10px 0" }, "No sales history, so cover can't be computed.");
  const after = showAfter && r.recommended_cover_after !== null && r.rule_action === "ORDER" ? r.recommended_cover_after : null;
  const max = Math.max(r.max_weeks * 1.35, r.cover_weeks * 1.1, (after || 0) * 1.1);
  const x = (v) => Math.min(100, Math.max(0, (v / max) * 100)) + "%";
  const state = r.cover_weeks < r.min_weeks ? "" : r.cover_weeks > r.max_weeks ? "high" : "ok";
  const lbl = (v, text, cls = "") => h("span", { class: "lb " + cls, style: `left:${x(v)}` }, text);
  const nowLeft = Math.min(100, (r.cover_weeks / max) * 100);
  return h("div", {}, h("div", { class: "fine", style: "margin-top:12px" }, `Weeks of cover · healthy zone ${r.min_weeks}–${r.max_weeks} wk`),
    h("div", { class: "gauge", title: `Now ${fmt(r.cover_weeks)} weeks. Healthy zone ${r.min_weeks} to ${r.max_weeks} weeks.` },
      h("div", { class: "band", style: `left:${x(r.min_weeks)};width:${Math.min(100, (r.max_weeks / max) * 100) - (r.min_weeks / max) * 100}%` }),
      after !== null && h("div", { class: "mk after", style: `left:${x(after)}`, title: `After order: ${fmt(after)} weeks` }),
      h("div", { class: "mk " + state, style: `left:${x(r.cover_weeks)}` }),
      lbl(r.cover_weeks, `${fmt(r.cover_weeks)} wk now`, "now"),
      after !== null && (Math.abs(nowLeft - (after / max) * 100) > 18) && lbl(after, `${fmt(after)} wk after order`)));
}
function sparkline(series, weeks, last4, avg12) {
  const W = 300, H = 140, pad = 28, vals = series.filter((v) => v !== null);
  if (!vals.length) return h("p", { class: "fine" }, "No sales history.");
  const lo = Math.min(...vals, last4 ?? Infinity, avg12 ?? Infinity), hi = Math.max(...vals, last4 ?? -Infinity, avg12 ?? -Infinity), span = hi - lo || 1;
  const x = (i) => pad + (i * (W - pad - 8)) / Math.max(series.length - 1, 1), y = (v) => H - 22 - ((v - lo) / span) * (H - 40);
  const ns = "http://www.w3.org/2000/svg", el = (t, a, txt) => { const e = document.createElementNS(ns, t); for (const k in a) e.setAttribute(k, a[k]); if (txt) e.textContent = txt; return e; };
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark", role: "img", "aria-label": "Weekly sales with 4-week and 12-week averages" });
  const pts = series.map((v, i) => (v === null ? null : [x(i), y(v)])).filter(Boolean);
  svg.append(el("polyline", { points: pts.map((p) => p.join(",")).join(" "), fill: "none", stroke: "#1f7a4a", "stroke-width": 2 }));
  [[avg12, "12-wk avg", "#9a5b00"], [last4, "4-wk avg", "#1c5fa5"]].forEach(([v, lab, col]) => { if (v === null) return;
    svg.append(el("line", { x1: pad, x2: W - 8, y1: y(v), y2: y(v), stroke: col, "stroke-dasharray": "4 3" }), el("text", { x: W - 8, y: y(v) - 3, "text-anchor": "end", style: `fill:${col}` }, `${lab} ${fmt(v, 0)}`)); });
  svg.append(el("text", { x: pad, y: H - 6 }, weeks[0]), el("text", { x: W - 8, y: H - 6, "text-anchor": "end" }, weeks[weeks.length - 1]), el("text", { x: 2, y: y(hi) + 3 }, fmt(hi, 0)), el("text", { x: 2, y: y(lo) + 3 }, fmt(lo, 0)));
  return svg;
}
const REASONS = [
  [/week\(s\) (.+?) more than [\d.]+ MADs from the rolling median/, "Unusual jump or drop in recent weekly sales ($1)"],
  [/weekly coefficient of variation [\d.]+ exceeds [\d.]+/, "Weekly sales are very uneven"],
  [/history is (\d+) weeks, shorter than (\d+)/, "Only $1 weeks of sales history (needs $2)"],
  [/no history \(no sales rows at all\)/, "No sales history yet"],
  [/(\d+) missing week\(s\) in history/, "$1 week(s) missing from the sales history"],
  [/zero-sales week\(s\) in history.*/, "A week with zero sales (may have been out of stock, not zero demand)"],
  [/on-hand is zero or negative.*/, "Stock count is zero or negative"],
  [/distributor is not Active \((.*)\)/, "Distributor is not active ($1)"],
  [/no whole multiple of the standard quantity brings cover inside the allowed range( while keeping the 1\.5x high-demand floor)?/, "No whole multiple of the standard pack lands cover in the healthy range$1"],
  [/verdict depends on how the rules are interpreted:/, "Answer changes with how the rules are read:"],
  [/overstock flag but demand is rising/, "Looks overstocked but demand is rising"],
  [/HIGH_DEMAND combined with OVERSTOCK_RISK/, "Demand surging while overstocked"],
  [/STOCKOUT_RISK/g, "running low"], [/OVERSTOCK_RISK/g, "overstocked"], [/\bOK\b/g, "healthy"], [/ -> /g, " → "],
];
const plainReason = (t) => REASONS.reduce((acc, [re, to]) => acc.replace(re, to), t).replace(/ while keeping the 1\.5x high-demand floor/, " while keeping the 1.5× demand-surge floor");
function why(r) {
  const out = [], assumed = r.lead_time_source === "default" ? " (assumed)" : "";
  if (r.cover_weeks === null) out.push("No sales history yet.");
  else if (r.primary_flag === "STOCKOUT_RISK") out.push(`${fmt(r.cover_weeks)} weeks of stock left (minimum ${r.min_weeks}). Restock takes ${fmt(r.lead_time_weeks, 0)} wk${assumed}${r.urgency_weeks <= 0 ? ", so you run out first." : "."}`);
  else if (r.primary_flag === "OVERSTOCK_RISK") out.push(`${fmt(r.cover_weeks)} weeks of stock, above the ${r.max_weeks}-week limit. About ${fmt(r.weeks_until_below_max)} weeks until it's back in range.`);
  else out.push(`${fmt(r.cover_weeks)} weeks of stock, inside the ${r.min_weeks}–${r.max_weeks} week range.`);
  if (r.tags.includes("HIGH_DEMAND")) out.push(`Demand is up ${pct(r.trend)} over the last 4 weeks.`);
  return out.join(" ");
}
const ruleText = (r) => ({ ORDER: r.recommended_qty === null ? "order (no quantity fits)" : `order ${fmt(r.recommended_qty, 0)}`, "PAUSE ORDER": "pause orders", WATCH: "watch", NONE: "no action" }[r.rule_action]);

/* ------------------------------------------------------------------ decisions on a row */
function canAct() {
  const p = S.plan;
  if (p.mode !== "db") return "Read-only offline";
  if (!p.writes_enabled) return "Editing is off on this server";
  if (p.stale) return "Re-run the planner first";
  return null;
}
async function decide(r, decision, qty, reason) {
  if (!S.passcode) return openUnlock(null, () => decide(r, decision, qty, reason));
  try { await write("/api/decisions", { run_id: S.plan.latest_run.run_id, sku_id: r.sku_id, decision, qty, reason, by: S.by }); S.form = null; await loadPlan(); toast("Saved: " + r.sku_name); }
  catch (e) { const m = $("#msg-" + r.sku_id); if (m) { m.textContent = e.message; m.className = "msg err"; } else toast(e.message, true); }
}
function actions(r, kind) {
  const d = S.plan.decisions && S.plan.decisions[r.sku_id];
  if (d) {
    const label = { Accept: "✓ Approved", "Change quantity": `✎ Changed to ${fmt(d.human_qty, 0)}`, Reject: "✕ Rejected", Hold: "⏸ On hold" }[d.human_decision];
    return h("div", {}, h("div", { class: "decided" + (d.human_decision === "Accept" || d.human_decision === "Change quantity" ? "" : " no") }, label), h("div", { class: "fine" }, `${d.decided_by}${d.reason ? " · " + d.reason : ""}`));
  }
  const block = canAct();
  if (block) return h("div", { class: "fine" }, block);
  const f = S.form && S.form.sku === r.sku_id ? S.form : null;
  const approveLabel = kind === "review" ? "Go with rules" : kind === "pause" ? "Approve pause" : "Approve";
  const btns = h("div", { class: "btns" },
    h("button", { class: "btn primary", onclick: () => decide(r, "Accept", null, null) }, approveLabel),
    kind !== "pause" && h("button", { class: "btn", onclick: () => { S.form = { sku: r.sku_id, mode: "Change quantity" }; render(); } }, "Change qty"),
    h("button", { class: "btn", onclick: () => { S.form = { sku: r.sku_id, mode: "Reject" }; render(); } }, "Reject"),
    h("button", { class: "btn", onclick: () => { S.form = { sku: r.sku_id, mode: "Hold" }; render(); } }, "Hold"));
  if (!f) return btns;
  const hint = h("div", { class: "fine" });
  const qty = h("input", { type: "number", min: 0, value: r.recommended_qty ?? 0, placeholder: "Quantity", oninput: () => {
    const q = Number(qty.value), c = r.weekly_demand > 0 ? (r.on_hand + r.open_po_units + q) / r.weekly_demand : null;
    hint.textContent = c === null || qty.value === "" ? "" : `Cover after this order: ${fmt(c)} wk (healthy ${r.min_weeks}–${r.max_weeks} wk)${c < r.min_weeks ? " — still running low" : c > r.max_weeks ? " — would overstock" : ""}`;
    hint.style.color = c !== null && (c < r.min_weeks || c > r.max_weeks) ? "var(--amber)" : ""; } }), why_ = h("input", { placeholder: `Reason for ${f.mode.toLowerCase()} (required)`, autofocus: true });
  return h("div", { class: "inline" }, h("strong", { style: "font-size:13px" }, f.mode), f.mode === "Change quantity" && [h("label", {}, "New quantity", qty), hint], why_,
    h("div", { class: "btns" }, h("button", { class: "btn primary", onclick: () => decide(r, f.mode, f.mode === "Change quantity" ? qty.value : null, why_.value) }, "Save"),
      h("button", { class: "btn quiet", onclick: () => { S.form = null; render(); } }, "Cancel")), h("div", { id: "msg-" + r.sku_id, class: "msg" }));
}

/* ------------------------------------------------------------------ Today */
function item(r, kind) {
  const p = S.plan, done = p.decisions && p.decisions[r.sku_id];
  const headline = kind === "order" ? [`Order ${fmt(r.recommended_qty, 0)}`, `${r.multiples} × ${fmt(r.standard_qty, 0)} standard`]
    : kind === "pause" ? ["Pause orders", "until cover falls back to range"] : kind === "review" ? ["Needs your call", `Rules say: ${ruleText(r)}`] : ["Watch", "no order needed yet"];
  const reasons = r.review_reasons.map(plainReason);
  const open = S.open.has(r.sku_id);
  const brief = r.rule_action === "ORDER" && r.brief_literal_qty !== null;
  return h("div", { class: "item" + (done ? " done" : "") },
    h("div", {},
      h("div", { class: "i-title" }, h("strong", {}, r.sku_name), h("span", { class: "muted" }, r.sku_id), chip(r.distributor_id),
        r.urgency_tier === "CRITICAL" && chip("Out before restock", "red"), r.tags.includes("HIGH_DEMAND") && chip("Demand surging", "blue"), r.perishability === "High" && chip("Perishable")),
      h("div", { class: "i-why" }, why(r), reasons.length ? h("div", { class: "reasons" }, "⚠ " + reasons[0] + (reasons.length > 1 ? ` (+${reasons.length - 1} more)` : "")) : ""),
      gauge(r),
      h("details", { open: open ? "" : null, ontoggle: (e) => { e.target.open ? S.open.add(r.sku_id) : S.open.delete(r.sku_id); } },
        h("summary", {}, "Details"),
        h("div", { class: "more" },
          h("div", {}, h("p", { style: "margin-top:0" }, r.explanation),
            brief && h("div", { class: "compare" }, h("div", {}, "Brief's rule", h("b", {}, `${fmt(r.brief_literal_qty, 0)} units`), `→ ${fmt(r.brief_literal_cover_after)} wk cover`),
              h("div", {}, "Recommended", h("b", {}, r.recommended_qty === null ? "n/a" : `${fmt(r.recommended_qty, 0)} units`), `→ ${fmt(r.recommended_cover_after)} wk cover`)),
            S.llm && askBox(r),
            reasons.length > 1 && h("ul", { style: "margin:6px 0;padding-left:18px;font-size:13px" }, reasons.map((x) => h("li", {}, x))),
            h("dl", { class: "kv" }, [["On hand", fmt(r.on_hand, 0)], ["Open POs", fmt(r.open_po_units, 0)], ["Weekly demand used", fmt(r.weekly_demand, 0)], ["Last 4 wk avg / 12 wk avg", `${fmt(r.last4_avg, 0)} / ${fmt(r.avg12, 0)}`],
              ["Festival uplift", "×" + fmt(r.festival_uplift, 2)], ["Restock time", `${fmt(r.lead_time_weeks, 0)} wk (${r.lead_time_source})`], ["Verdict stability", r.stability]].map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]))),
          h("div", {}, sparkline(p.series[r.sku_id] || [], p.weeks, r.last4_avg, r.avg12))))),
    h("div", { class: "i-act" }, h("div", {}, h("div", { class: "qty" }, headline[0]), h("div", { class: "fine" }, headline[1])), actions(r, kind)));
}
function askBox(r) {
  const q = h("input", { placeholder: "Ask about this SKU, e.g. why this quantity?", style: "flex:1" }), out = h("div", { class: "fine", style: "margin-top:6px" });
  const go = async (text) => { out.textContent = "Thinking..."; try { const a = await post("/api/ask", { sku: r.sku_id, question: text }); out.textContent = a.text; out.style.color = a.ok ? "" : "var(--amber)"; if (a.ok) out.append(h("div", { class: "fine" }, `AI (${a.source}) explains; it never changes the plan.`)); } catch (e) { out.textContent = e.message; } };
  return h("div", { style: "margin:10px 0" }, h("div", { class: "row", style: "align-items:center" }, q, h("button", { class: "btn", onclick: () => go(q.value) }, "Ask")),
    h("div", { class: "btns", style: "margin-top:6px" }, ["Why this quantity?", "What should I check before approving?"].map((t) => h("button", { class: "btn quiet", style: "padding:2px 8px;font-size:12px", onclick: () => { q.value = t; go(t); } }, t))), out);
}
function today(page) {
  const p = S.plan;
  head(page, "Today", `Stock as of ${p.as_of}${p.festival_month ? " · festival month, demand +25%" : ""} · every line is a draft until you approve it`);
  if (p.gate_stop) return;
  let R = p.recs;
  if (S.dist) R = R.filter((r) => r.distributor_id === S.dist);
  if (S.q) R = R.filter((r) => (r.sku_name + r.sku_id).toLowerCase().includes(S.q.toLowerCase()));
  const all = p.recs;
  const by = (a) => R.filter((r) => r.action === a);
  const order = by("ORDER"), review = by("REVIEW"), pause = by("PAUSE ORDER"), watch = by("WATCH"), fine = R.filter((r) => r.action === "NONE");
  const units = all.filter((r) => r.action === "ORDER").reduce((a, r) => a + (r.recommended_qty || 0), 0);
  const crit = all.filter((r) => r.urgency_tier === "CRITICAL" && r.action !== "REVIEW" || (r.urgency_tier === "CRITICAL" && r.rule_action === "ORDER"));
  const kpis = [
    ["order", "red", all.filter((r) => r.action === "ORDER").length, "Order now", `${fmt(units, 0)} units across ${new Set(all.filter((r) => r.action === "ORDER").map((r) => r.distributor_id)).size} distributors`],
    ["critical", "red", new Set(crit.map((r) => r.sku_id)).size, "Out before restock", "stock ends before a new order can arrive"],
    ["review", "amber", all.filter((r) => r.action === "REVIEW").length, "Needs your call", "rules can't settle these safely"],
    ["pause", "blue", all.filter((r) => r.action === "PAUSE ORDER").length, "Pause orders", "too much stock on hand"],
  ];
  page.append(h("div", { class: "kpis" }, kpis.map(([k, c, n, l, d]) => h("button", { class: `kpi ${c}${S.kpi === k ? " on" : ""}`, disabled: n === 0 && S.kpi !== k, onclick: () => { S.kpi = S.kpi === k ? "" : k; render(); } },
    h("div", { class: "n" }, n), h("div", { class: "l" }, l), h("div", { class: "d" }, d)))));
  const dists = [...new Set(all.map((r) => r.distributor_id))].sort();
  page.append(h("div", { class: "row", style: "margin-bottom:18px" },
    h("label", { class: "grow" }, "Search", h("input", { placeholder: "SKU or name", value: S.q, oninput: (e) => { S.q = e.target.value; clearTimeout(S.t); S.t = setTimeout(() => { render(); const i = $("#page input"); i && (i.focus(), i.setSelectionRange(S.q.length, S.q.length)); }, 250); } })),
    h("label", {}, "Distributor", h("select", { onchange: (e) => { S.dist = e.target.value; render(); } }, [h("option", { value: "" }, "All"), ...dists.map((d) => h("option", { value: d, selected: S.dist === d }, d))])),
    S.kpi && h("button", { class: "btn", onclick: () => { S.kpi = ""; render(); } }, "Clear filter")));
  const show = (k) => !S.kpi || S.kpi === k || (S.kpi === "critical" && k === "order");
  const sec = (key, title, hint, list, kind) => list.length && show(key) ? h("section", { class: "sec" }, h("div", { class: "sec-h" }, h("h2", {}, title), h("span", { class: "count" }, list.length), h("span", { class: "hint" }, hint)), list.map((r) => item(r, kind))) : null;
  const orderList = S.kpi === "critical" ? order.filter((r) => r.urgency_tier === "CRITICAL") : order;
  const parts = [sec("order", "Order now", "most urgent first. Quantities bring cover back into the healthy range.", orderList, "order"),
    sec("review", "Needs your call", "the rules disagree with themselves or lack data. Nothing here goes on a PO until you decide.", review, "review"),
    sec("pause", "Pause orders", "the rules say hold the next order.", pause, "pause"),
    sec("watch", "Watch", "healthy now, demand is rising.", watch, "watch")].filter(Boolean);
  if (!parts.length && !(fine.length && !S.kpi)) page.append(empty("Nothing matches", "Clear the filter to see everything."));
  page.append(...parts);
  if (fine.length && !S.kpi) page.append(h("section", { class: "sec" }, h("details", {}, h("summary", {}, `${fine.length} healthy SKUs, no action needed`),
    h("div", { style: "margin-top:10px" }, table([{ key: "sku_name", label: "SKU", render: (r) => `${r.sku_name} (${r.sku_id})` }, { key: "distributor_id", label: "Distributor" },
      { key: "cover_weeks", label: "Cover", num: true, render: (r) => fmt(r.cover_weeks) + " wk" }, { key: "x", label: "Healthy range", render: (r) => `${r.min_weeks}–${r.max_weeks} wk` }, { key: "stability", label: "Stability" }], fine)))));
  if (p.excluded.length) page.append(h("p", { class: "fine" }, "Excluded (discontinued): " + p.excluded.map((e) => e.SKU_ID).join(", ")));
}

/* ------------------------------------------------------------------ Orders */
function orders(page) {
  const p = S.plan, dec = p.decisions || {};
  head(page, "Orders", "Draft purchase orders by distributor. Approve lines on Today; this page shows what will go out.");
  const entries = Object.entries(p.draft_pos);
  if (!entries.length) page.append(empty("No order lines yet", "Nothing is ready to order. Lines that need your judgment are on Today."));
  entries.forEach(([d, lines]) => {
    const rows = lines.map((l) => ({ ...l, d: dec[l.sku_id] }));
    const final = (r) => (!r.d ? r.quantity : r.d.human_decision === "Change quantity" ? r.d.human_qty : r.d.human_decision === "Accept" ? r.quantity : 0);
    const approved = rows.filter((r) => r.d && (r.d.human_decision === "Accept" || r.d.human_decision === "Change quantity")).length;
    page.append(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", {}, `${lines[0].distributor_name}`), chip(d), h("span", { class: "fine" }, `${approved} of ${rows.length} lines approved · ${fmt(rows.reduce((a, r) => a + (r.d ? final(r) : 0), 0), 0)} units approved`),
      h("span", { class: "grow" }), h("button", { class: "btn", onclick: () => download(`po/${d}.csv`) }, "Draft CSV"),
      h("button", { class: "btn primary", disabled: !approved || p.stale, title: p.stale ? "Re-run the planner first" : approved ? "" : "Approve at least one line on Today", onclick: () => download(`approved/${d}.csv`) }, "Download approved PO")),
      table([{ key: "sku_name", label: "Item", render: (r) => `${r.sku_name} (${r.sku_id})` }, { key: "quantity", label: "Draft qty", num: true, render: (r) => fmt(r.quantity, 0) },
        { key: "f", label: "Final qty", num: true, render: (r) => (r.d ? fmt(final(r), 0) : "—") },
        { key: "s", label: "Status", render: (r) => (!r.d ? chip("Awaiting approval", "amber") : r.d.human_decision === "Accept" ? chip("Approved", "green") : r.d.human_decision === "Change quantity" ? chip("Changed", "blue") : chip(r.d.human_decision === "Hold" ? "On hold" : "Rejected", "red")) }], rows),
      h("p", { class: "fine", style: "margin:8px 0 0" }, "DRAFT - requires human approval. The tool never places orders.")));
  });
  const held = p.recs.filter((r) => r.rule_action === "ORDER" && r.needs_review);
  if (held.length) page.append(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", {}, "Held back from POs"), chip(held.length, "amber"), h("a", { href: "#today", class: "fine" }, "Decide on Today")),
    table([{ key: "sku_name", label: "Item", render: (r) => `${r.sku_name} (${r.sku_id})` }, { key: "distributor_id", label: "Distributor" }, { key: "recommended_qty", label: "Rule qty", num: true, render: (r) => fmt(r.recommended_qty, 0) },
      { key: "w", label: "Why held", wrap: true, render: (r) => plainReason(r.review_reasons[0]) }], held)));
  page.append(h("div", { class: "row" }, h("button", { class: "btn", onclick: () => download("action_list.csv") }, "Download full action list"), h("button", { class: "btn", onclick: () => download("exceptions.csv") }, "Download exceptions"), h("button", { class: "btn", onclick: () => download("run_report.md") }, "Download run report")));
}

/* ------------------------------------------------------------------ What-if */
async function whatif(page) {
  const p = S.plan;
  head(page, "What-if", "Try a change on one SKU and see the verdict move. Nothing here is saved.");
  const presets = await api("/api/scenario/presets");
  const sku = h("select", {}, p.recs.map((r) => h("option", { value: r.sku_id }, `${r.sku_name} (${r.sku_id})`))), out = h("div", { id: "scenario-out" });
  const run = async (body) => {
    try {
      const r = await post("/api/scenario", { sku: sku.value, ...body }), base = p.recs.find((x) => x.sku_id === sku.value);
      const col = (title, x, full) => h("div", { class: "card", style: "margin:0" }, h("div", { class: "fine" }, title), h("div", { class: "row", style: "margin:4px 0 0" }, flagChip(x.primary_flag), x.tags.map((t) => chip(t === "HIGH_DEMAND" ? "Demand surging" : t, "blue")), x.stability === "Unstable" && chip("Unstable", "amber")),
        h("div", { class: "qty", style: "margin:8px 0" }, x.action === "ORDER" ? `Order ${fmt(x.recommended_qty, 0)}` : x.action === "PAUSE ORDER" ? "Pause orders" : x.action === "REVIEW" ? "Needs your call" : x.action === "WATCH" ? "Watch" : "No action"),
        full, h("dl", { class: "kv", style: "margin-top:8px" }, [["Cover", fmt(x.cover_weeks) + " wk"], ["Weekly demand", fmt(x.weekly_demand, 0)], ["Trend", pct(x.trend)], ["Urgency", x.urgency_tier || "—"]].map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
        x.review_reasons.length && h("div", { class: "warnbox" }, x.review_reasons.map(plainReason).join("; ")));
      const g = (x) => gauge({ ...base, cover_weeks: x.cover_weeks, primary_flag: x.primary_flag, rule_action: "NONE" }, false);
      out.replaceChildren(h("div", { class: "grid", style: "grid-template-columns:1fr 1fr" }, col("Now", r.before, g(r.before)), col("If this happens", r.after, g(r.after))));
    } catch (e) { out.replaceChildren(h("div", { class: "strip err" }, e.message)); }
  };
  page.append(h("div", { class: "card" }, h("label", {}, "SKU", sku), h("div", { class: "row", style: "margin-top:12px" }, presets.map((x) => h("button", { class: "btn", onclick: () => run({ preset: x.key }) }, x.label.replace(/\s*\(.*\)/, "")))),
    h("details", {}, h("summary", {}, "Custom scenario"), customForm(run))), out);
  const fp = h("div"); page.append(h("details", { class: "card", style: "margin-top:14px" }, h("summary", { style: "margin:0;font-weight:600;color:var(--ink)" }, "Is the 25% festival uplift right? Compare with the data"), fp));
  const overrides = {};
  const loadF = async () => {
    const d = await post("/api/festival", { overrides });
    if (!d.available) return fp.replaceChildren(h("p", { class: "muted" }, "No festival weeks in the data."));
    fp.replaceChildren(h("div", { class: "warnbox" }, "Three festival weeks is a signal, not evidence."),
      h("p", { class: "fine" }, `Festival weeks ${d.festival_weeks} vs ${d.baseline_weeks}: median lift ${pct(d.median_all)}. Only ${d.above_20.join(", ") || "no SKU"} is above +20%. Raghav's rule applies +25% to everything.`),
      h("div", { class: "grid" }, d.categories.map((c) => h("label", {}, `${c.Category}: data says ${pct(c.median_lift)}`, h("input", { type: "number", step: 0.05, min: 1, max: 2, value: d.overrides[c.Category] ?? d.configured_default, onchange: (e) => { const v = e.target.value; if (v === "" || !(+v >= 0.5 && +v <= 3)) { delete overrides[c.Category]; e.target.value = d.overrides[c.Category] ?? d.configured_default; toast("Uplift must be between 0.5 and 3.", true); } else overrides[c.Category] = +v; } })))),
      h("div", { class: "row", style: "margin:10px 0" }, h("button", { class: "btn primary", onclick: loadF }, "Apply what-if")),
      table([{ key: "sku_name", label: "SKU", render: (r) => `${r.sku_name} (${r.sku_id})` }, { key: "configured_flag", label: "Now", render: (r) => flagChip(r.configured_flag) }, { key: "whatif_flag", label: "With your uplifts", render: (r) => flagChip(r.whatif_flag) },
        { key: "c", label: "", render: (r) => (r.configured_flag !== r.whatif_flag ? chip("changed", "amber") : "") }], d.compare));
  };
  loadF().catch(() => {});
}
function customForm(run) {
  const n = h("input", { type: "number", value: 4, min: 1, max: 16 }), m = h("input", { type: "number", value: 1, step: 0.05, min: 0 }), o = h("input", { type: "number", placeholder: "unchanged" }),
    l = h("input", { type: "number", step: 0.5, placeholder: "unchanged" }), f = h("input", { type: "checkbox" });
  return h("div", { style: "margin-top:10px" }, h("div", { class: "grid" }, h("label", {}, "Last N weeks", n), h("label", {}, "Demand multiplier", m), h("label", {}, "On-hand", o), h("label", {}, "Restock time (weeks)", l), h("label", {}, "Festival uplift off", f)),
    h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "btn primary", onclick: () => { const bad = (n.value === "" || +n.value < 1 || +n.value > 52 || !Number.isInteger(+n.value)) ? "Last N weeks must be a whole number from 1 to 52."
        : (m.value === "" || +m.value < 0 || +m.value > 10) ? "Demand multiplier must be between 0 and 10." : (o.value !== "" && +o.value < 0) ? "On-hand can't be negative." : (l.value !== "" && (+l.value < 0 || +l.value > 52)) ? "Restock time must be between 0 and 52 weeks." : "";
      if (bad) return $("#scenario-out").replaceChildren(h("div", { class: "strip err" }, bad)); run({ custom: { last_n: +n.value, multiplier: +m.value, on_hand: o.value === "" ? null : +o.value, lead_time: l.value === "" ? null : +l.value, festival_off: f.checked } }); } }, "Run")));
}

/* ------------------------------------------------------------------ SKUs */
function commitFlow(box, payload, label, after) {
  const out = h("div"); box.replaceChildren(out);
  const msg = (t, c) => out.replaceChildren(h("div", { class: "msg " + c }, t));
  if (!S.passcode) return openUnlock(null, () => commitFlow(box, payload, label, after));
  post("/api/skus/preview", { ...payload, by: S.by }).then((p) => {
    out.replaceChildren(h("div", { class: "warnbox" }, "Preview only — nothing is written until you confirm."), h("pre", {}, JSON.stringify(p.diff, null, 2)),
      h("div", { class: "msg ok" }, "Data-quality check on the proposed state: passed." + (p.gate_warnings.length ? " " + p.gate_warnings.join("; ") : "")),
      h("div", { class: "row", style: "margin-top:8px" }, h("button", { class: "btn " + (payload.action === "delete" ? "danger" : "primary"), onclick: async () => {
        try { await write("/api/skus/commit", { ...payload, by: S.by }); toast("Done: " + label + ". Re-run the planner to refresh."); await loadPlan(); after && after(); } catch (e) { msg("Nothing was written: " + e.message, "err"); } } }, "Confirm: " + label),
        h("button", { class: "btn quiet", onclick: () => out.replaceChildren() }, "Cancel")));
  }).catch((e) => { if (e.status === 401) openUnlock("That passcode didn't work."); msg("Refused, nothing written: " + e.message, "err"); });
}
const fileInto = (input, ta) => input.addEventListener("change", () => { const f = input.files[0]; if (f) f.text().then((t) => (ta.value = t)); });
async function skus(page) {
  head(page, "SKUs", "Add, change, retire or remove products. Every change is previewed, checked, logged and can be undone.");
  if (S.plan.mode !== "db") return page.append(empty("Read-only in offline mode", "Connect to Neon to manage SKUs."));
  const d = await api("/api/skus");
  const views = [["list", "All SKUs"], ["add", "Add SKU"], ["bulk", "Bulk import"], ["history", "History & undo"]];
  page.append(h("div", { class: "tabs" }, views.map(([k, t]) => h("button", { class: "btn" + (S.skuView === k ? " on" : ""), onclick: () => { S.skuView = k; render(); } }, t))));
  const box = h("div"); page.append(box);
  ({ list: skuList, add: skuAdd, bulk: skuBulk, history: skuHistory })[S.skuView](box, d);
}
function skuList(box, d) {
  const q = h("input", { placeholder: "Search", value: S.sq || "", oninput: (e) => { S.sq = e.target.value; draw(); } }), st = h("select", { onchange: (e) => { S.sst = e.target.value; draw(); } }, ["", "Active", "Discontinued"].map((o) => h("option", { value: o, selected: S.sst === o }, o || "All statuses")));
  const holder = h("div"), manage = h("div");
  const draw = () => {
    const rows = d.skus.filter((s) => (!S.sst || s.status === S.sst) && (!S.sq || (s.sku_name + s.sku_id).toLowerCase().includes(S.sq.toLowerCase())));
    holder.replaceChildren(table([{ key: "sku_id", label: "SKU" }, { key: "sku_name", label: "Name" }, { key: "distributor_id", label: "Distributor" }, { key: "perishability", label: "Shelf life", render: (r) => (r.perishability === "High" ? "Perishable" : "Normal") },
      { key: "standard_reorder_qty", label: "Std qty", num: true }, { key: "on_hand", label: "On hand", num: true, render: (r) => fmt(r.on_hand, 0) }, { key: "weeks_of_history", label: "Weeks of sales", num: true, render: (r) => (r.weeks_of_history < 12 ? chip(r.weeks_of_history + " (short)", "amber") : r.weeks_of_history) },
      { key: "status", label: "Status", render: (r) => (r.status === "Active" ? chip("Active", "green") : chip("Discontinued"))}], rows, { onrow: (r) => { S.skuSel = r.sku_id; draw(); drawManage(); }, sel: (r) => r.sku_id === S.skuSel }));
  };
  const drawManage = () => { const s = d.skus.find((x) => x.sku_id === S.skuSel); manage.replaceChildren(s ? manageSku(s, d) : h("p", { class: "fine", style: "margin-top:10px" }, "Select a SKU to edit, retire, add sales or delete it.")); };
  box.append(h("div", { class: "row", style: "margin-bottom:10px" }, h("label", { class: "grow" }, "Search", q), h("label", {}, "Status", st)), holder, manage); draw(); drawManage();
}
function manageSku(s, d) {
  const name = h("input", { value: s.sku_name }), cat = h("input", { value: s.category }), per = h("select", {}, ["Low", "High"].map((o) => h("option", { value: o, selected: o === s.perishability }, o === "High" ? "Perishable" : "Normal"))),
    qty = h("input", { type: "number", value: s.standard_reorder_qty }), dist = h("select", {}, d.distributors.map((x) => h("option", { value: x.distributor_id, selected: x.distributor_id === s.distributor_id }, `${x.distributor_id} ${x.distributor_name}`))),
    st = h("select", {}, ["Active", "Discontinued"].map((o) => h("option", { selected: o === s.status }, o))), oh = h("input", { type: "number", value: s.on_hand ?? 0 }), dt = h("input", { type: "date", value: S.plan.as_of }), out = h("div");
  const ta = h("textarea", { rows: 4, placeholder: "SKU_ID,Week,Units_Sold" }), file = h("input", { type: "file", accept: ".csv" }); fileInto(file, ta);
  const typed = h("input", { placeholder: `Type ${s.sku_id} to confirm` });
  return h("div", { class: "card", style: "margin-top:14px" }, h("div", { class: "card-h" }, h("h2", {}, s.sku_name), chip(s.sku_id)),
    h("div", { class: "grid" }, h("label", {}, "Name", name), h("label", {}, "Category", cat), h("label", {}, "Shelf life", per), h("label", {}, "Standard qty", qty), h("label", {}, "Distributor", dist), h("label", {}, "Status", st), h("label", {}, "On hand (new snapshot)", oh), h("label", {}, "Snapshot date", dt)),
    h("div", { class: "row", style: "margin-top:12px" }, h("button", { class: "btn primary", onclick: () => commitFlow(out, { action: "edit", sku_id: s.sku_id, fields: { sku_name: name.value, category: cat.value, perishability: per.value, standard_reorder_qty: qty.value, distributor_id: dist.value, status: st.value }, onhand: +oh.value !== (s.on_hand ?? 0) ? { units: oh.value, date: dt.value } : null }, "edit " + s.sku_id, () => render()) }, "Preview changes"),
      s.status === "Active" && h("button", { class: "btn", onclick: () => commitFlow(out, { action: "retire", sku_id: s.sku_id }, "retire " + s.sku_id, () => render()) }, "Retire (keeps history)")), out,
    h("details", {}, h("summary", {}, "Add new weeks of sales"), h("div", { style: "margin-top:8px" }, ta, file, h("div", { class: "row", style: "margin-top:8px" }, h("button", { class: "btn", onclick: () => commitFlow(out, { action: "append_sales", sku_id: s.sku_id, sales_csv: ta.value }, "add sales to " + s.sku_id, () => render()) }, "Preview")))),
    h("details", {}, h("summary", { style: "color:var(--red)" }, "Permanently delete"), h("div", { style: "margin-top:8px" }, h("div", { class: "warnbox" }, "Removes this SKU and its sales and stock rows. Past recommendations and decisions are kept. Retire is usually what you want."),
      h("div", { class: "row" }, typed, h("button", { class: "btn danger", onclick: () => (typed.value === s.sku_id ? commitFlow(out, { action: "delete", sku_id: s.sku_id }, "permanently delete " + s.sku_id, () => { S.skuSel = null; render(); }) : toast("Type " + s.sku_id + " exactly.", true)) }, "Preview delete")))));
}
function skuAdd(box, d) {
  const f = {}; const inp = (k, label, a = {}) => h("label", {}, label, (f[k] = h("input", a)));
  const per = h("select", {}, h("option", { value: "Low" }, "Normal"), h("option", { value: "High" }, "Perishable")), dist = h("select", {}, d.distributors.map((x) => h("option", { value: x.distributor_id }, `${x.distributor_id} ${x.distributor_name}`)));
  const ta = h("textarea", { rows: 4, placeholder: "Optional sales history: SKU_ID,Week,Units_Sold. With none, the SKU appears under 'Needs your call' as having no history." }), file = h("input", { type: "file", accept: ".csv" }), out = h("div"); fileInto(file, ta);
  box.append(h("div", { class: "card" }, h("div", { class: "grid" }, inp("id", "SKU ID"), inp("name", "Name"), inp("cat", "Category"), h("label", {}, "Shelf life", per), inp("qty", "Standard reorder qty", { type: "number", min: 1 }), inp("uom", "Unit", { value: "Units" }), h("label", {}, "Distributor", dist),
    inp("oh", "Initial on-hand", { type: "number", min: 0 }), inp("date", "As of", { type: "date", value: S.plan.as_of })), h("div", { style: "margin-top:10px" }, ta, file),
    h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "btn primary", onclick: () => commitFlow(out, { action: "add", sku_id: f.id.value, fields: { sku_name: f.name.value, category: f.cat.value, perishability: per.value, standard_reorder_qty: f.qty.value, unit_of_measure: f.uom.value, distributor_id: dist.value }, sales_csv: ta.value, onhand: { units: f.oh.value, date: f.date.value } }, "add " + f.id.value, () => { S.skuView = "list"; render(); }) }, "Preview")), out));
}
function skuBulk(box) {
  const ta = h("textarea", { rows: 6, placeholder: "SKU_ID,SKU_Name,Category,Perishability,Standard_Reorder_Qty,Unit_of_Measure,Distributor_ID,Units_On_Hand,As_Of_Date" }), file = h("input", { type: "file", accept: ".csv" }), out = h("div"); fileInto(file, ta);
  box.append(h("div", { class: "card" }, h("p", { class: "fine", style: "margin-top:0" }, "Every row is checked first; only valid rows are written, one transaction per SKU."), ta, file, h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "btn primary", onclick: async () => {
    if (!S.passcode) return openUnlock();
    try { const rep = await post("/api/skus/bulk-preview", { csv: ta.value }), ok = rep.filter((r) => r.ok).length;
      out.replaceChildren(table([{ key: "sku_id", label: "SKU" }, { key: "m", label: "Result", wrap: true, render: (r) => (r.ok ? chip("ok", "green") : chip("refused: " + r.message, "red")) }], rep),
        ok ? h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "btn primary", onclick: async () => { const res = await write("/api/skus/bulk-commit", { csv: ta.value, by: S.by }); toast(`${res.filter((r) => r.ok).length} SKU(s) written.`); await loadPlan(); } }, `Write ${ok} valid SKU(s)`)) : "");
    } catch (e) { out.replaceChildren(h("div", { class: "strip err" }, e.message)); } } }, "Check rows")), out));
}
async function skuHistory(box, d) {
  const log = await api("/api/skus/changes"), last = d.last_undoable;
  box.append(last ? h("div", { class: "card" }, h("div", { class: "row" }, h("div", { class: "grow" }, h("b", {}, "Last change: "), `${last.action} ${last.sku_id} by ${last.changed_by}`),
    h("button", { class: "btn", onclick: async () => { if (!S.passcode) return openUnlock(); try { await write("/api/skus/undo", { by: S.by }); toast("Undone."); await loadPlan(); } catch (e) { toast(e.message, true); } } }, "Undo it")))
    : h("p", { class: "fine" }, "Nothing to undo."), log.length ? table([{ key: "id", label: "#" }, { key: "changed_at", label: "When", render: (r) => new Date(r.changed_at).toLocaleString() }, { key: "changed_by", label: "By" }, { key: "action", label: "Action" }, { key: "sku_id", label: "SKU" }], log) : "");
}

/* ------------------------------------------------------------------ Scoreboard */
async function scoreboard(page) {
  head(page, "Scoreboard", "Shadow mode: the tool recommends, you decide. Agreement is how often you approved a line unchanged.");
  if (S.plan.mode !== "db") return page.append(empty("Needs the database", "The decision log isn't available offline."));
  const s = await api("/api/shadow");
  if (s.empty) return page.append(empty("No decisions yet", "Approve, change, reject or hold lines on Today and your scoreboard fills in. This is the evidence for whether the rules match your judgment."));
  page.append(h("div", { class: "kpis" }, [[s.total, "Decisions logged", ""], [Math.round(s.agreement * 100) + "%", "Agreement with the tool", "green"], [s.overrides.reduce((a, o) => a + o.count, 0), "Overrides", "amber"], [s.by_run.length, "Runs reviewed", ""]].map(([n, l, c]) => h("div", { class: "kpi " + c, style: "cursor:default" }, h("div", { class: "n" }, n), h("div", { class: "l" }, l)))));
  page.append(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", {}, "Why you overrode the tool")), s.overrides.length ? table([{ key: "human_decision", label: "Decision" }, { key: "reason", label: "Reason", wrap: true }, { key: "count", label: "Times", num: true }], s.overrides) : h("p", { class: "muted" }, "No overrides yet.")),
    h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", {}, "Decision log")), table([{ key: "decided_at", label: "When", render: (r) => new Date(r.decided_at).toLocaleString() }, { key: "sku_id", label: "SKU" }, { key: "tool_qty", label: "Tool qty", num: true, render: (r) => fmt(r.tool_qty, 0) }, { key: "human_decision", label: "Decision" },
      { key: "human_qty", label: "Final qty", num: true, render: (r) => fmt(r.human_qty, 0) }, { key: "reason", label: "Reason", wrap: true }, { key: "decided_by", label: "By" }], s.log)));
}

/* ------------------------------------------------------------------ Rules & data */
async function rules(page) {
  const p = S.plan;
  head(page, "Rules & data", "What the tool assumes, how healthy the data is, and the thresholds you can change.");
  const m = await api("/api/meta"), c = await api("/api/config"), cfg = c.config;
  const all = [...p.gate.stops, ...p.gate.warnings, ...p.gate.info];
  page.append(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", {}, "Data health"), all.length ? chip(all.length + " note(s)", "amber") : chip("All clear", "green")),
    all.map((f) => h("div", { class: "strip " + (f.level === "stop" ? "err" : f.level === "warn" ? "warn" : "info"), style: "margin-bottom:6px" }, f.message)),
    h("p", { class: "fine", style: "margin:6px 0 0" }, `${p.recs.length} SKUs · ${(p.weeks || []).length} weeks of sales · fingerprint ${p.fingerprint || "n/a"}`)));
  const fields = [["festival_uplift_default", "Festival uplift", 0.05], ["high_demand_trend", "Demand-surge trigger (rise over 12-wk avg)", 0.05], ["stockout_weeks", "Running low below (weeks)", 0.5], ["overstock_weeks", "Overstocked above (weeks)", 0.5], ["target_cover_weeks", "Target cover after an order (weeks)", 0.5], ["cv_threshold", "Volatility limit (CV)", 0.05]];
  const inputs = fields.map(([k, l, st]) => h("label", {}, l, h("input", { type: "number", step: st, value: cfg[k], "data-k": k }))), low = h("input", { type: "number", step: 0.5, value: cfg.lead_time_weeks.Low }), high = h("input", { type: "number", step: 0.5, value: cfg.lead_time_weeks.High }), note = h("input", { placeholder: "Why are you changing this? (required)" }), msg = h("div", { class: "msg" });
  page.append(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", {}, "Thresholds"), h("span", { class: "fine" }, "Saved as a new version with your note. Re-run the planner to apply.")),
    h("div", { class: "grid" }, inputs, h("label", {}, "Restock time, normal (weeks)", low), h("label", {}, "Restock time, perishable (weeks)", high)), h("div", { class: "row", style: "margin-top:12px" }, h("label", { class: "grow" }, "Note", note),
      h("button", { class: "btn quiet", disabled: c.mode !== "db", onclick: async () => { if (!S.passcode) return openUnlock(); if (!confirm("Replace all thresholds with the original defaults?")) return;
        try { await write("/api/config", { reset: true, note: note.value.trim() || "Reset to defaults" }); toast("Defaults restored. Re-run the planner to apply."); await loadPlan(); } catch (e) { toast(e.message, true); } } }, "Reset to defaults"),
      h("button", { class: "btn primary", disabled: c.mode !== "db", onclick: async () => { if (!S.passcode) return openUnlock(); const changes = { lead_time_weeks: { Low: +low.value, High: +high.value } }; inputs.forEach((l) => { const i = l.querySelector("input"); changes[i.dataset.k] = +i.value; });
        try { await write("/api/config", { changes, note: note.value }); msg.className = "msg ok"; msg.textContent = "Saved. Re-run the planner to apply."; await loadPlan(true); } catch (e) { msg.className = "msg err"; msg.textContent = e.message; } } }, "Save version")), msg,
    c.history.length ? h("details", {}, h("summary", {}, "Version history"), table([{ key: "id", label: "#" }, { key: "created_at", label: "Saved", render: (r) => new Date(r.created_at).toLocaleString() }, { key: "note", label: "Note", wrap: true }], c.history)) : h("p", { class: "fine" }, "Defaults in use; no saved versions.")));
  page.append(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", {}, "Assumptions")), h("ul", { style: "margin:0;padding-left:18px;font-size:14px" }, m.assumptions.map((a) => h("li", {}, a)),
    ["ISO weeks; a week belongs to the month of its Thursday.", "A healthy SKU with surging demand is flagged Watch; the 1.5× floor applies only when stock is also low.", "Orders are whole multiples of the standard quantity that restore cover; the brief's literal quantity is shown beside it."].map((a) => h("li", {}, a)))),
    h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", {}, "Limitations")), h("ul", { style: "margin:0;padding-left:18px;font-size:14px" }, m.limitations.map((a) => h("li", {}, a)),
      h("li", {}, "The tool never places orders. The optional Claude layer only rewords explanations; any number not in the engine's facts makes it fall back to the template."))));
}

/* ------------------------------------------------------------------ boot */
window.addEventListener("hashchange", () => { S.form = null; render(); });
setInterval(() => { if (!document.hidden && !document.activeElement.matches("input,textarea,select") && !S.form) loadPlan(true); }, 30000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && !S.form) loadPlan(true); });
if (S.passcode) post("/api/auth").catch((e) => { if (e.status === 401) { S.passcode = ""; safe(() => sessionStorage.removeItem("passcode")); paintHeader(); } });
paintNav(); paintHeader(); loadPlan(); api("/api/meta").then((m) => (S.llm = m.llm_available)).catch(() => {});
