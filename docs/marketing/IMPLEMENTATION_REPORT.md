# Implementation & Verification Report — campaign infrastructure (2026-10-18)

Branch `claude/gemi-leads-audit-campaign-016e37`. **Nothing deployed, nothing committed to `main`, no production
command run, no migration.**

## Implemented (code)

| Area | Files |
|---|---|
| Landing pages `/for/horeca/`, `/for/insurance/` | `templates/campaigns/horeca.html`, `templates/campaigns/insurance.html`, `gemiapp/views.campaign_landing`, `gemiapp/urls.py` |
| Landing CSS (`.public-radar-sample`, `.public-fit`, `.cookie-settings-link`) | end of `static/css/product-ui.css` |
| Sitemap + robots (`Allow: /for/`) | `gemiapp/seo.py` |
| Attribution (UTM sanitising, carried in URL only) + whitelisted one-shot events | `gemiapp/campaigns.py` (new), `gemiapp/context_processors.py` |
| Events `sign_up` (email + Google/Apple), `email_verified`, `product_search`, `radar_created`, `sign_up_start`, `landing_cta_click` | `gemiapp/views.py`, `gemiapp/adapters.py`, `templates/registration/signup.html`, `static/js/app.js`, `templates/includes/analytics.html` |
| Generic `data-analytics-*` → GA params (existing `pricing_cta_click`/`tier` unchanged) | `static/js/app.js` |
| Consent withdrawal (footer «ΡΥΘΜΙΣΕΙΣ COOKIES», privacy page) | `templates/base.html`, `templates/legal/privacy.html`, `templates/includes/analytics.html` |
| Home links to the two audience pages | `templates/home.html` |
| Tests (24) | `gemiapp/test_campaigns.py` |

Not changed: authentication logic, verification, billing/Stripe, entitlements, digests, ingestion, any model.

## Verified

- `manage.py test --noinput --parallel 4`: **2.508 tests OK** (24 new). `manage.py check` clean,
  `makemigrations --check` → no changes, `node --check static/js/app.js` OK.
- Local server on a throwaway SQLite DB (no staging/production connection): both pages render in the Signal Ledger
  system on desktop; at 375px `scrollWidth == innerWidth` (no horizontal scroll) on both; one mobile ordering bug in
  the Radar example found and fixed.
- Tests prove: UTMs sanitised (emails/free text dropped), signup links carry them, anonymous landing visits create
  no session, `sign_up` carries method + campaign but no email, event markup absent when GA is off, events are
  one-shot, `product_search` sends filter kinds not values, `radar_created` only on creation.
- Live site (read-only): GA4 `G-H9YRV774X0` present and consent-gated; /pricing/ shows Free + €19/49/99; Google
  sign-in live; `/for/horeca/` currently 404 (expected until deploy).

## Not verified / remains

- GA4 events in a real property (DebugView) — needs deploy + the QA checklist in `ANALYTICS_TRACKING_PLAN.md`.
- GA4 admin setup (custom dimensions, key events, funnel) — manual, in the GA UI.
- `purchase` tracking — proposed (Measurement Protocol from the Stripe webhook), not built.
- Copy fixes on the home page and privacy page (`PRODUCT_AUDIT.md` §4) and CRO items 🟡/🔴 — proposed only.
- Apple sign-in on production, production phone-number coverage — not verified.

## To ship (needs your approval)

1. Review the pages locally or on staging; adjust copy.
2. Merge the branch to `main` → Render deploy (no migration; `preDeployCommand` is a no-op for this change).
3. Run the analytics QA checklist on production with a test UTM link, then configure GA4 key events.
4. Start ads with `utm_campaign=gl_horeca|gl_insurance` and `utm_content=gl004…gl007`.
