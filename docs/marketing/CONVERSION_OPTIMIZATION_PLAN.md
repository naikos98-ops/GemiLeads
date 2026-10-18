# Conversion Optimization Plan

Journey audited: **Ad → Landing page → Registration → Email verification → First search → Subscription.**
Status legend: ✅ implemented in this work · 🟡 proposed, low risk · 🔴 proposed, touches auth/billing — needs
explicit approval.

## 1. Journey as it is today

| Step | What happens | Friction found |
|---|---|---|
| Ad → page | Before this work, every ad had to land on the generic home page («Βρες τους επόμενους πελάτες σου…»). | Audience had to translate a generic B2B message into their own trade. |
| Landing → signup | CTA «ΞΕΚΙΝΑ ΤΩΡΑ» → `/signup/`. | CTA did not say it is free although a Free plan exists; UTMs were lost on the way. |
| Signup form | 4 fields (Όνομα, Email, Κωδικός ×2) + Google button. Subtitle «Καθημερινό digest και πλήρες ιστορικό…». | Password typed twice; subtitle generic; no reminder of what happens next. |
| Verification | Account inactive until the emailed link is clicked; page `verify_pending`; resend exists. | Classic drop-off point; not measured until now. Google sign-in skips it. |
| First experience | `/start/` → `/dashboard/` (Signals): the full unfiltered list. Filters are collapsed by default. | No prompt to pick a trade or area — the "aha" (seeing *my* kind of new customer) needs the user to find the filter panel and the ΚΑΔ picker alone. |
| Free → paid | Paywalls: contact fields in the dossier, CSV export, Radar creation → /pricing/. | Upgrade moments are good (intent-rich) but the redirect drops context: /pricing/ does not say *why* the user arrived. |

## 2. Prioritised recommendations

| # | Change | Impact | Effort | Status |
|---|---|---|---|---|
| 1 | Audience landing pages `/for/horeca/`, `/for/insurance/` with trade-specific proof, honest limits, free CTA | High | M | ✅ |
| 2 | CTA «ΞΕΚΙΝΑ ΔΩΡΕΑΝ» / «ΔΗΜΙΟΥΡΓΙΑ ΔΩΡΕΑΝ ΛΟΓΑΡΙΑΣΜΟΥ» on campaign pages (Free plan is real) | High | S | ✅ |
| 3 | UTMs carried to signup; funnel events `landing_cta_click → sign_up_start → sign_up → email_verified → product_search → radar_created` | High (makes everything else measurable) | M | ✅ |
| 4 | Consent withdrawal control (compliance prerequisite for any campaign) | Compliance | S | ✅ |
| 5 | Audience-aware signup subtitle when `utm_campaign` is `gl_horeca`/`gl_insurance` (e.g. «Δες τα νέα καταστήματα εστίασης της περιοχής σου από αύριο το πρωί») — read from the URL only, nothing stored | Medium | S | 🟡 |
| 6 | First-run card on an unfiltered, empty-history dashboard: «Διάλεξε κλάδο και νομό» with 3 one-click presets (Εστίαση & ποτά: 56.11/56.30/56.21; Καταλύματα: 55.10/55.20; Νέες Ι.Κ.Ε. & Α.Ε.) that just open the existing filters pre-filled | High | M | 🟡 |
| 7 | Paywall context: append `?reason=contacts|csv|radar` to the /pricing/ redirects and show one line («Τα στοιχεία επικοινωνίας είναι διαθέσιμα από το Pro») | Medium | S | 🟡 |
| 8 | Fix home-page copy issues listed in PRODUCT_AUDIT §4 (phone missing, «ολόκληρο κλάδο», preview for Free) | Medium (trust) | S | 🟡 |
| 9 | Link the home «Σε ποιους χρησιμεύει» rows (Ασφαλιστές, Προμηθευτές) to the new pages | Low–Medium | S | 🟡 |
| 10 | Make Google sign-in the visually first option on `/signup/` for campaign traffic (skips verification) | Medium | S | 🟡 |
| 11 | Single password field with show/hide (toggle already exists) instead of ×2 | Medium | S | 🔴 auth form |
| 12 | Verification reminder: if not verified after 24 h, one reminder email (reuse `send_verification_email_now`) | Medium | M | 🔴 email/auth flow |
| 13 | Social-signup starter Radar «Όλες οι νέες επιχειρήσεις» is created even for Free accounts (limit 0) — decide whether to keep it | Clarity | S | 🔴 product decision |
| 14 | Annual pricing / first-month offer for campaign cohorts | Unknown | M | 🔴 billing |

## 3. Measurement

Primary metric per campaign: **email_verified ÷ landing sessions** (activation rate), split by `utm_content`.
Secondary: `product_search` within 7 days of `email_verified`; `pricing_cta_click` and paid subscriptions (Stripe)
by signup cohort. Minimum ~100 landing sessions per creative before judging a creative; do not A/B test copy
until each arm can reach ~300 sessions.
