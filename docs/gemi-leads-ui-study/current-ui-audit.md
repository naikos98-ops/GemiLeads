# Current UI audit

## Scope inspected

The audit covers `base.html`, `dashboard.html`, dashboard rows, Radar list/detail/form, Lead Inbox, business detail, shared Tailwind tokens, client JavaScript, URL definitions, view context, and the `Company`, `CustomerRadar`, `RadarMatch`, and `UserCompanyLead` models.

The application is server-rendered Django with Tailwind and a small shared JavaScript file. It already has good functional separation and does not need an architectural rewrite for a visual redesign.

## What currently reads as generic or AI-generated

### Dashboard composition

- The page opens with a generic greeting and four equally prominent KPI cards. This is precisely the familiar generated-SaaS pattern called out in the brief.
- A seven-day bar chart occupies prime space but does not explain relevance, Radar matches, or the next business to inspect.
- The pipeline panel exposes ingestion health as a decorative companion card. It does not advance the user's daily sales-discovery task.
- Lead Inbox and the company archive are separate rounded white containers, so the causal chain between a new registration and a Radar match is visually broken.
- Large top spacing and repeated `p-6` surfaces lower useful information density.

### Navigation

- `Dashboard / Leads / Radars / Settings` is functionally valid, but the navigation gives every area equal semantic weight and does not express signals → matches → leads.
- The dark translucent navbar, gradient logo tile, pills, blur, and universal rounded actions feel like a broad SaaS shell rather than a market-intelligence instrument.
- English and Greek terminology is mixed without a deliberate role: Dashboard, Lead Inbox, Radars, Pipeline, Live data, and Greek body labels.

### Radar representation

- Radars are presented as settings-like cards. Criteria, operating state, latest activity, and outputs are not aligned as a single inspectable object.
- The matching rule is semantically important but distributed across chips and descriptive text.
- Match provenance is visible only after opening a Radar or business, rather than acting as the bridge between signal and lead.

### Lead Inbox and business detail

- Inbox headline metrics repeat the KPI-card pattern.
- Status and Radar identity rely heavily on rounded pills, even when plain text in aligned columns would scan faster.
- Business detail is split into several unrelated white cards: company data, people, activities, match reason, status, notes, and history.
- The most differentiating part—“why it was found”—sits below generic company data instead of appearing near the identity and event.
- Emoji lock, star, crown, and mail marks mix with outline SVGs, adding visual noise and inconsistent tone.

## Weak hierarchy and hidden product logic

- `EVENT`: incorporation date is present, but is usually metadata rather than the organizing axis.
- `INFORMATION`: public GEMI identity is well represented but fragmented across surfaces.
- `CRITERIA`: Radar criteria are visually separated from the business that satisfied them.
- `RELEVANCE`: match reason exists in `RadarMatch.match_reason` and `matched_activity_codes`, yet it is treated as secondary chips.
- `LEAD`: lead state and notes are strong actions, but appear as a generic CRM sidebar rather than the final step of the intelligence chain.

## Density and spacing

- Repeated card padding, generous gaps, and rounded containers mean the desktop viewport shows fewer actionable records than the underlying table architecture permits.
- Mobile archive rows are converted into cards, retaining the container-heavy grammar instead of designing a dedicated scan row.
- Large headings work for occasional pages, but consume too much vertical space for daily operational use.

## Patterns to preserve

- Server-rendered Django routes, ownership isolation, and POST-only mutations.
- The existing information architecture and labels as routes: Dashboard, Leads, Radars, Settings. Visual language can clarify their relationship without inventing areas.
- Accessible KAD autocomplete: debounced search, keyboard navigation, selected items, and the 25-code limit.
- Inclusive date filters, KAD OR logic, and CSV exports that retain active filters.
- Infinite-scroll pagination of 20 company records.
- Lead deduplication across Radars.
- Existing status taxonomy: New, Viewed, Contacted, Interested, Not interested, Archived.
- Favorite, private notes, source link, and history.
- Subscription-gated contact and people data.
- Active/paused Radar behavior, soft deletion, preview, and Radar limits.
- Responsive tables and established focus/reduced-motion protections.
- Current light/dark contrast remediation and disabled-state behavior.

