# Analytics & Attribution — Tracking Plan

Property: **GA4 `G-H9YRV774X0`** (env `GA_MEASUREMENT_ID`). Code: `templates/includes/analytics.html`,
`gemiapp/campaigns.py`, `static/js/app.js` (data-analytics handler).

## 1. Principles (enforced in code)

1. **Consent first.** gtag.js loads only after «Αποδοχή». Without consent no script, no cookie, no event — conversion
   events for that page are dropped, never stored for later. Consent can now be withdrawn (footer «ΡΥΘΜΙΣΕΙΣ
   COOKIES», privacy page): the choice is cleared, `_ga*` cookies deleted, page reloaded.
2. **No PII.** Event names are whitelisted (`campaigns.ALLOWED_EVENTS`); parameters are booleans, small integers or
   `[A-Za-z0-9._-]{1,64}` tokens. No email, name, ΑΦΜ, ΓΕΜΗ number, company name or search text — ever.
3. **Attribution without storage.** UTMs are carried in the URL from the landing page to `/signup/` (sanitised by
   `clean_attribution`), never put in a cookie, session or database. Anonymous landing visits create no session.
4. **No duplicates.** One `gtag('config')` per page load (guarded by `__gemiAnalyticsLoaded`); no GTM; no
   `pushState`, so enhanced measurement adds no virtual page views. Server-side events are one-shot (popped from
   the session when rendered).

## 2. Campaign URL convention

```
https://gemileads.gr/for/horeca/?utm_source=<platform>&utm_medium=<paid_social|cpc|email|organic_social>&utm_campaign=gl_horeca&utm_content=gl004
https://gemileads.gr/for/insurance/?utm_source=<platform>&utm_medium=<…>&utm_campaign=gl_insurance&utm_content=gl006
```

| Field | Rule | Examples |
|---|---|---|
| `utm_source` | platform, lowercase | `facebook`, `instagram`, `linkedin`, `google`, `newsletter` |
| `utm_medium` | GA4 default-channel compatible | `paid_social`, `cpc`, `email`, `organic_social` |
| `utm_campaign` | audience campaign | `gl_horeca`, `gl_insurance` |
| `utm_content` | one creative/content piece | `gl004`, `gl005`, `gl006`, `gl007` (keep a register: id → creative, date, audience) |
| `utm_term` | optional keyword token, never free text | `kafe_attiki` |

Values outside `[A-Za-z0-9._-]{1,64}` are dropped by the site. Never put emails, names or phone numbers in a URL.

## 3. Funnel & events

| Funnel step | GA4 event | Type | Where fired | Parameters |
|---|---|---|---|---|
| Campaign landing visit | `page_view` (automatic) | GA4 automatic | `/for/horeca/`, `/for/insurance/` | UTMs from URL (automatic) |
| Landing CTA click | `landing_cta_click` | custom | CTA links (`data-analytics`) | `audience` (horeca/insurance), `cta_location` (hero/final/plans_pricing) |
| Registration start | `sign_up_start` | custom | first focus in the signup form | — |
| Registration completed (account created) | **`sign_up`** | GA4 recommended | page after successful email signup; first page after Google/Apple signup | `method` (email/google/apple), `utm_campaign`, `utm_content` (email path) |
| Account activated | `email_verified` | custom | first page after clicking the verification link | — |
| First meaningful search | `product_search` | custom | dashboard rendered with ≥1 filter | `has_kad`, `has_region`, `has_legal_form`, `has_date`, `has_name` (booleans only) |
| Radar created (activation for paid) | `radar_created` | custom | page after saving a **new** Radar | `kad_count` |
| Checkout intent | `pricing_cta_click` | custom (pre-existing) | plan buttons on /pricing/ | `tier` |
| Trial start | — | **N/A** | no trial exists | — |
| Paid subscription | `purchase` | GA4 recommended | **proposed, not implemented** (see §5) | `transaction_id`, `value`, `currency`, `items` |

"First" search is derived in GA4 (funnel exploration "first occurrence" / user-scoped segment), not tracked by the
site, so no per-user storage was added.

## 4. GA4 admin configuration (manual — not doable from code)

1. **Custom dimensions** (event scope): `audience`, `cta_location`, `method`, `has_kad`, `has_region`,
   `has_legal_form`, `has_date`, `has_name`, `tier`. Custom metric: `kad_count`.
2. **Key events (conversions):** `sign_up` (primary), `email_verified`, `radar_created`; keep `landing_cta_click`,
   `sign_up_start`, `product_search` as non-key funnel events.
3. **Funnel exploration:** page_view (page path starts `/for/`) → `landing_cta_click` → `sign_up_start` → `sign_up`
   → `email_verified` → `product_search` → `pricing_cta_click`, breakdown by session campaign / `utm_content`.
4. Data retention 14 months; Google signals off (no ads remarketing until a consent framework for it exists);
   internal traffic filter for office IPs.
5. Verify in **DebugView** (add `?debug_mode=1` is not wired — use the GA Debugger extension) before launch.

## 5. Gaps and proposals

| Gap | Impact | Proposal |
|---|---|---|
| `purchase` not tracked | revenue attribution by campaign missing | Server-side GA4 Measurement Protocol from the Stripe `checkout.session.completed` webhook, keyed to the GA `client_id` captured **only with consent** at checkout start (store on the Checkout Session metadata, not in our DB). Requires an API secret and a privacy-policy update. Until then: report paid conversions from Stripe/Superadmin by signup date and campaign. |
| Events on the page where consent is first given are lost | slight under-count of `sign_up` | accepted (privacy-first); measure the gap by comparing GA `sign_up` with new users in Superadmin. |
| Email verification on another device starts a new GA session | `email_verified` may lose campaign attribution | use the user-scoped first-user campaign dimension for this step. |
| No Google Consent Mode v2 | no modelled conversions in Google Ads | only needed if Google Ads is used; implement `gtag('consent','default',{…denied})` before loading, then `update`. Do not add ad pixels (Meta/LinkedIn) before a consent category for advertising exists in the banner and privacy policy. |
| Privacy text calls GA data «ανώνυμα» | accuracy | change to «ψευδωνυμοποιημένα» (owner/legal review). |

## 6. QA checklist (before launch)

- [ ] Open `/for/horeca/?utm_source=test&utm_medium=paid_social&utm_campaign=gl_horeca&utm_content=gl004` in a clean
      profile → no request to googletagmanager.com before «Αποδοχή».
- [ ] Accept → one `page_view` with campaign `gl_horeca`; click CTA → `landing_cta_click`; signup URL still has UTMs.
- [ ] Focus a field → one `sign_up_start`; submit → `sign_up` (method email, utm_campaign gl_horeca).
- [ ] Verify link → `email_verified` once; reload → not again.
- [ ] Filter dashboard by ΚΑΔ → `product_search` with `has_kad=true`, no values.
- [ ] Footer «ΡΥΘΜΙΣΕΙΣ COOKIES» → page reloads, `_ga` cookies gone, banner shown, no GA requests.
- [ ] Decline → no GA requests on any page.
