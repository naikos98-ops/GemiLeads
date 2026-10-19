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

## 3b. What can and cannot be measured today (traced through the code, not yet verified in GA4)

**Nothing below has been observed in the production GA4 property yet.** It is what the code does; confirm each
row in DebugView after deploy (§6).

| Path | What the browser sees | Campaign on the conversion? | Conversion event |
|---|---|---|---|
| Ad → `/for/<audience>/?utm_…` → «ΞΕΚΙΝΑ ΔΩΡΕΑΝ» | `/signup/?utm_…` (UTMs copied into the link, sanitised) | yes — the signup page URL carries the UTMs | `landing_cta_click`, then `sign_up_start` on first field focus |
| Email signup | form posts back to `/signup/?utm_…` (no `action`, so the query string survives); success page renders at that URL | yes — `sign_up` carries `utm_campaign` / `utm_content` as **event parameters** (register them as custom dimensions to report on them) | `sign_up` (method `email`) |
| Email verification | user clicks a link in the email → `/verify/…` → login → redirect to `/dashboard/` (or to Stripe if a plan was picked before signup) | **not from the URL** — the link carries no UTMs, and webmail usually arrives as a referral (e.g. `mail.google.com`), which starts a new session. Attribution relies on GA's user-scoped *first user campaign*, and only if it is the same browser with consent. Other device = new GA user, unattributed. | `email_verified` (once, on the first rendered page; if the user went straight to Stripe, it fires on return to the dashboard) |
| Google sign-in | `/signup/?utm_…` → `/accounts/google/login/` → accounts.google.com → callback → `/start/` → dashboard. UTMs are **dropped** at the Google button. | no campaign parameters on the event. Session attribution survives only if `accounts.google.com` is in GA4 **unwanted referrals** (otherwise the return starts a referral session). | `sign_up` (method `google`), on the first rendered page after the redirects |
| Existing user signs in with Google | same redirects | — | none (not a sign-up — correct) |
| First search | `/dashboard/?kad=…` | user-scoped first campaign only | `product_search` on **every** filtered page load; "first" must be derived in GA4 |
| Radar created | redirect to the Radar page | user-scoped only | `radar_created` (paid accounts only) |
| Checkout | `/pricing/` → Stripe Checkout (checkout.stripe.com) → `/dashboard/?session_id=…` | — | `pricing_cta_click` only. **`purchase` is not implemented**: GA4 cannot report paid subscriptions or revenue by campaign today. Add `checkout.stripe.com` to unwanted referrals. The Stripe Checkout Session id reaches GA as part of `page_location` (not personal data, but noise). |

**Consent timing:**
- Accepting on the landing page gives the full picture above.
- Accepting on the signup page still works: the URL carries the UTMs, and since this change, events rendered on the
  page where consent is given are sent at the moment of acceptance.
- Accepting only after verification means the campaign is lost for that user.
- Declining means nothing is measured, by design.

**Therefore:** GA4 can measure *campaign → landing → CTA → signup* per `utm_content` with reasonable confidence. It
can measure *signup → verification → first search* only at user level, and with gaps (cross-device, Google
sign-in without the unwanted-referral setting, late consent). It **cannot** measure *paid subscription by campaign*.
For revenue, join Stripe/Superadmin paid users by signup date against campaign launch dates until `purchase` exists.
There is no end-to-end, user-level attribution from ad to payment today.

## 4. GA4 admin configuration (manual — not doable from code)

1. **Custom dimensions** (event scope): `audience`, `cta_location`, `method`, `has_kad`, `has_region`,
   `has_legal_form`, `has_date`, `has_name`, `tier`. Custom metric: `kad_count`.
2. **Key events (conversions):** `sign_up` (primary), `email_verified`, `radar_created`; keep `landing_cta_click`,
   `sign_up_start`, `product_search` as non-key funnel events.
3. **Funnel exploration:** page_view (page path starts `/for/`) → `landing_cta_click` → `sign_up_start` → `sign_up`
   → `email_verified` → `product_search` → `pricing_cta_click`, breakdown by session campaign / `utm_content`.
4. **Unwanted referrals** (Admin → Data streams → Configure tag settings): `accounts.google.com`,
   `appleid.apple.com`, `checkout.stripe.com`, `billing.stripe.com`.
5. Data retention 14 months; Google signals off (no ads remarketing until a consent framework for it exists);
   internal traffic filter for office IPs.
6. Verify in **DebugView** with the Google Analytics Debugger browser extension before launch.

## 5. Gaps and proposals

| Gap | Impact | Proposal |
|---|---|---|
| `purchase` not tracked | revenue attribution by campaign missing | Server-side GA4 Measurement Protocol from the Stripe `checkout.session.completed` webhook, keyed to the GA `client_id` captured **only with consent** at checkout start (store on the Checkout Session metadata, not in our DB). Requires an API secret and a privacy-policy update. Until then: report paid conversions from Stripe/Superadmin by signup date and campaign. |
| Events on the page where consent is first given | — | **fixed**: sent at the moment of acceptance. Events on pages viewed *before* acceptance are still not sent (by design). Compare GA `sign_up` with new users in Superadmin to size the gap. |
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
- [ ] Google sign-in with a new Google account → `sign_up` method `google` on the dashboard; session source is not `accounts.google.com`.
- [ ] Accept cookies only on the signup success page → `sign_up` still arrives.
- [ ] Footer «ΡΥΘΜΙΣΕΙΣ COOKIES» → page reloads, `_ga` cookies gone, banner shown, no GA requests.
- [ ] Decline → no GA requests on any page.
