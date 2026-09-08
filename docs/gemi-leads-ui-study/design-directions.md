# Three product UI directions

## Ranking

1. Signal Ledger
2. Operational Index
3. Business Briefing

## 1 — Signal Ledger (recommended)

**Concept:** a live market register where time is the primary axis and every relevant registration carries an explicit path from source event to lead.

- Layout: compact text navigation, date/time rail, chronological signal rows, and a contextual inspection pane on wide screens.
- Hierarchy: date → business identity → matched Radar → exact reasons → lead state.
- Typography: neutral grotesk for working text; tabular numerals and monospaced GEMI/KAD identifiers.
- Density: medium-high, with 56–76 px signal rows and no outer card per record.
- Surfaces: mostly flat ground, strong hairlines, one dark inspection/action strip.
- Interaction grammar: select a signal to inspect; expand match evidence inline; lead actions stay attached to the selected record.
- Signal representation: timestamped entries on a vertical registration rail. Radar matches interrupt the rail with a narrow amber relevance marker.
- Radar representation: an executable definition—criteria on the left, health/activity in aligned columns, matched businesses below as a ledger.
- Dossier: a stable identity header followed by a two-column public record and a right-side “relevance to lead” chain.

Mobile uses a bottom destination bar and a single-column signal queue. Time, company name, match reason, and lead state remain visible; the full record opens as a sheet-like detail page, not stacked dashboard cards.

Why it wins: it makes new registrations and explained relevance the unmistakable product, supports repeated scanning, and maps cleanly onto the current Django data without new features.

## 2 — Operational Index

**Concept:** a systematic registry for expert users who think in records, codes, filters, and queues.

- Layout: narrow top command band, dense indexed table, persistent criteria column, and record drawer.
- Hierarchy: record number/GEMI → incorporation date → business → region/legal form → Radar → state.
- Typography: condensed sans for labels and a mono face for codes and dates.
- Density: highest of the three; optimized for comparison and large volumes.
- Navigation: horizontal register tabs with explicit counts embedded as text, not cards.
- Surfaces: flat gray-white canvas, dark rules, minimal tint for selection and warnings.
- Interaction grammar: keyboard-like row selection, sort/filter band, right-hand record drawer.
- Signal representation: indexed table rows; relevant matches are marked in a dedicated `MATCH` column with the matching criterion summarized.
- Radar representation: rule-sheet rows with criteria expressed as readable boolean clauses.
- Dossier: a numbered public record with field groups separated by horizontal rules.

Mobile becomes a compact record index: two-line rows, an anchored filter action, and full-page record inspection. Secondary columns are not blindly stacked; they are moved into a labeled reveal.

Tradeoff: excellent throughput, but the austere registry tone risks feeling more governmental than sales-oriented.

## 3 — Business Briefing

**Concept:** an editorial daily briefing that frames the market change first and turns each matched business into a concise dossier.

- Layout: top masthead, “today” briefing column, selected company story, and quiet Radar index.
- Hierarchy: market change summary → significant matched businesses → evidence → action.
- Typography: serif business names/headlines paired with a disciplined sans for facts and actions.
- Density: medium; more reading-oriented than the other two.
- Navigation: top edition-style sections; current destination underlined rather than boxed.
- Surfaces: paper-like canvas, section rules, selective ink blocks for actions.
- Interaction grammar: move through the daily edition, open a business brief, then update lead state.
- Signal representation: chronological “dispatches” grouped by morning/afternoon with narrative-free factual decks.
- Radar representation: named watch briefs with criteria, latest hit, and a compact activity strip.
- Dossier: editorial lead, public-record facts, activities, people, and match explanation in a reading sequence.

Mobile behaves like a briefing reader: a compact edition header, swipe-sized dispatch rows, and a single persistent lead action. It avoids converting every desktop column into a card.

Tradeoff: distinctive and trustworthy, but slower for operators processing dozens of records in one sitting.

## Navigation recommendation

Keep the four confirmed destinations and their route semantics. Present them as:

- **Signals** (label for the existing Dashboard route; implementation decision still needs approval because it changes visible terminology, not functionality)
- **Leads**
- **Radars**
- **Settings**

If terminology must remain unchanged, keep “Dashboard” and add the contextual descriptor “New signals” inside the page. Do not add a separate Matches route: matches already belong within Dashboard, Radars, and business detail.

