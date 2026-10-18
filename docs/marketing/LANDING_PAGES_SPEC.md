# Campaign Landing Pages — Specification

Implemented: `templates/campaigns/horeca.html`, `templates/campaigns/insurance.html`, view
`gemiapp.views.campaign_landing`, routes in `gemiapp/urls.py`, CSS block «CAMPAIGN LANDING PAGES» at the end of
`static/css/product-ui.css`, tests `gemiapp/test_campaigns.py`.

| | HoReCa suppliers | Insurance advisors |
|---|---|---|
| URL | `/for/horeca/` (`landing_horeca`) | `/for/insurance/` (`landing_insurance`) |
| Campaign id | `gl_horeca` | `gl_insurance` |
| H1 | «Βρες νέες επιχειρήσεις που μπορεί να γίνουν πελάτες σου.» | «Ανακάλυψε νέες επιχειρήσεις για πιθανές ασφαλιστικές συνεργασίες.» |
| Organising idea | product line → ΚΑΔ → distribution territory | legal form + νομός → follow-up pipeline |
| Primary CTA | ΞΕΚΙΝΑ ΔΩΡΕΑΝ → `/signup/` + carried UTMs | same |
| Indexing | indexable, in sitemap, `Allow: /for/` | same |

## Shared rules

- Extends `base.html` → public topbar, footer, consent banner, GA (consent-gated).
- Every capability shown is VERIFIED in `PRODUCT_AUDIT.md`; paid-only items say «με πληρωμένο πλάνο».
- Sample rows are fictional, labelled «ΕΙΚΟΝΙΚΟ ΔΕΙΓΜΑ». ΚΑΔ codes used exist in `gemiapp/data/kad_2025.json`.
- Prices are **not** hard-coded on these pages (link to /pricing/), so a price change cannot leave them wrong.
- One ink (primary) button per screen; secondary actions are text buttons.
- Every signup link is `{% url 'signup' %}{{ signup_query }}` — **required** for attribution.
- CTAs carry `data-analytics="landing_cta_click" data-analytics-audience=… data-analytics-cta-location=…`.
- Mobile: same document order; no element wider than 375px (verified: `scrollWidth == innerWidth`).
- Performance: no new images, fonts, scripts or libraries; ~4 KB of extra CSS.

## Page A — HoReCa (`/for/horeca/`)

1. **Hero** — eyebrow ΓΙΑ ΠΡΟΜΗΘΕΥΤΕΣ HORECA; H1; copy (restaurants, cafés, bars, lodging, catering publish
   their incorporation to GEMI; Radar delivers matches each morning); CTA; factual strip
   (source · ενδεικτικοί ΚΑΔ 56.11/56.30/56.21/55.10 · email 09:00); Signals register with 2 matches + 1 non-match
   («Εκτός περιοχής του Ραντάρ») to show *why* things match.
2. **How a supplier sets it up** — `.public-chain` ×4: territory → ΚΑΔ (word search, ≤25 codes) → 09:00 email
   → status & notes.
3. **Product line → ΚΑΔ** — coffee, food wholesale, beverages, packaging, professional equipment, each with
   suggested starting codes; explicit note that matching is exact-code and the user chooses.
4. **Radar example** — two Radars of a coffee supplier (Αττική / Θεσσαλονίκη) in the real Radar-row grammar.
5. **Does / does not promise** — a registration ≠ an open shop or a supplier search; no invented contacts; no
   scoring; no outreach on the user's behalf.
6. **Plans** — Free vs paid in two lines, link to /pricing/.
7. **Responsible contact notice** — data protection & direct marketing law; sole proprietors are natural persons.
8. **Final CTA** — «Δημιουργία δωρεάν λογαριασμού».

## Page B — Insurance (`/for/insurance/`)

1. **Hero** — eyebrow ΓΙΑ ΑΣΦΑΛΙΣΤΙΚΟΥΣ ΣΥΜΒΟΥΛΟΥΣ & ΓΡΑΦΕΙΑ; H1; copy (legal form, seat, sector → you decide
   whether a first contact is worth it); CTA; **«Χωρίς υποσχέσεις» notice above the fold** (a registration is not an
   insurance need or purchase intent); register where a sole proprietorship is shown *not* matching.
2. **Segments** — three example Radars: new Ι.Κ.Ε./Α.Ε. in Αττική; technical companies (43.21) in Θεσσαλονίκη;
   retail (47.11) across two Cretan νομοί — each with a «Γιατί έτσι» rationale, none implying a need.
3. **Follow-up pipeline** — the real statuses (Νέο, Προβλήθηκε, Επικοινώνησα, Ενδιαφέρεται / Δεν ενδιαφέρεται,
   Αρχείο).
4. **Before the first contact** — what the dossier shows / what the product does not do (no risk or needs
   assessment, no invented contacts, no messages on your behalf, no scoring).
5. **Plans**, **responsible-contact notice** (adds insurance-distribution rules), **final CTA**.

## Proposed (not implemented)

- Real screenshots/short loops of the Radar form inside the pages (needs a recorded asset from seed data).
- A/B test of CTA copy «ΞΕΚΙΝΑ ΔΩΡΕΑΝ» vs «ΔΕΣ ΤΙΣ ΝΕΕΣ ΕΓΓΡΑΦΕΣ ΤΗΣ ΠΕΡΙΟΧΗΣ ΣΟΥ» (see CRO plan).
- Links from the home page «Σε ποιους χρησιμεύει» list to the two audience pages.
