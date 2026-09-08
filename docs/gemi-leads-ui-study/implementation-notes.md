# Implementation notes and risks

## Existing components and behavior to reuse

- `base.html` authentication, messages, legal/footer links, impersonation banner, and CSRF forms.
- Dashboard view context, company filtering, result counts, seven-day data (as text/context rather than a decorative chart), realtime-tier update data, and 20-row pagination.
- `includes/dashboard_rows.html` data contract and infinite-scroll behavior.
- KAD picker markup/JavaScript and KAD catalog endpoints.
- Radar CRUD, toggle, delete, preview, export, limit enforcement, annotations (`match_count`, `latest_match`), and match status filtering.
- Lead search, status/Radar/favorite filters, CSV export, deduplication, and ownership rules.
- Lead status, favorite, and notes POST endpoints and forms.
- Company `source_url`, `people`, contact gating, activities, `RadarMatch` reason fields, and timestamps.
- Shared accessibility protections: visible focus, reduced motion, touch targets, mobile menu semantics, and disabled states.

## Visual components to replace

- Four-card Dashboard KPI strip → inline daily ledger summary.
- Decorative seven-day bar chart → compact daily counts integrated into the time axis or a small factual sparkline only if it aids comparison.
- Rounded Radar cards → aligned Radar definitions with criteria clauses and activity columns.
- Three Lead Inbox metric cards → filter-linked textual totals in the queue header.
- Repeated status pills → status text, a narrow state bar, or a single controlled select.
- Business-detail card stack → dossier sections separated by rules and alignment.
- Generic blue primary actions → dark ink actions plus a restrained amber relevance accent.
- Gradient/glass navbar and logo tile → flat product mark and typographic navigation.
- Emoji icons → plain labels or a single coherent icon set where meaning genuinely benefits.

## Implementation risks

- Dashboard terminology: changing “Dashboard” to “Signals” improves product truth but may affect learned navigation and tests that assert visible copy.
- Existing templates combine presentation and dense Django conditional logic. Refactor into includes before production restyling to prevent divergence between first-page and AJAX rows.
- Mobile archive styles currently depend on positional `nth-child` selectors; column changes can silently mislabel values.
- Contact/people subscription gates must remain identical across every dossier layout.
- A fixed contextual pane needs careful keyboard focus order and should degrade to a normal route on smaller screens.
- Status changes, notes, favorites, Radar toggles, and deletion must remain POST-only with CSRF. The prototype deliberately does not model network behavior.
- The chart data and pipeline health are real, but reducing their prominence must not hide operational failure states from users who currently rely on them.
- Existing dark-mode remapping in `input.css` is broad and selector-based; a production redesign should replace it with semantic tokens rather than layer more overrides.
- Full visual implementation will touch snapshot/copy assertions even without changing backend logic; tests must distinguish semantic behavior from old class strings.

## Remaining questions before implementation

1. May the authenticated navigation label “Dashboard” become “Signals,” while retaining the `/dashboard/` route?
2. Is ingestion/pipeline health user-facing decision information, or can it move to a quiet status line except on failure?
3. Should archived leads remain inside the Leads filter, as currently implemented, or receive stronger visual separation without becoming a new route?
4. Which density should be the default for production: the recommended medium-high ledger or the denser Operational Index?
5. Should the seven-day registration series remain as compact factual context, or be removed from the primary viewport entirely?

