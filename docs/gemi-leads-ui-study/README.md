# Gemi Leads UI art-direction study

Status: **Signal Ledger approved; focused refinement ready for visual review — no production implementation**

This study treats the running Django application, its models, views, URLs, templates, and existing JavaScript as the source of truth. It preserves the product flow:

`GEMI registration event → public company information → Radar criteria → explained match → user-managed lead`

## Recommendation

1. **Signal Ledger** — approved governing direction. A chronological intelligence register with a visible event-to-lead chain.
2. **Operational Index** — strongest for high-volume expert scanning, but more austere.
3. **Business Briefing** — clearest for occasional users, but slower at large lead volumes.

Open `prototype/index.html` for the interactive low-fidelity prototype. It contains Dashboard, Radar view, and Lead detail states and uses anonymized data shaped only from fields already present in the application.

## Contents

- `current-ui-audit.md` — evidence-based audit of the existing templates and UI architecture.
- `design-directions.md` — three distinct systems, desktop/mobile behavior, ranking, and rationale.
- `implementation-notes.md` — reuse/replacement map, risks, and unanswered questions.
- `anti-ai-audit.md` — explicit review against the brief's anti-pattern checklist.
- `refinement-review.md` — approved-direction hierarchy, state grammar, mobile behavior, and capture manifest.
- `mockups/` — desktop and mobile captures for all three directions.
- `prototype/` — local, standalone prototype; it does not call or modify the backend.

## Prototype route

When served from this directory: `/prototype/`.

The prototype is deliberately local and static. All buttons change only in-memory demo state, and links never reach billing, GEMI, email, or production endpoints.
