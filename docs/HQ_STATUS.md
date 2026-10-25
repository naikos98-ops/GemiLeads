# Gemi Leads — HQ Status

Σύντομη κατάσταση για το Claude Project «Gemi Leads | HQ». Η λεπτομερής τεχνική μνήμη είναι το
[`AGENTS.md`](../AGENTS.md)· αυτό το αρχείο δεν το αντικαθιστά, μόνο το συνοψίζει.

**Τελευταία επαλήθευση:** 2026-10-25 · `origin/main` = `aee5214`

Ετικέτες πηγής — κάθε γραμμή δηλώνει από πού προέρχεται:

- **[ΕΠΑΛΗΘ.]** ελέγχθηκε την ημερομηνία επαλήθευσης (git, δημόσιο HTTP του gemileads.gr, `render.yaml`).
- **[ΑΝΑΦΟΡΑ]** καταγράφηκε στο `AGENTS.md` από προηγούμενη εργασία ή από τον χρήστη· δεν ξαναελέγχθηκε.
- **[ΜΟΝΟ BRANCH]** υπάρχει μόνο σε branch/worktree, όχι στο `main`.
- **[ΑΓΝΩΣΤΟ]** δεν μπορεί να επιβεβαιωθεί από το repository (π.χ. env vars του Render, κατάσταση βάσης).

## 1. Τρέχουσα κατάσταση προϊόντος

- Django SaaS που εισάγει νέες επιχειρήσεις από το Open Data API του ΓΕΜΗ, με Signals, Radars, Leads,
  CSV και email digest. Free πλάνο (daily digest, 0 Radars) + Pro / Business / Enterprise / Custom.
- **Gemi Leads 2.0** (Organizations, Opportunities, Signals SHADOW, ενιαία ingestion, LIVE cutover
  υποδομή) βρίσκεται στο `main`· η LIVE λειτουργία είναι **αδρανής** πίσω από flags.
- Τελευταία ενεργή δουλειά: auth (αλλαγή/ορισμός κωδικού), Opportunity Detail, campaign landing pages,
  μέτρηση GA4.

## 2. Production status

| Στοιχείο | Κατάσταση | Πηγή |
|---|---|---|
| `gemileads.gr/login/` | HTTP 200, assets `v=20261021-password-security` (= release `aee5214`) | [ΕΠΑΛΗΘ.] |
| Sitemap | `<loc>` σε `https://gemileads.gr/` (όχι `example.com`) | [ΕΠΑΛΗΘ.] |
| Billing — configuration | `LEGAL_BILLING_ACTIVE=1`, `BETA_MODE=0` στο `render.yaml` (όχι ανάγνωση του Render env) | [ΕΠΑΛΗΘ.] (repo) |
| Billing — δημόσια σελίδα | η `/pricing/` αποδίδει φόρμα checkout | [ΕΠΑΛΗΘ.] (HTTP) |
| Billing — πραγματική λειτουργία | επιτυχής πληρωμή, webhook και ενεργοποίηση συνδρομής **δεν** επαληθεύτηκαν | [ΑΓΝΩΣΤΟ] |
| Cold outreach — configuration | `OUTREACH_ENABLED=0`, cap `0` στο `render.yaml` | [ΕΠΑΛΗΘ.] (repo) |
| Cold outreach — πραγματική λειτουργία | ότι δεν στέλνεται κανένα email δεν ελέγχθηκε στο Brevo/Render | [ΑΓΝΩΣΤΟ] |
| Render deploy `aee5214` | success | [ΑΝΑΦΟΡΑ] 2026-10-21 |
| Τελευταία migration στο `main` | `0059_live_signal_period` | [ΕΠΑΛΗΘ.] (repo) |
| Εφαρμοσμένη migration στη βάση production | εικάζεται `0059` μέσω `preDeployCommand`, δεν ελέγχθηκε | [ΑΓΝΩΣΤΟ] |
| Ενιαία ingestion + same-day lane | ενεργές, parity OK 2026-10-09 → 10-13 | [ΑΝΑΦΟΡΑ] |
| `GEMI_LIVE_SIGNALS_ENABLED` / `GEMI_LIVE_OPPORTUNITIES_VISIBLE` | δεν ορίζονται στο `render.yaml` → default `0` | [ΑΓΝΩΣΤΟ] (Render env) |
| GA4 `G-H9YRV774X0` | στέλνει δεδομένα· όχι έτοιμο για paid campaigns | [ΑΝΑΦΟΡΑ] 2026-10-20 |

## 3. Πρόσφατα releases (στο `main`)

| Ημερομηνία | Commit / PR | Τι |
|---|---|---|
| 2026-10-21 | `aee5214` | Αλλαγή/ορισμός κωδικού από τα Settings |
| 2026-10-20 | `38eb99a` | Opportunity Detail: στοιχεία επικοινωνίας, ενιαία «Διαχείριση» |
| 2026-10-19 | PR #2 (`460cab9`) | Sites record από `BASE_URL` (sitemap, password-reset links) |
| 2026-10-19 | PR #1 (`0d35073`) | Campaign landing pages HoReCa / ασφαλιστές, consent-safe attribution |
| 2026-10-14 | `c669476`, `b9dcbaa` | Αδρανής LIVE cutover υποδομή (migration `0059`) |
| 2026-10-09 | `f9a331f` | Same-day discovery lane |

## 4. Ενεργά development tasks

- **Σύνδεση HQ ↔ Claude Code:** αυτό το αρχείο και ο κανόνας στο `AGENTS.md` (branch `docs/hq-status`).
- **SEO public pages** — worktree `gemi-leads-seo-audit-31a410`: **17 uncommitted αρχεία**, χωρίς commit
  ή push από 2026-09-21. [ΜΟΝΟ BRANCH, τοπικά] Η δουλειά δεν υπάρχει πουθενά αλλού.
- **LIVE cutover:** περιμένει απόφαση (βλ. §8).
- **GA4 πριν από paid campaigns:** Consent Mode v2, επαλήθευση funnel events, `purchase` από το Stripe
  webhook. Δεν έχει ξεκινήσει υλοποίηση.

## 5. Ανοιχτά PRs και σημαντικά branches

- **Ανοιχτά PRs:** κανένα [ΕΠΑΛΗΘ.], εκτός από το PR αυτής της οργάνωσης.
- **Remote branches:** όλα (`feature/live-cutover-dormant`, `feature/gemi-ingestion-ops-safety`,
  `claude/unified-ingestion-parity-miss-58e98f`, `claude/email-deliverability-suppression-77bff7`,
  `claude/fix-sitemap-site-domain`, `claude/gemi-leads-audit-campaign-016e37` κ.ά.) έχουν ήδη γίνει merge
  στο `main` [ΕΠΑΛΗΘ.]. Μπορούν να καθαριστούν μετά από έγκριση.
- **Τοπικά worktrees:** όλα χωρίς μοναδικά commits εκτός `main` (`git cherry`) [ΕΠΑΛΗΘ.]· η μόνη
  εκκρεμής δουλειά είναι τα uncommitted αρχεία του SEO worktree (§4).
- **Main checkout** (`D:\Projects\Gemi-Signal`): uncommitted σημείωση release στο `AGENTS.md` και 3
  untracked PNG στη ρίζα. Σκόπιμα ανέγγιχτα, εκκρεμεί απόφαση.

## 6. Τεχνικές εκκρεμότητες

- **Ασυνέπεια τεκμηρίωσης:** το `README.md` και το `AI_SUMMARY.md` γράφουν «beta, billing κλειστό», ενώ το
  `render.yaml` το ενεργοποιεί και η `/pricing/` έχει φόρμα checkout (πραγματική πληρωμή μη επαληθευμένη). Επίσης στο `AGENTS.md` αρκετές ενότητες λένε
  ακόμη «ΟΧΙ στο `main`» για δουλειά που πλέον είναι στο `main`.
- Legacy Radar → OrganizationRadar: έγινε μόνο dry-run σε production, όχι η πραγματική μεταφορά [ΑΝΑΦΟΡΑ].
- Email deliverability: δεν έχει αναφερθεί εκτέλεση του production backfill (`--apply`) [ΑΓΝΩΣΤΟ].
- Late publications / pending hydration: τα μη ασφαλή pending μένουν σκόπιμα χωρίς σήμα.
- Heartbeat για νεκρό worker: δεν υπάρχει· τα alerts εξαρτώνται από το ότι τρέχει το task.
- Search Console: δεν έχει αναφερθεί υποβολή sitemap μετά το fix της 2026-10-19.

## 7. Κίνδυνοι

- **Χαμένη δουλειά:** τα uncommitted αρχεία του SEO worktree χάνονται αν διαγραφεί ή καθαριστεί το worktree.
- **Λάθος δημόσιες δηλώσεις:** η παλιά τεκμηρίωση για το billing μπορεί να περάσει σε marketing ή HQ
  αποφάσεις. Πηγή αλήθειας για ισχυρισμούς: `docs/marketing/PRODUCT_AUDIT.md`.
- **LIVE cutover:** αν ενεργοποιηθεί πριν βγει `READY` από το `report_live_cutover_readiness`,
  εμφανίζονται στους πελάτες μη πιστοποιημένες ευκαιρίες.
- **Paid campaigns χωρίς consent mode και attribution εσόδων:** σπατάλη budget χωρίς δυνατότητα μέτρησης.
- **Ιστορικό django-q** (`save_limit: 50`): οι συχνοί κύκλοι ingestion σπρώχνουν έξω το ιστορικό άλλων tasks.

## 8. Αποφάσεις που χρειάζονται την έγκρισή σου

1. **LIVE cutover:** τρέξιμο του `report_live_cutover_readiness` σε production και, αν βγει `READY`, το βήμα
   «dark LIVE» (`GEMI_LIVE_SIGNALS_ENABLED=1`, visibility `0`). Το 14ήμερο G4 τελείωνε ~2026-10-22 [ΑΝΑΦΟΡΑ].
2. **Main checkout:** commit ή απόρριψη της σημείωσης release στο `AGENTS.md`· τι θα γίνουν τα 3 PNG.
3. **SEO worktree:** commit/PR ή απόρριψη των 17 uncommitted αρχείων.
4. **Διόρθωση της τεκμηρίωσης billing** στα `README.md` / `AI_SUMMARY.md` (ξεχωριστό μικρό PR).
5. **Privacy wording** (`docs/marketing/PRIVACY_WORDING_PROPOSAL.md`): έγκριση πριν εφαρμοστεί.
6. **GA4/paid campaigns:** σειρά υλοποίησης (Consent Mode → funnel events → Stripe `purchase`).
7. **Legacy Radar migration:** έγκριση για την πραγματική εκτέλεση (`--apply`) μετά το provisioning dry-run.
8. **Free owners:** αν θα βλέπουν τις σελίδες του οργανισμού τους (κανόνας πρόσβασης).
9. **Καθαρισμός** των ήδη merged remote branches και worktrees.

## 9. Αναλυτική τεκμηρίωση

- Τεχνική μνήμη, κανόνες, ιστορικό: [`AGENTS.md`](../AGENTS.md)
- Πύλες release G0–G6: [`docs/RELEASE_READINESS.md`](RELEASE_READINESS.md)
- Αρχιτεκτονική 2.0: [`docs/GEMI_LEADS_2_BLUEPRINT.md`](GEMI_LEADS_2_BLUEPRINT.md)
- ΓΕΜΗ API: [`docs/GEMI_API_CAPABILITY_REPORT.md`](GEMI_API_CAPABILITY_REPORT.md)
- Compliance outreach: [`docs/compliance/cold-outreach-shutdown.md`](compliance/cold-outreach-shutdown.md)
- Marketing (πηγή αλήθειας ισχυρισμών): [`docs/marketing/PRODUCT_AUDIT.md`](marketing/PRODUCT_AUDIT.md),
  [`BRAND_GUIDELINES.md`](marketing/BRAND_GUIDELINES.md), [`LANDING_PAGES_SPEC.md`](marketing/LANDING_PAGES_SPEC.md),
  [`ANALYTICS_TRACKING_PLAN.md`](marketing/ANALYTICS_TRACKING_PLAN.md),
  [`CONVERSION_OPTIMIZATION_PLAN.md`](marketing/CONVERSION_OPTIMIZATION_PLAN.md)
- Design system: [`static/css/product-ui.css`](../static/css/product-ui.css),
  [`docs/gemi-leads-ui-study/`](gemi-leads-ui-study/)

## Πώς ενημερώνεται

Το Claude Code ενημερώνει αυτό το αρχείο στο ίδιο commit με την αλλαγή, όταν μια εργασία ολοκληρώνεται ή
αλλάζει ουσιαστικά (κανόνας 7 στο `AGENTS.md`). Το HQ διαβάζει από το GitHub και δεν γράφει απευθείας εδώ·
οι αποφάσεις του φτάνουν ως οδηγίες προς το Claude Code.
