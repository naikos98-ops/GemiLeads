# Approved-direction refinement

The approved **Signal Ledger** direction remains the governing system. This pass changes hierarchy and responsive behavior only; it does not introduce a new direction, route, field, filter, state, or backend action.

## Refinements

- Signal rows now use one fixed state column. Line length, weight, label weight, and a restrained amber segment distinguish signal, new, viewed, contacted, and interested states. The separate amber vertical rule continues to mean “matched by a Radar.”
- The selected-signal panel now reads identity → public metadata → event/criteria/lead chain → match evidence → primary action. Matching evidence has more vertical space and a short factual explanation.
- Sidebar numbers were replaced by product-stage labels: Market, Criteria, Outcome, and System. Each destination explains its role, while a quiet source → match → lead line makes the Gemi Leads logic explicit.
- Top metadata now says GEMI intake and last source sync. This keeps live-market provenance while avoiding trading-terminal language.
- Amber is reserved for signal relevance and current selection. Primary actions use dark ink.
- Mobile Signals is a queue with an inline selected-business reveal directly after the selected row. The desktop inspection column is not stacked below the whole list.
- Mobile Lead detail places relevance and lead status before the longer public record, preserving the primary decision flow.

## State grammar

| State | Primary treatment |
| --- | --- |
| Signal | Short, faint rule; regular label; no match rail |
| Matched signal | Persistent amber rail between timestamp and business identity |
| New | Full-width amber rule; strong label; “unreviewed” descriptor |
| Viewed | Half-width thin rule; muted label |
| Contacted | Double dark rule; medium-weight label |
| Interested | Heavy dark rule ending in an amber segment; strongest label |
| Lead | Appears as the outcome in the event → criteria → lead chain and as the actionable state in the dossier |

## Anti-AI review

Passed. The refinement introduces no KPI cards, rounded dashboard cards, chart, floating panel, bento composition, blue default action, status pill, shadow system, or decorative icon navigation. Information remains aligned by time, rule, typography, and position.

## Review captures

- `screenshots/refined-signals-desktop.png`
- `screenshots/refined-radar-desktop.png`
- `screenshots/refined-lead-desktop.png`
- `screenshots/refined-signals-mobile.png`
- `screenshots/refined-lead-mobile.png`

