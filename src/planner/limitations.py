"""Single source for the limitations list: used by the app page, and checked against README and DESIGN.md by a test."""
LIMITATIONS = [
    "It automates Raghav's rules; it is not a forecast and has no machine learning.",
    "16 weeks of history means no seasonality can be learned and the festival uplift cannot be fitted.",
    "Lead time, shelf life and open POs are assumptions; money at risk is unknown without unit cost.",
    "Sales understate demand when stock runs out.",
    "SKU-level and single-warehouse; no distributor-level early warning (sales are not given by distributor).",
    "Data enters by CSV ingest, not a live SAP/Zoho connection.",
    "The data looks cleaner than real SAP exports will be.",
    "Single-user app with no login or permissions; fine for a pilot, not for production.",
    "No evidence yet that it outperforms the human planner; shadow mode exists to produce that evidence.",
    "Neon free tier may pause when idle, so the first request can be slow.",
]
