# Gemi Leads — Product Audit (marketing source of truth)

**Audit date:** 2026-10-18 · **Code base:** branch `claude/gemi-leads-audit-campaign-016e37` (= `main` + LIVE-cutover
work, dormant) · **Live site checked:** https://gemileads.gr (home, /pricing/, /signup/, robots, GA tag).

> **Rule for every future campaign:** a capability may appear in an ad, video, landing page or email only if it is
> **VERIFIED** below. Change this file first when the product changes, then the marketing.

## 1. What Gemi Leads is

An independent SaaS by **NORVA Ι.Κ.Ε.** (not a GEMI service) that reads the official **GEMI Open Data API**
(opendata.businessportal.gr), stores newly published business registrations, and lets subscribers find the ones
that match their own criteria (ΚΑΔ, νομός, νομική μορφή). It does **not** score companies, predict purchases,
enrich data from third parties, or contact businesses.

Flow: `GEMI API → ingestion (legacy daily/3-hourly importer + unified 30′ ingestion) → local DB → user filters /
Radars → dashboard, Business Dossier, email digest, CSV`.

## 2. Plans, registration, billing (VERIFIED)

| Fact | Source of truth | Status |
|---|---|---|
| Plans: **Free €0**, Pro €19, Business €49, Enterprise €99 /μήνα, Custom by agreement | `templates/pricing.html`, `superadmin/services.PLAN_PRICES`, live /pricing/ | VERIFIED |
| Active Radars: Free 0 · Pro 5 · Business 10 · Enterprise 15 · Custom 15 | `RADAR_LIMITS` (`gemiapp/models.py`) | VERIFIED |
| Billing is **live** (real Stripe charges since 2026-08-26); beta badge off in production | `render.yaml` `LEGAL_BILLING_ACTIVE=1`, `BETA_MODE=0` | VERIFIED |
| **Free trial** | — no trial exists (only the Stripe `trialing` status is handled) | **NOT AVAILABLE** |
| Signup: first name, email, password ×2 → verification email → account activated on click → logged in → `/dashboard/` (or checkout resume if a plan was picked) | `views.signup`, `views.verify_email` | VERIFIED |
| Google sign-in (account active immediately, no verification email) | `adapters.SocialAccountAdapter`, live /signup/ shows Google | VERIFIED |
| Apple sign-in | code present, rendered only if `APPLE_*` configured | NOT VERIFIED (live) |

## 3. Feature classification

| # | Feature | Status | How it actually works | Plan |
|---|---|---|---|---|
| 1 | Discovery of newly registered businesses | **VERIFIED** | Every published GEMI incorporation is imported (daily 09:00 job, 3-hourly intraday job, plus the unified 30′ ingestion with a same-day lane). Shown in **Signals** (`/dashboard/`), newest first, infinite scroll by 20. | Free |
| 2 | Filtering by ΚΑΔ | **VERIFIED** (with caveat) | Picker searches the 9.651-code ΚΑΔ catalogue by number or Greek word. Up to **25 codes**, OR logic. **Exact-code match**: choosing `56.11.00.00` does **not** include sub-codes such as `56.11.01.01` — each code must be picked. | Free (dashboard), paid (Radar) |
| 3 | Filtering by geography | **VERIFIED at νομός level** / PARTIALLY for finer areas | Dashboard and Radars filter by **prefecture (νομός)**. Municipality exists only in the 2.0 Organization Radar, which is not customer-visible yet. City/ΤΚ: not a filter. | Free / paid |
| 4 | Filtering by legal form | **VERIFIED** | Ι.Κ.Ε., Α.Ε., Ο.Ε., Ε.Ε., Ατομική etc. as published. | Free / paid |
| 5 | Saved searches / Radar | **VERIFIED** | `CustomerRadar`: name, ΚΑΔ, νομοί, νομικές μορφές, optional name text, "only active", frequency; pause/resume, preview, per-Radar CSV. Each new registration is checked; a match stores *why* it matched. | **Paid only** (Free limit 0) |
| 6 | Lead status management | **VERIFIED** | Statuses: Νέο → Προβλήθηκε (automatic on opening the dossier) → Επικοινώνησα → Ενδιαφέρεται / Δεν ενδιαφέρεται → Αρχείο; favourites. Leads are created for paying users (Radar matches and dossiers they open). | **Paid** |
| 7 | Private notes | **VERIFIED** | One private notes field per lead (`lead_notes`). | **Paid** |
| 8 | CSV export | **VERIFIED** | Dashboard export (max 5.000 rows), per-Radar, Lead Inbox, and digest CSV link. Free users are redirected to /pricing/. | **Paid** |
| 9 | Email digests | **VERIFIED** | Daily 09:00 Athens. Free: general new registrations (up to 100), no Radar section, no CSV. Paid: Radar matches + general list + CSV. **Priority Alerts every 3 hours 08:00–23:00 only for Enterprise/Custom.** Unsubscribe link in every digest. | Free (daily) / paid |
| 9b | In-app notifications | PARTIALLY | Exist for 2.0 organizations (assignment, task due) — not visible to customers until the LIVE cutover. Do not market. | — |
| 10 | Business contact information | **PARTIALLY VERIFIED** | Dossier shows GEMI **phone** (`raw_data["phone"]`, ~89 % coverage measured on a dev copy, *not* on production), **email**, **website** and **διαχειριστές/εταίροι** — only when GEMI publishes them, and only for paid accounts. Nothing is enriched from other sources. | **Paid** |
| 11 | Business Dossier | **VERIFIED** | ΓΕΜΗ no., ΑΦΜ, incorporation date, legal form, seat, ΚΑΔ, match reasons, lead workspace. | Free (public fields) / paid (contacts, people, lead tools) |
| 12 | AI scoring / buying intent | **NOT AVAILABLE** (by design) | 2.0 has a deterministic score internally, not customer-visible. Never claim scoring. | — |
| 13 | Outreach / emailing businesses | **NOT AVAILABLE** (deliberately shut down 2026-09-09) | Never imply Gemi Leads contacts businesses. | — |

## 4. Copy issues found on the live site (proposed, not changed)

1. **Home FAQ "Τι περιλαμβάνει κάθε lead"** says only email and website — the phone has been shown since the hotfix. Under-claims; safe but stale.
2. **Home "από μία δραστηριότητα μέχρι ολόκληρο κλάδο"** over-claims slightly: ΚΑΔ matching is exact-code, max 25. Suggest: «από μία δραστηριότητα μέχρι 25 κωδικούς ΚΑΔ».
3. **Home final CTA "βλέπεις τι θα είχε πιάσει"** — the Radar preview exists, but a Free account cannot create a Radar.
4. **Home «Ασφαλιστές» row** says new companies «χρειάζονται κάλυψη» — an implied need that the insurance landing
   page deliberately avoids. Suggest: «Νέες εταιρείες με έδρα, εξοπλισμό και προσωπικό — πιθανοί νέοι πελάτες».
5. **Privacy page, providers table**: lists Render as database host; the production database is Supabase PostgreSQL. Legal text — needs the owner's review, not a marketing edit. GA described as «ανώνυμα στατιστικά»: GA4 uses pseudonymous identifiers; consider «ψευδωνυμοποιημένα».

## 5. Analytics infrastructure (as found)

- GA4 property **G-H9YRV774X0** (from `GA_MEASUREMENT_ID`), loaded by `templates/includes/analytics.html`
  **only after explicit consent** (own banner, choice in `localStorage['gemi_cookie_consent']`). Declining loads no
  third-party code. No Google Consent Mode signals, no GTM, no Meta/LinkedIn pixels.
- One custom event existed: `pricing_cta_click` (param `tier`) on checkout buttons.
- **Gap found and fixed in this work:** there was no way to withdraw consent from the UI (see report).

## 6. Technical stack

Django 5.2, PostgreSQL (Supabase) in production / SQLite in dev, django-q2 scheduler, Tailwind build + the
authoritative `static/css/product-ui.css`, vanilla JS (`static/js/app.js`), Stripe Checkout/Portal, Brevo SMTP,
django-allauth (Google/Apple), Render hosting, Sentry optional.

## 7. UI assets for marketing videos (all with fictional data only)

| Feature | Screen / element | Existing asset |
|---|---|---|
| Discovery | Signals register: time · amber match stroke · name · ΚΑΔ/region · state | `docs/gemi-leads-ui-study/screenshots/final/signals-desktop.png`, `signals-mobile.png`; `docs/marketing-screenshots/marketing_signals_*` |
| ΚΑΔ filter | Dashboard filter panel → ΚΑΔ picker chips (type «καφέ») | record live on a demo account |
| Radar | Radars register row: ● ΕΝΕΡΓΟ · criteria block · match count | `final/radars-desktop.png`, `marketing_radars_desktop_*` |
| Match reason / Dossier | Business Dossier with EVENT → CRITERIA → LEAD chain | `final/dossier-desktop.png`, `marketing_dossier_*` |
| Lead status + notes | Lead Inbox / dossier status select + notes | `final/leads-desktop.png`, `marketing_leads_desktop_*` |
| Plans | Pricing register | `final/pricing-desktop.png`, `pricing-mobile.png` |

Data for recordings: `python manage.py seed_demo_marketing_data` (fictional companies, ΓΕΜΗ `999000…`), remove with
`--clean`. **Never record real company or customer data.** Record against a local or staging copy, never production.
