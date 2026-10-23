# Design notes

## Rules as implemented, and every interpretation choice

| Brief rule | Implementation | Choice made |
|---|---|---|
| Metrics | `last4_avg`, `avg12`, `trend = last4/avg12 - 1`, `weekly_demand = avg12 x uplift`, `cover = (on_hand + open_po) / weekly_demand` | avg12 includes the last 4 weeks. The trend test uses raw sales. Uplift is looked up from the as-of date's month (Oct 2026 = festival), per category if overridden. |
| Primary flag | STOCKOUT_RISK if cover < min, else OVERSTOCK_RISK if cover > max, else OK | Non-perishable 6/14 weeks; High perishability 3/4, which always replaces 6/14. A supplied shelf life can only lower the perishable maximum. |
| HIGH_DEMAND | tag when trend > 20% | Tag only. With STOCKOUT it forces at least 1.5x standard. With healthy cover it becomes action WATCH, not an order (ordering at 1.5x with adequate cover would push toward overstock). |
| Overstock conflicts | OVERSTOCK with trend >= +10%, or with HIGH_DEMAND, becomes REVIEW | Strict `>=`: SKU007's trend is +9.88%, so this rule does not fire for it. |
| Sizing | smallest whole multiple k of standard qty with cover in [target 8, 14] (non-perishable); perishable picks k with cover in [3, 4] closest to 3.5 | Literal brief quantity (standard, or 1.5x) is stored beside it with the cover it leaves. If no k fits, REVIEW. |
| Urgency | `cover - lead_time`; CRITICAL if <= 0; stockout orders otherwise HIGH; OK SKUs within 2 weeks of their minimum are MEDIUM, else LOW | The 2-week margin is configurable. |
| Stability | flag recomputed under 4 readings; any disagreement sets REVIEW and lists the readings | Reading 2 uses the 12 weeks before the last 4 (W27-W38 on this data). |
| Exceptions | history < 12 weeks, zero or missing weeks, CV > 0.30 over the last 12 weeks, outlier weeks, on-hand <= 0, inactive distributor, no history | Outlier test: a week in the last 12 is flagged if more than 3 scaled MADs from the median of the preceding 8 weeks. MAD is floored at 10% of the median (and 1 unit) so Poisson-sized noise is not flagged. This floor is my choice, raised from 5% after SKU009's W34 triggered it. |
| REVIEW | any review or exception reason makes `action = REVIEW`; `rule_action` keeps what the rules alone said | REVIEW lines are excluded from draft POs. |

## Findings from running the rules on the pilot data

1. **A uniform 25% uplift is contradicted by the data.** Festival weeks (W40-W42) against earlier non-festival weeks (W27-W39) give a median lift of about 3%; only SKU001 is above 20% (+32%). Three weeks is a signal, not evidence, but the configured 25% is not supported by it. The Scenario lab lets a user set uplift per category and watch every flag move.
2. **The brief's quantities often do not restore cover.** SKU003 stays at about 6.0 weeks and SKU013 at about 5.3 weeks after the literal quantity, right at the stockout line. Whole-multiple sizing gives 9.1 and 9.5 weeks.
3. **Verdicts are sensitive to interpretation.** SKU002 is OK with the uplift and OVERSTOCK (14.3 weeks) without it. SKU007 is OVERSTOCK on the base reading and OK when the last-4-week average is the base.
4. **The perishable trap.** SKU004 (180 on hand, standard 150): rounding up to 2 multiples gives 4.8 weeks, over the 4-week cap; 1 multiple gives 3.3 weeks. Blind round-up would breach shelf life.
5. **SKU001 is both the clearest stockout and an exception.** Its step change in W39-W42 trips the outlier check, so it is held for review and gets no draft PO even though the engine recommends 1,500 units.
6. **Results that differ from the brief's wording.** The brief says SKU007 has a rising trend of about +10% and must end as REVIEW. The trend is +9.9%, below the rule's threshold, so it ends as REVIEW because its verdict is unstable, not because of the trend rule. No test was edited to pass.

## Limitations

See the README; the list is shared with the app's Assumptions & limitations page (`src/planner/limitations.py`) and a test checks they match.

## Architecture

`src/planner/engine.py` and the modules it imports are pure DataFrame functions. Only `store.py` and `db.py` touch Postgres. All master-data changes go through `store.apply_change`, one transaction each, with a data-quality gate dry run on the proposed state, a `sku_changes` before-image row, and undo from that image.
