# Privacy policy — proposed wording (REQUIRES OWNER / LEGAL APPROVAL)

**Status: proposal only. `templates/legal/privacy.html` has NOT been changed.** Apply only after NORVA Ι.Κ.Ε.
(the controller) or its legal advisor approves the text and the owner has checked every item in §B.

Last fact-check: 2026-10-19.

## A. Facts confirmed, with evidence

| # | Fact | Evidence | Strength |
|---|---|---|---|
| A1 | The web application is served by **Render**, behind **Cloudflare** | Live response headers of https://gemileads.gr/: `Server: cloudflare`, `rndr-id`, `x-render-origin-server: gunicorn` | Observed live |
| A2 | Render's managed PostgreSQL was **removed** from the deployment blueprint on 2026-08-17; `DATABASE_URL` became a manually set secret | Commit `34ea38b` («Optimize Render architecture with honcho and Supabase») deletes the `databases:` block and `fromDatabase` reference in `render.yaml` | Repository |
| A3 | The production database is **Supabase PostgreSQL** | `AGENTS.md` history (2026-08-23): a read-only connection to the production Supabase database. The `render.yaml` comment names Supabase. | Repository record — **not re-verified today** |
| A4 | `render.yaml` **declares** `region: frankfurt` for the web service | `render.yaml` line 10 | Config only — its own comment says an existing service cannot change region and must be re-created, so the declaration does not prove the live service's region |
| A5 | GA4 property `G-H9YRV774X0` is live and loads **only after consent** | Live HTML of the home page; `templates/includes/analytics.html` | Observed live + code |
| A6 | GA4 sets first-party cookies (`_ga`, `_ga_<id>`) holding a random client identifier | Standard gtag.js behaviour; the site's own withdrawal code deletes `_ga*` cookies | Code + vendor docs |
| A7 | The site sends GA4 **no** name, email, ΑΦΜ, ΓΕΜΗ number, company name or search text; event parameters are whitelisted tokens, booleans and small integers | `gemiapp/campaigns.py` (`ALLOWED_EVENTS`, `_clean_params`), tests in `gemiapp/test_campaigns.py` | Code + tests |
| A8 | GA4 receives the visited page URL, including `utm_*` campaign parameters and, after checkout, the Stripe Checkout Session id in `?session_id=` | `gtag('config')` default page_view; Stripe `success_url` in `gemiapp/billing.py` | Code |
| A9 | The code does **not** set `allow_google_signals` or `allow_ad_personalization_signals`; those follow the GA4 property settings | `templates/includes/analytics.html` | Code |
| A10 | Consent can be withdrawn from the footer and the privacy page | PR #1 | Code + local browser test |

## B. Details that require your verification (not guessed)

| # | Question | Where to check | Unknown because |
|---|---|---|---|
| B1 | Region of the **live** Render web service and worker | Render dashboard → service → Settings | No Render access; `render.yaml` is intent, not proof (A4) |
| B2 | Region of the **production** Supabase project | Supabase dashboard → Project settings → General | The Supabase account connected to this session does not contain the Gemi Leads project |
| B3 | That `DATABASE_URL` in Render points to that Supabase project | Render → Environment | Secret, not readable here |
| B4 | DPA in place with Render and Supabase (and Brevo, Stripe, Sentry, Cloudflare, Google) | Each provider's account / legal pages | Contractual, not in the repository |
| B5 | GA4 admin: Google signals, ads personalisation, data retention period, data-sharing settings | GA4 → Admin → Data collection / Data retention / Account settings | No GA4 access |
| B6 | Which Google entity is the contracting party for this GA4 account | GA4 / Google Ads data processing terms | Account-level |
| B7 | Whether Sentry is enabled in production (`SENTRY_DSN`) | Render → Environment | The policy lists Sentry; `AGENTS.md` says it is not used in this deployment |

## C. Proposed text

### C1. Section 5, providers table — database host (depends on A2, A3 and on B1–B4)

Current row (inaccurate): `Render | Φιλοξενία εφαρμογής και βάσης | Όλα τα δεδομένα λογαριασμού`

Proposed rows:

| Πάροχος | Ρόλος | Τι λαμβάνει |
|---|---|---|
| Render | Φιλοξενία της εφαρμογής και των εργασιών παρασκηνίου | Όλα τα δεδομένα λογαριασμού, κατά την επεξεργασία τους |
| Supabase | Φιλοξενία της βάσης δεδομένων (PostgreSQL) | Όλα τα δεδομένα λογαριασμού |

Add the following sentence **only after B1 and B2 are confirmed**, with the regions you actually find:
> «Η εφαρμογή και η βάση δεδομένων φιλοξενούνται σε κέντρα δεδομένων εντός της ΕΕ ([πόλη/περιοχή]).»

Keep the existing paragraph on transfers outside the EEA (Standard Contractual Clauses).

### C2. Google Analytics — replace «Ανώνυμα στατιστικά» (depends on A5–A9 and B5–B6)

*Why the current wording is wrong:* a cookie identifier that links a visitor's hits is pseudonymous data. Under
GDPR that is personal data, not anonymous data (A6).

Proposed providers-table row:

| Google Analytics | Στατιστικά επισκεψιμότητας | Ψευδωνυμοποιημένο αναγνωριστικό συσκευής (cookie), σελίδες που επισκέφθηκες μαζί με τις παραμέτρους της διεύθυνσης (π.χ. καμπάνια), πηγή επίσκεψης και ενέργειες όπως η εγγραφή — μόνο με τη συγκατάθεσή σου |

Proposed section 4 «Στατιστικών» bullet:
> «**Στατιστικών.** Cookies του Google Analytics που μετρούν την επισκεψιμότητα μέσω ενός ψευδωνυμοποιημένου
> αναγνωριστικού. Δεν στέλνουμε στο Google όνομα, email, ΑΦΜ ή ό,τι πληκτρολογείς στην αναζήτηση.
> **Δεν φορτώνονται καθόλου** αν δεν πατήσεις «Αποδοχή». Αν επιλέξεις «Απόρριψη», δεν φορτώνεται κανένας κώδικας
> τρίτου μέρους. Μπορείς να ανακαλέσεις τη συγκατάθεσή σου οποιαδήποτε στιγμή από τις «Ρυθμίσεις cookies».»

If B5 shows Google signals or ads personalisation **on**, the text above is incomplete: either switch them off or
describe them.

### C3. Optional, for the owner's attention

- Google sign-in (and Apple, if enabled) is not listed as a recipient, although it receives the login request.
- If Sentry is not used (B7), remove it from the table; if it is, keep it.
