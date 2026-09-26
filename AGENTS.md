# Gemi Leads — μόνιμο project handoff

Το αρχείο αυτό είναι η κοινή μνήμη του project για όλα τα Codex/ChatGPT accounts και όλους τους υπολογιστές που επεξεργάζονται το repository.

## Υποχρεωτική διαδικασία για κάθε AI

1. Διάβασε ολόκληρο αυτό το αρχείο πριν αλλάξεις κώδικα.
2. Έλεγξε `git status` και διατήρησε τις αλλαγές που υπάρχουν ήδη.
3. Μην αποθηκεύεις ποτέ API keys, SMTP keys ή άλλα secrets στο Git. Τα secrets ανήκουν μόνο στο `.env`, το οποίο αγνοείται από το Git.
4. Μετά από κάθε ολοκληρωμένη εντολή του χρήστη, ενημέρωσε τουλάχιστον τις ενότητες «Τρέχουσα κατάσταση», «Τι απομένει» και «Ιστορικό εργασιών».
5. Μην χαρακτηρίζεις μια λειτουργία ολοκληρωμένη χωρίς ανάλογο έλεγχο (`manage.py check`, tests και λειτουργική επαλήθευση όπου χρειάζεται).
6. Η ενότητα «Τι απομένει» είναι ενεργή λίστα εργασιών και περιέχει μόνο μη ολοκληρωμένα στοιχεία. Μόλις ολοκληρώνεται και επαληθεύεται κάτι, αφαίρεσέ το από εκεί, ενημέρωσε την «Τρέχουσα κατάσταση» και πρόσθεσέ το στο «Ιστορικό εργασιών».

## Τι είναι η εφαρμογή

Το Gemi Leads είναι Django SaaS που εισάγει καθημερινά τις νέες επιχειρήσεις από το επίσημο Open Data API του ΓΕΜΗ. Οι εγγεγραμμένοι χρήστες βλέπουν ιστορικό και φίλτρα στο dashboard, εξάγουν CSV και λαμβάνουν προσωποποιημένο email digest.

Βασική ροή:

`GEMI API → ημερήσιο import → τοπική βάση → φίλτρα χρήστη → dashboard / email digest`

## Τεχνική δομή

- Framework: Django 5.2
- Development DB: SQLite (`db.sqlite3`, δεν ανεβαίνει στο Git)
- Settings: `config/settings.py`
- Models: `gemiapp/models.py`
- GEMI import και digest: `gemiapp/services.py`
- Views/filters/API: `gemiapp/views.py`
- UI: `templates/` και `static/js/app.js`
- Daily command: `python manage.py run_daily_pipeline`
- Local environment: `.venv/`
- Local secrets: `.env`

## Τοπική εκκίνηση

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test --noinput --parallel 4
.\run_dev.ps1
```

**Κανονική εντολή πλήρους regression suite:** `manage.py test --noinput --parallel 4` (~40 s για 1.726 tests,
16 λογικοί πυρήνες). Όλο το suite, κανένα skip/tag/exclude/failfast. Το `TEST_RUNNER` είναι
`config.fast_test_runner.FastHasherTestRunner`: ίδιο discovery και ίδια tests με τον `DiscoverRunner`, αλλά με τον
test-only `MD5PasswordHasher` (στη main process και σε κάθε `--parallel` worker). Το production κρατά τον default
PBKDF2 του Django (το `settings.py` **δεν** ορίζει `PASSWORD_HASHERS`)· tests που ελέγχουν τη μορφή του production
hash κάνουν `override_settings(PASSWORD_HASHERS=[PBKDF2...])` (π.χ. `BackupCriticalTests`). Το `--keepdb` **δεν**
χρησιμοποιείται: η test βάση είναι in-memory SQLite (~4 s δημιουργία), άρα δεν κερδίζει τίποτα, και με
`--parallel` αφήνει αρχεία `default_N.sqlite3` στη ρίζα.

**Πολιτική επαλήθευσης ανά πακέτο (από το D35):** (1) focused tests του νέου πακέτου, (2) affected/regression
suites, (3) `manage.py check`, (4) `manage.py makemigrations --check`, (5) **μία** κανονική πλήρης εκτέλεση. Δεύτερη
συνεχόμενη πλήρης εκτέλεση μόνο σε flaky/intermittent αποτυχία, ζήτημα concurrency/locking που χρειάζεται
επανάληψη, μεγάλη αλλαγή tenant/security υποδομής, ρητό αίτημα ή τελική release/staging επαλήθευση. Ο κύκλος
migration (forward/reverse/reapply στο αντίγραφο της dev βάσης) παραμένει υποχρεωτικός για πακέτα με migration.

Demo login (μόνο development): `demo@gemileads.gr` / `demo12345`.

## Το ΕΓΚΥΡΟ design system (διάβασέ το πριν αγγίξεις UI)

> **`static/css/product-ui.css` μαζί με τις αποδοθείσες οθόνες του Signal Ledger είναι το
> αυθεντικό design system του προϊόντος.** Κάθε νέα ή τροποποιημένη επιφάνεια — authenticated,
> public ή admin — πρέπει να χτίζεται από τα primitives αυτού του αρχείου.

Τα τρία scoped layers του `product-ui.css`:

| Scope | Επιφάνειες | Primitives |
|---|---|---|
| `body.product-body` | Signals, Radars, Leads, Dossier, Settings | `.product-rail`, `.product-signal`, `.product-radar-row`, `.product-ink-button`, … |
| `body.public-body` | Landing, Pricing, auth, legal | `.public-topbar`, `.public-plan-row`, `.auth-field`, `.prose-page`, … |
| `body.admin-body` | Superadmin | `.admin-rail`, `.admin-register`, `.admin-panel`, `.admin-tag`, `.admin-btn`, … |

Tokens (μία πηγή): `--product-ink #12201e`, `--product-paper #f3f2ec`, `--product-white #fbfbf7`,
`--product-amber #e6a029`, `--product-green #295c4c`, `--product-line`, `--product-muted`.

### ΜΗ ΕΓΚΥΡΑ (legacy) primitives

Τα παρακάτω ανήκουν στο **παλιό, μη εγκεκριμένο** SaaS σύστημα και **δεν** αποτελούν πηγή
αλήθειας για UI του προϊόντος:

- `rounded-card`, `rounded-chip`, `shadow-glow` — **αφαιρέθηκαν** από το `tailwind.config.js`.
  Αν τα γράψεις, δεν παράγονται καν από το Tailwind.
- `shadow-soft`, `rounded-control`, `rounded-panel` — παραμένουν **μόνο** για το
  `includes/kad_picker.html` και το `static/js/app.js`. Μην τα χρησιμοποιείς αλλού.
- `bg-signal` / `text-signal` / μπλε CTA, generic stat cards, floating auth cards,
  rounded-full κουμπιά: **μην τα εισάγεις ξανά.**
- Το `static/src/input.css` και οι generic Tailwind utilities **δεν** είναι το design system.
  Προηγούμενοι agents το συμπέραναν από εκεί και επανέφεραν το παλιό look.

Η μόνη επιφάνεια που κρατά legacy classes σκόπιμα είναι το `includes/kad_picker.html`, επειδή
αποδίδεται μέσα στις **εγκεκριμένες** οθόνες Signals και Radar-form.

### Authority (English, for any agent)

> **AUTHORITY: `static/css/product-ui.css` + the rendered Signal Ledger authenticated screens.**
>
> **Legacy/global Tailwind card primitives are NOT authoritative for Gemi Leads product UI.**
> Do not derive the design system from `static/src/input.css`, `tailwind.config.js` or generic
> Tailwind utilities.

### Component mapping — ποια οθόνη γεννά ποια

Κάθε public/admin επιφάνεια είναι παράγωγο μιας εγκεκριμένης οθόνης. Όταν αλλάζεις μία, ξεκίνα
από την πηγή της στήλης αριστερά — όχι από κάποιο generic component.

| Εγκεκριμένη πηγή | Primitive πηγής | → Παράγωγο | Primitive παραγώγου |
|---|---|---|---|
| **Signals** (χρονολογική γραμμή) | `.product-signal`, `.product-match-stroke` | **Landing** public intake | `.product-signal` αυτούσιο μέσα σε `.public-intake` |
| **Signals** (inspector / αλυσίδα) | `.product-inspect` | **Landing** relevance proof | `.public-chain` (EVENT → INFORMATION → CRITERIA → RELEVANCE → LEAD) |
| **Radars** (γραμμή ορισμού) | `.product-radar-row`, `.product-criteria` | **Pricing** plan row | `.public-plan-row` — ΠΛΑΝΟ / ΣΤΟΧΕΥΣΗ / ΡΑΝΤΑΡ / ΣΥΧΝΟΤΗΤΑ / ΤΙΜΗ / ΕΝΕΡΓΕΙΑ |
| **Leads** (φίλτρα + register) | `.product-ledger-tools`, register rows | **Superadmin** registers | `.admin-filters`, `.admin-register`, `.admin-tag` |
| **Business Dossier** (ενότητες) | `.product-record-section` | **Superadmin** detail / compliance | `.admin-panel`, `.admin-compliance` |
| **Settings** (notice) | notice με amber αριστερό rule | **Pricing / auth** notices | `.public-notice`, `.auth-notice` |
| **Product rail** | `.product-rail` (184px) | **Superadmin rail** | `.admin-rail` (218px, +1 επίπεδο ομαδοποίησης) |

Η κοινή υπογραφή «επιλεγμένο/ενεργό» είναι **ίδια δήλωση** παντού:
`box-shadow: inset 3px 0 var(--product-amber)` — στο `.product-rail`, `.admin-rail`,
`.product-signal.selected` και `.public-plan-row.featured`. Αν μια νέα επιφάνεια χρειάζεται
"selected" state, αυτή είναι η δήλωση· όχι background tint, όχι pill, όχι σκιά.

Χρώμα κειμένου amber: `#775215` (όχι `--product-amber`, που είναι για rules/strokes και δίνει
2.15:1 ως κείμενο). Κόκκινο `--product-red` = αποκλειστικά disabled/compliance-off.

Η πλήρης χαρτογράφηση με τις πραγματικές τιμές (grids, borders, type scale, responsive) είναι στο
`docs/gemi-leads-ui-study/forensic-audit-and-primitive-mapping.md`.

### ⚠ Το λάθος που προκάλεσε το visual drift — μην το επαναλάβεις

Το εγκεκριμένο design system **δεν υπήρχε στο `main`**. Το `static/css/product-ui.css` είχε
**0 γραμμές** στο `main` και ζούσε μόνο στο branch `fix/outreach-hard-bounces`, που δεν είχε γίνει
ποτέ merge. Ένας agent που δούλευε στο `main` δεν μπορούσε να το βρει, οπότε αναπαρήγαγε τα
πραγματικά primitives του `main` (`rounded-card`, `shadow-soft`, stat cards).

**Πριν αγγίξεις UI, επιβεβαίωσε ότι το design system υπάρχει στο branch σου:**

```bash
test -s static/css/product-ui.css && grep -q "body.product-body" static/css/product-ui.css && echo OK
```

Αν δεν τυπώσει `OK`, **σταμάτα** — δουλεύεις σε branch χωρίς το εγκεκριμένο σύστημα.

### Canonical screenshots

`docs/gemi-leads-ui-study/screenshots/final/` — το μικρό, τρέχον σετ αναφοράς (14 αρχεία).
Τα `direction-*`, `production-*`, `prototype-*`, `refined-*` στον γονικό φάκελο είναι το ιστορικό
της design μελέτης που οδήγησε στο Signal Ledger. Τα παλαιότερα `marketing_*.png` και `review_*.png`
δείχνουν ενδιάμεσες καταστάσεις πριν το consolidation και **αφαιρέθηκαν** — μην τα αναζητήσεις.

### Γνωστές εξαιρέσεις

- `gemiapp/forms.py` → το κοινό `INPUT` constant εκπέμπει `rounded-2xl` (16px). Εμφανίζεται στο
  status `<select>` και στο notes `<textarea>` των εγκεκριμένων Settings/Dossier. Είναι μέρος της
  εγκεκριμένης κατάστασης· στις auth/admin σελίδες υπερισχύουν scoped κανόνες.
- `templates/allauth/layouts/entrance.html` → override μόνο εμφάνισης που βάζει τις anonymous σελίδες
  του django-allauth μέσα στο public chrome. Το `manage.html` (signed-in σελίδες allauth) μένει
  στο default του allauth σκόπιμα.

## Τρέχουσα κατάσταση

- **Free: ημερήσιο email digest (2026-09-26).** Το DAILY digest είναι πλέον μέρος του Free plan.
  - **Κανόνας (`services.digest_skip_reason`):** PREFERENCE (υπάρχει `DigestPreference`, όχι `off`) → ACCOUNT
    (`is_active`, υπάρχει email) → για **κάθε συχνότητα εκτός DAILY** ENTITLEMENT (`has_entitlement`) → για intraday
    και TIER (Enterprise/Custom). Η εξαίρεση είναι μία σταθερά, `FREE_DIGEST_FREQUENCY = "daily"`. Άρα ένας λογαριασμός
    χωρίς entitlement — νέος, ή με συνδρομή cancelled / past_due / unpaid / inactive / λήξη complimentary — παίρνει
    το **daily** και **ποτέ** το intraday.
  - **Περιεχόμενο για Free (`send_digests`, `send_user_yesterday_digest`):** `radar_features = has_entitlement`.
    Χωρίς αυτό: **καμία** ανάγνωση `RadarMatch` (ούτε παλιό match από πριν λήξει μια συνδρομή), **καμία** ενότητα
    ή διατύπωση Ραντάρ στο HTML/text, **κανένα** CSV token — ο σύνδεσμος γίνεται «CSV & Ραντάρ με συνδρομή» προς
    το `/pricing/`· το «include_empty_digest» δεν στέλνει κενό digest σε Free (δεν έχει Ραντάρ). Τα γενικά νέα
    εγγραφέντα της ημέρας (έως 100) μπαίνουν κανονικά. Πληρωμένα πλάνα: **ακριβώς όπως πριν**.
  - **Αμετάβλητα:** schedules (ο ίδιος daily task 09:00 και ο ίδιος intraday, χωρίς αλλαγή επιλογής — το query
    παραληπτών έπαιρνε ήδη όλους τους ενεργούς με preference ≠ off· ο μόνος φραγμός ήταν ο κανόνας), idempotency
    (`DigestDelivery` sent/skipped ανά ημέρα), unsubscribe, verification, `RADAR_LIMITS["free"] == 0`, το POST του
    Radar form που αρνείται με όριο 0, `export_csv`/`digest_export_csv` που στέλνουν Free στο pricing, Stripe,
    billing, συνδρομές, Organization entitlement, G4/Discovery/hydration, LIVE/SHADOW. Καμία migration.
  - Ιστορικό του κανόνα: 2026-09-06 daily χωρίς entitlement → `37e639c` (2026-09-11) entitlement για όλες τις
    συχνότητες → **2026-09-26 daily ξανά χωρίς entitlement, αλλά χωρίς Ραντάρ/CSV για Free**.
  - Tests: `FreeDailyDigestTests` (νέα), `DigestEntitlementTests` και το Superadmin manual send ξαναγράφτηκαν για τον
    νέο κανόνα (daily επιτρέπεται, intraday αρνείται). Έλεγχος mutation: daily με entitlement, intraday για Free,
    ανάγνωση Radar για Free, πάντα ορατή ενότητα Ραντάρ, CSV token για Free → όλα πιάνονται.

- **Pricing: γραμμή Free (2026-09-26). Μόνο περιεχόμενο/UI της σελίδας τιμών.**
  - Πρώτη γραμμή «Free · €0 / μήνα» στο plan register (`templates/pricing.html`), στην ίδια γραμματική με τα
    πληρωμένα πλάνα. **Περιλαμβάνει** (πράσινο ✓): προβολή νέων εγγραφών, βασικά στοιχεία επιχείρησης, ημερήσιο
    email digest (συχνότητα «Ημερήσια · digest 09:00»). **Δεν περιλαμβάνει** (muted, «από Pro»): εξαγωγή CSV,
    Ραντάρ. CTA «Ξεκίνα δωρεάν» → signup για επισκέπτες· «Στα Signals · Τρέχον πλάνο» για συνδεδεμένο χωρίς
    συνδρομή· «Περιλαμβάνεται στο πλάνο σου» για συνδρομητή.
  - Κάθε ισχυρισμός της γραμμής ελέγχεται από `PricingFreePlanTests` απέναντι στο backend (dashboard/company page
    μόνο με login, CSV → pricing, όριο Ραντάρ 0, daily digest χωρίς entitlement, intraday με entitlement). Αν αλλάξει
    ο κανόνας στο backend, η γραμμή Free πρέπει να αλλάξει μαζί.
  - **Χωρίς αλλαγή:** τιμές, billing, συνδρομές, JSON-LD offers (ένα Offer με τιμή 0 το απαγορεύει σκόπιμα το
    `StructuredDataTests`). CSS μόνο με τα υπάρχοντα tokens (`.plan-scope-label`, `.plan-included`, `.plan-excluded`).

- **Gemi Leads 2.0 — pending-company hydration (2026-09-26). ΚΛΕΙΣΤΟ εξ ορισμού, κανένα schedule.**
  Η ασφαλής διαδρομή για τα ευρήματα Discovery v2 που μένουν `pending_no_company` (στο πρώτο G4 SHADOW cycle
  σε production: **578**). `gemiapp/pending_company_hydration.py` + `manage.py hydrate_pending_discovery_companies`.
  - **Ροή:** παρατήρηση Discovery → λείπει τοπικό `Company` → **αυτό:** δημιουργία canonical `Company` →
    **αμετάβλητο** `materialize_new_company_signals` → SHADOW NEW_COMPANY signal → αμετάβλητο G2 pipeline →
    SHADOW opportunities. Η hydration **μόνο** δημιουργεί `Company`: κανένα signal/snapshot/opportunity/match/
    lead/ειδοποίηση/email.
  - **Flag:** `GEMI_DISCOVERY_PENDING_COMPANY_HYDRATION_ENABLED`, **προεπιλογή 0** (settings + `.env.example`).
    Αφιερωμένο flag: το `GEMI_DISCOVERY_V2_ENABLED` ανοίγει και το ingest mode του Discovery (cutover), άρα δεν
    επαναχρησιμοποιείται. Με 0 η εντολή **αρνείται πριν επιλέξει, ζητήσει ή γράψει οτιδήποτε** — και σε
    `--dry-run`. **Καμία εγγραφή στο `apps.SCHEDULES`**, κανένα task.
  - **Επιλογή:** ακριβώς το pending σύνολο του materialiser (`ELIGIBLE_CLASSIFICATIONS` + κανένα τοπικό
    `Company`), ένας αριθμός ΓΕΜΗ ανά γραμμή όσες παρατηρήσεις κι αν έχει, παλαιότερο τεκμήριο πρώτο. Η
    ημερομηνία σύστασης **δεν** είναι κριτήριο (late publications και invalid dates περνούν).
  - **Πηγή:** ένα `GET /companies?arGemi=<n>` ανά εταιρεία μέσω του **κοινού** `GemiClient` (κοινό budget, bounded
    retries, G6 metrics, A4 source records), επικυρωμένο ως `company_search`, lane **`MONITORED_REFRESH`** (κάτω από
    DISCOVERY και DIGEST_IMPORT, όπως ο legacy backfill). Search item και όχι detail: είναι ακριβώς το σχήμα του
    `raw_data` του legacy importer, που ο materialiser ξαναεπικυρώνει για το detection-time baseline. Γίνεται δεκτό
    μόνο item με **ίδιο** `arGemi` (ποτέ Y κάτω από X). Multi-value `arGemi` δεν είναι επαληθευμένο → δεν
    χρησιμοποιείται. **Pacing** 20 s ανάμεσα σε αιτήματα (≤3/λεπτό, μέσα στο headroom 4/λεπτό του G6).
  - **Create-only:** ξανα-έλεγχος ύπαρξης πριν το fetch (αν υπάρχει: skip χωρίς αίτημα) και ξανά μέσα σε **ένα
    savepoint**, μετά `Company.objects.create(**company_defaults(item))` + `sync_company_activities` — τα ίδια
    primitives του legacy importer, καμία δεύτερη normalisation. Race στο insert → `IntegrityError` (unique
    `gemi_number`) → rollback → `race_skipped`, η γραμμή του άλλου writer μένει ανέγγιχτη. Ποτέ `update_or_create`
    (και ο `_ingest` του Discovery το χρησιμοποιεί, γι' αυτό δεν επαναχρησιμοποιήθηκε). Κανένα placeholder.
  - **Αποτυχίες:** ανά εταιρεία (validation, `not_found`, `ambiguous`, σφάλμα εγγραφής) → καταγραφή και
    συνέχεια· σε επίπεδο client (budget timeout/unavailable, retries exhausted, configuration/staging, auth,
    bad request) → **σταματά** η εκτέλεση (`aborted`). Καμία μερική γραμμή, **non-zero exit**, μόνο τύπος
    σφάλματος και αριθμοί ΓΕΜΗ στην έξοδο — ποτέ payload/PII.
  - **`--dry-run` = καμία αλλαγή στη βάση, ΟΧΙ «κανένα αίτημα ΓΕΜΗ»** (κάνει fetch για να δείξει τι θα γινόταν).
  - **Έκθεση στο legacy προϊόν — η απόφαση που προστατεύει το flag:** ένα `Company` είναι canonical, άρα το
    legacy προϊόν το βλέπει όπως κάθε import (dashboard, CSV και — όταν το `incorporation_date` ισούται με την
    ημερομηνία ενός import run — legacy matching/digest). Το `company_defaults` (αμετάβλητο) αποθηκεύει
    missing/invalid/future ημερομηνία ως **σήμερα**: ένα `invalid_date` θα έμπαινε στο **σημερινό** legacy digest
    σαν να συστάθηκε σήμερα. Η αναφορά μετρά σε κάθε εκτέλεση (και dry run) `stored_as_today` και `date_clamped`.
  - **Signal timing αμετάβλητο:** evidence = η αρχική (παλαιότερη) παρατήρηση· `detected_at` = ο κανόνας B2
    `max(discovery, baseline observed_at)`, δηλαδή η στιγμή της hydration (τότε υπάρχει πρώτη φορά state).
  - **G4 ανεξάρτητο:** κανένα άγγιγμα σε `run_g4_shadow_cycle`, cursor, παρατηρήσεις, schedules, flags. **LIVE
    παραμένει απαγορευμένη** — η εντολή δεν έχει επιλογή mode/live και δεν δημιουργεί σήματα.
  - **Χρήση (μόνο μετά από ρητή απόφαση):**
    `GEMI_DISCOVERY_PENDING_COMPANY_HYDRATION_ENABLED=1 python manage.py hydrate_pending_discovery_companies
    --dry-run --limit 20` → έλεγχος `stored_as_today`/`date_clamped` → χωρίς `--dry-run` → `python manage.py
    materialize_new_company_signals`. Επιλογές: `--limit N` (1–200, προεπιλογή 20), `--pace-seconds S`
    (προεπιλογή 20).
  - **Καμία migration.** 26 νέα tests (`gemiapp/test_pending_company_hydration.py`)· έλεγχος mutation: overwrite,
    χωρίς flag, χωρίς pacing, χωρίς recheck, χωρίς identity check, χωρίς savepoint → όλα πιάνονται.
    **2.124 tests OK** (στο feature branch)· `check`, `makemigrations --check` καθαρά.

- **Νέα email templates στο Signal Ledger (2026-09-26). Μόνο εμφάνιση/κείμενο· καμία αλλαγή σε αποστολή.**
  - **Κοινό πλαίσιο `templates/emails/_base.html`** (+ `_button.html`): paper `#f3f2ec`, ένα φύλλο `#fbfbf7` με
    1px ink rule, logo αριστερά και mono metadata δεξιά (το topbar του προϊόντος), τετράγωνες γωνίες. **Όχι**
    emoji, σκιές, pills, στρογγυλές κάρτες, κεντραρισμένο hero ή μπλε — ό,τι έκανε τα παλιά να μοιάζουν
    γενικά/AI. Όλα inline και σε πίνακες (Gmail/Outlook)· preheader, `color-scheme: light`, phone padding.
  - **Digest** (`daily_digest.html/.txt`): ίδιο context. Σύνοψη (πλήθος), «Ταιριάζουν στα Ραντάρ σου» με το amber
    match stroke (3px αριστερό rule, όπως `.product-signal.selected`), «Νέες εγγραφές ΓΕΜΗ» ως register με
    γραμμές `#cbd0cb`, ΓΕΜΗ/ΑΦΜ/τόπος σε mono· **κανένα επινοημένο default** (πριν: «Νέα επιχείρηση»,
    «Ελλάδα»). Ink κουμπί + «Λήψη CSV». Το footer **δεν** λέει πια «έχεις ενεργή συνδρομή» — από 2026-09-06 το
    daily πάει και σε μη συνδρομητές· δείχνει Ρυθμίσεις + διακοπή. Το `.txt` είναι πλέον `autoescape off`
    (πριν ένα `&` στην επωνυμία έβγαινε `&amp;` σε text/plain) και δεν λέει «Καλημέρα» στο alert των 20:00.
  - **Verification / password reset:** επικεφαλίδα, μία πρόταση, κουμπί, σύνδεσμος για αντιγραφή, notice με amber
    rule («ισχύει 3 ημέρες» = `PASSWORD_RESET_TIMEOUT` του Django). Ενικός, όπως όλο το site.
  - **Bug που διορθώθηκε:** το Django `PasswordResetView` έπαιρνε για το **text/plain** το default
    `registration/password_reset_email.html`, δηλαδή έστελνε raw HTML ως κείμενο. Νέο
    `registration/password_reset_email.txt` + `email_template_name` στο `config/urls.py`·
    `TransactionalEmailFormatTests` το κλειδώνει.
  - Subjects, tags Brevo, links, unsubscribe/CSV tokens **αμετάβλητα**. Το `client_outreach.*` **δεν** άλλαξε
    (cold outreach παγωμένο). **2.101 tests OK** (στο feature branch)· καμία migration.

- **Νέο branding: artwork αντί για CSS mark (2026-09-26). Μόνο assets/references, κανένα άλλο UI.**
  - **Assets (`static/images/`, μία πηγή, χωρίς διπλότυπα):** `gemi-leads-logo-horizontal.png` (icon + λεκτικό,
    400×100, διαφανές) · `gemi-leads-icon.png` (512², διαφανές) · `favicon.ico` (16/32/48) · `favicon.png` (96²,
    **ίδιο όνομα**, ώστε και τα παλιά email που το δείχνουν απόλυτα να δείχνουν πλέον το νέο icon) ·
    `apple-touch-icon.png` (180², σε `--product-white`, γιατί το iOS γεμίζει τη διαφάνεια με μαύρο) ·
    `gemi-leads-logo-email.png` (256×64, **flattened σε `#fbfbf7`**, μόνο για email) · `gemi-leads-og.png`
    (1200×630, στο cream του artwork). Το παλιό `logo.png` (μπλε radar) **διαγράφηκε**.
  - **Διαφάνεια:** η κάρτα του artwork έχει το **ίδιο** cream με το φόντο, οπότε color-key θα την τρυπούσε. Η κάρτα
    εντοπίστηκε από το αχνό της περίγραμμα και μένει αδιαφανής (μαζί με τα παράθυρα του κτιρίου)· μόνο το
    εξωτερικό φόντο, τα counters των γραμμάτων και η διάχυτη σκιά έγιναν διαφανή (το σύστημα είναι flat). Τα
    anti-aliased άκρα είναι un-blended, άρα καθαρά σε οποιοδήποτε φόντο. Το ίδιο το σχέδιο **δεν** άλλαξε.
  - **Πού:** full logo σε **και τα τρία** topbars (`.product-brand`, `.public-brand`, `.admin-brand`) ως
    `<img class="brand-logo" alt="Gemi Leads">`, **25px** ύψος παντού — το μέγιστο που χωρά μαζί με το BETA μέσα
    στη στήλη 184px του rail (επαληθεύτηκε: καμία υπερχείλιση σε 1280/768/375/320, ίδια ύψη bar 58/52px). Το
    λεκτικό **δεν** ξαναγράφεται σε HTML δίπλα του. Το CSS-drawn `.product-mark` και τα `.*-brand b` αφαιρέθηκαν.
    Favicon set σε `base.html` **και** `superadmin/base.html` (που πριν δεν είχε κανένα favicon). `og:image` →
    `gemi-leads-og.png`, JSON-LD `logo` → `gemi-leads-icon.png`. Τα emails: βλ. «Νέα email templates».
  - Νέο `BrandAssetTests`: κάθε `static 'images/…'` / `gemileads.gr/static/images/…` σε template πρέπει να
    υπάρχει, και τα headers αποδίδουν το artwork χωρίς το παλιό mark. **2.100 tests OK** (στο feature branch)· καμία migration.

- **Gemi Leads 2.0 — G6: παρατηρησιμότητα του request budget του ΓΕΜΗ (2026-09-21). Μέτρηση, όχι απόφαση.**
  - **Τι μετριέται:** **κάθε πραγματική εξερχόμενη απόπειρα**, όχι οι λογικές κλήσεις. Μία κλήση που κάνει
    retry ξοδεύει δικό της slot κάθε φορά, άρα γράφει μία γραμμή ανά απόπειρα (`attempt` 1..n· ό,τι είναι
    πάνω από 1 είναι retry). Σημείο μέτρησης: η **μοναδική** εξερχόμενη διαδρομή, το
    `GemiClient.get()` — ένα `self._transport(...)` και ένα `budget.acquire(...)` σε όλο το repository.
  - **Πεδία:** χρόνος, lane, αριθμός απόπειρας, κλάση αποτελέσματος (`success` / `rate_limited` /
    `server_error` / `client_error` / `transport_error` / `budget_timeout` / `budget_unavailable`), HTTP
    status όταν υπήρξε απάντηση, **χρόνος αναμονής στο κοινό budget** (ms) και **κανονικοποιημένο**
    endpoint.
  - **Τι ΔΕΝ αποθηκεύεται:** API key, παράμετροι ερωτήματος, payload, αριθμοί ΓΕΜΗ, δεδομένα εταιρειών ή
    πελατών. Το `normalise_endpoint` κόβει το query string και αντικαθιστά κάθε αριθμητικό τμήμα με `{id}`
    (`/companies/123456789000` → `/companies/{id}`). Το test το επιβάλλει σε **όλα** τα πεδία της γραμμής.
  - **Γιατί πίνακας και όχι το κοινό DatabaseCache** (ελέγχθηκε, δεν υποτέθηκε): (1) ο `DatabaseCache` δεν
    υλοποιεί `incr`, οπότε κληρονομεί το `BaseCache.incr` = get + πρόσθεση σε Python + set, **χωρίς
    κλείδωμα** — δύο workers χάνουν μετρήσεις· (2) το `_cull` **σβήνει** γραμμές όταν περαστεί το
    `MAX_ENTRIES`, δηλαδή ακριβώς όταν η κίνηση είναι υψηλή· (3) το cache API **δεν απαριθμεί** κλειδιά. Το
    budget δεν χρειάζεται τίποτα από αυτά γιατί χρησιμοποιεί `add()` σε κλειδί που κανείς άλλος δεν
    διεκδικεί, που **είναι** ατομικό. Άρα: ένας μικρός append-only πίνακας `GemiRequestAttempt`
    (**migration 0055**, όπως ζητήθηκε: ορθότητα πάνω από την αποφυγή migration). **Ένα** index,
    `(occurred_at, outcome)`: το `occurred_at` προηγείται, οπότε εξυπηρετεί και τα δύο range φίλτρα της αναφοράς
    (επαληθεύτηκε με `EXPLAIN QUERY PLAN`: index range seek, χωρίς επιπλέον sort). Ένα δεύτερο index μόνο στο
    `occurred_at` θα ήταν διπλότυπο και αφαιρέθηκε πριν από το deploy.
  - **Peak:** με μία γραμμή ανά απόπειρα και ακριβή χρόνο, το peak υπολογίζεται σε **αληθινό κυλιόμενο
    παράθυρο 60 δευτερολέπτων** (τα παράθυρα αγκυρώνονται σε κάθε απόπειρα — το μέγιστο οποιουδήποτε
    διαστήματος 60s πιάνεται πάντα σε διάστημα που ξεκινά σε απόπειρα). **Δεν** είναι bucket ημερολογιακού
    λεπτού και η αναφορά το λέει ρητά· τα «saturated windows» είναι επικαλυπτόμενα αγκυρωμένα παράθυρα, όχι
    ξεχωριστές περίοδοι.
  - **Ασφάλεια production:** καμία επιπλέον κλήση ΓΕΜΗ· ένα μικρό INSERT **μετά** την απάντηση· **fail-open**
    (κάθε σφάλμα καταγραφής καταπίνεται και λογαρίζεται — η ingestion δεν σπάει ποτέ επειδή έσπασε η
    μέτρηση). Όλη η παρατήρηση — ταξινόμηση status, χρονοσφραγίδα, εγγραφή — είναι μέσα σε **ένα** guard στο
    `GemiClient._observe`, ώστε τίποτα από αυτήν να μην μπορεί να αντικαταστήσει το σφάλμα budget που θα πάρει ο
    καλών· τα logs γράφουν μόνο τον **τύπο** της εξαίρεσης, ποτέ το μήνυμα· το INSERT τρέχει σε **δικό του savepoint** ώστε σφάλμα βάσης να μη δηλητηριάσει transaction
    του καλούντος· το budget μένει **fail-closed** (αποτυχία budget καταγράφεται και **ξαναρίχνεται
    αυτούσια**, δεν στάλθηκε τίποτα και δεν μετρά στο ceiling). Διακόπτης:
    `GEMI_REQUEST_METRICS_ENABLED` (προεπιλογή 1).
  - **Αναφορά:** `python manage.py report_gemi_request_budget --hours 24` — **μόνο ανάγνωση**: καμία κλήση
    ΓΕΜΗ, καμία εγγραφή. Το «outbound attempts» είναι **μόνο** όσες έφτασαν στο transport· τα budget timeout /
    unavailable έχουν δική τους γραμμή και **δεν** μετρούν ούτε εκεί ούτε στο peak.
  - **Δύο όρια, πάντα και τα δύο:** το **ασφαλές ανώτατο** `MAX_REQUESTS_PER_MINUTE = 7` (θεωρητικό μέγιστο)
    και η **effective configured capacity** — το `GEMI_RATE_LIMIT_PER_MINUTE` με το clamp 2..7 του
    `BudgetConfig.from_settings()`, που η αναφορά **καλεί** αντί να το ξαναγράφει, ώστε να μη διαφωνεί ποτέ με το
    budget. Utilisation και headroom τυπώνονται και για τα δύο. **Κάθε λειτουργική απόφαση G6 χρησιμοποιεί την
    effective capacity**, όχι το 7: με όριο 5 και peak 3 μένουν 2, όχι 4. Το headroom **δεν** είναι ποτέ
    αρνητικό· peak πάνω από το όριο εμφανίζεται ως ξεχωριστό **OVER CAPACITY** (ή **AT CAPACITY** όταν ισούται).
    Τα saturated / high-utilisation παράθυρα κρίνονται έναντι της effective capacity. Δίνει παράθυρο,
    απόπειρες (outbound / retries / λογικές κλήσεις / όσες δεν στάλθηκαν),
    ανά lane, 429 / 5xx / 4xx / transport, budget timeout & unavailable, συνολική-μέση-μέγιστη αναμονή, peak
    σε κυλιόμενο λεπτό, utilisation και headroom έναντι του ασφαλούς ορίου 7/λεπτό, saturated και
    high-utilisation παράθυρα, και κανονικοποιημένα endpoints.
  - **Δεν κρίνει το G6.** Το repository **δεν** ορίζει αριθμητικό κατώφλι για το G6, οπότε η εντολή τυπώνει
    γεγονότα και λέει ρητά ότι το `G6_STATUS` δεν αποφασίζεται εκεί. Κενό παράθυρο δηλώνεται ως κενό, **όχι**
    ως headroom.
  - **`G4_STATUS = NOT_STARTED / NOT_PASSED`** — η 14ήμερη παρατήρηση SHADOW δεν ξεκίνησε, **καμία** νέα
    προγραμματισμένη εργασία 2.0 δεν μπήκε στο `apps.SCHEDULES`, και η **LIVE παραμένει απαγορευμένη**.
  - **Ανοιχτό (τεκμηριωμένο, όχι κρυμμένο):** ο πίνακας είναι append-only χωρίς retention. Στο ceiling των
    7/λεπτό το απόλυτο άνω όριο είναι ~10.080 γραμμές/ημέρα (στην πράξη πολύ λιγότερες)· χρειάζεται
    ξεχωριστή απόφαση για purge, όπως έγινε με το `purge_gemi_source_records`.
  - 46 νέα tests (`gemiapp/test_gemi_request_metrics.py`)· **2.098 tests OK**· `check`,
    `makemigrations --check` καθαρά.

- **Gemi Leads 2.0 — Organization Radar: διόρθωση του chevron του dropdown (2026-09-21). UX μόνο.**
  - **Το bug (από production):** με το dropdown ανοιχτό, κλικ στο chevron δεν έκανε φαινομενικά τίποτα. Ο
    handler του chevron **έκλεινε** τη λίστα, αλλά το ίδιο κλικ συνέχιζε να ανεβαίνει στον γονέα
    (`field?.addEventListener('click', openResults)`) που την **ξανάνοιγε** αμέσως — μέσα στο ίδιο κλικ.
  - **Η διόρθωση:** το κλικ του chevron κάνει `stopPropagation()`, **και** ο opener του field αγνοεί κλικ που
    προέρχονται από το chevron (`if (!toggle?.contains(event.target))`). Δύο φράχτες, γιατί ο ένας από τους
    δύο θα έσπαγε σιωπηλά αν κάποιος αναδιατάξει τους listeners. Ό,τι άλλο μένει ως έχει: input/focus
    ανοίγουν, Escape και κλικ έξω κλείνουν, η επιλογή κρατάει τη λίστα ανοιχτή.
  - **Δεύτερο bug της ίδιας οικογένειας, που βρέθηκε γράφοντας το test:** μια απάντηση αναζήτησης που
    ερχόταν **αφού** ο πελάτης είχε κλείσει τη λίστα την ξανάνοιγε. Το `closeResults()` κάνει πλέον
    `controller?.abort()`, άρα το κλείσιμο ακυρώνει και το request που τρέχει.
  - **Νέο είδος test: `gemiapp/jstests/picker_dropdown.mjs`.** Και τα δύο bugs είναι bugs **διάδοσης
    γεγονότων** — δύο listeners που μιλάνε μεταξύ τους — και είναι **αόρατα** σε test που απλώς διαβάζει τον
    κώδικα. Οπότε το script τρέχει **πραγματικά**, πάνω σε ένα DOM τόσο μικρό όσο χρειάζεται ο picker
    (attribute selectors, capture-then-bubble dispatch με `stopPropagation`, dataset, classList, focus):
    χωρίς browser, χωρίς jsdom, **χωρίς νέα npm εξάρτηση**. Τρέχει μέσα από το
    `test_organization_radar_ui.PickerDropdownTests` (skip όπου δεν υπάρχει node) και το Python test απαιτεί
    να **ονομαστεί** κάθε έλεγχος που πέρασε, ώστε ένα harness που σταμάτησε να τρέχει μια περίπτωση να μην
    περνάει σιωπηλά. Επαληθεύτηκε ότι **κόβει**: με τον παλιό κώδικα πέφτει το «open -> click chevron ->
    closed», με bubble-phase listener πέφτουν τα checks της επιλογής, χωρίς abort πέφτει το late-response.
  - **Καμία αλλαγή σε backend/search/matcher**, καμία migration, καμία αλλαγή στην αναπαράσταση που
    ποστάρεται. **2.052 tests OK**. Επαληθεύτηκε και σε πραγματικό browser με **αληθινά κλικ ποντικιού**
    στο chevron: κλειστό → ανοιχτό → κλειστό.

- **Gemi Leads 2.0 — Organization Radar: τα κριτήρια έγιναν κανονικά dropdown (2026-09-20). UX μόνο.**
  - **Το πρόβλημα:** τα αναζητήσιμα multi-select της ίδιας μέρας άνοιγαν **μόνο** αφού πληκτρολογήσεις δύο
    χαρακτήρες. Ένα κενό πλαίσιο αναζήτησης δεν λέει τι υπάρχει μέσα: ο πελάτης δεν ξέρει καν ότι υπάρχει
    λίστα.
  - **Νέο:** `browse_reference(kind, limit)` και `reference_options(kind, query, limit)` στο
    `gemiapp/reference_search.py`. Ένα κλικ ή focus ζητάει το **ίδιο** endpoint με κενό `q` και παίρνει την
    **αρχή** του καταλόγου· γράψιμο φιλτράρει την ίδια λίστα. Ένα σημείο εισόδου, ένα όριο.
  - **Το όριο δεν άλλαξε:** `MAX_LIMIT = 50`, προεπιλογή `DEFAULT_LIMIT = 40`, ίδιο για αναζήτηση και για το
    άνοιγμα. Καμία από τις ~19.000 γραμμές ΚΑΔ δεν μπαίνει ποτέ στη σελίδα ή στη μνήμη του browser: το
    άνοιγμα είναι μία μικρή απάντηση, που κρατιέται (`browseCache`) και ξαναχρησιμοποιείται σε κάθε επόμενο
    άνοιγμα του ίδιου picker.
  - **Συμπεριφορά dropdown:** άνοιγμα με κλικ/focus/βελάκι κάτω, κλείσιμο με Escape ή κλικ έξω, πλοήγηση με
    Arrow Up/Down + Enter, κατάσταση φόρτωσης, «Δεν βρέθηκαν αποτελέσματα» όταν δεν υπάρχει τίποτα. Ό,τι
    είναι ήδη chip **δεν προσφέρεται ξανά**· αφαίρεση του chip το ξαναφέρνει στη λίστα. Μετά από επιλογή το
    dropdown μένει ανοιχτό στην αρχική λίστα, για τη δεύτερη και τρίτη επιλογή.
  - **Bug που βρέθηκε στον browser και διορθώθηκε:** ο listener «κλικ έξω» ήταν στο bubble phase. Η επιλογή
    ενός option αντικαθιστά τη λίστα, άρα ο κόμβος που πατήθηκε είναι **ήδη αποσπασμένος** όταν φτάσει το
    event στο document, το `root.contains(target)` το διάβαζε ως κλικ έξω και **έκλεινε** το dropdown σε κάθε
    επιλογή. Ο listener πήγε στο capture phase· το `PickerScriptTests` το κρατάει εκεί.
  - **Όψη:** chevron μέσα στο πεδίο (γυρίζει όταν ανοίγει), πάνελ 400px (300px σε κινητό), εντονότερο
    hover/active με amber. Ίδια primitives: paper, ink rule, amber επιλογή.
  - **Η αναπαράσταση ΔΕΝ άλλαξε** — ούτε τώρα ούτε στο προηγούμενο βήμα. Τα πεδία ΚΑΔ ποστάρουν μία γραμμή
    `"<code> <version>"` το καθένα, τα υπόλοιπα primary keys· `parse_radar_form` αυτούσιο, matcher/scoring/
    signals/billing/legacy CustomerRadar/schema ανέγγιχτα. **Καμία migration.**
  - **Έλεγχος:** 21 νέα tests (bounded αρχική λίστα, `reference_options`, endpoint με κενό `q`, μη διπλή
    επιλογή, wiring του dropdown στο `app.js`)· **2.050 tests OK**. Στον browser, σε αντίγραφο dev με 83
    ΚΑΔ: κλικ στο κενό πεδίο δίνει 40 γραμμές, «εστίαση» φιλτράρει σε 2, η επιλογή γράφει
    `99380002 kad_2026`, το Radar αποθηκεύεται με σωστή έκδοση ΚΑΔ και το edit επιστρέφει chips με ετικέτα.

- **Gemi Leads 2.0 — Organization Radar: αναζητήσιμα multi-select κριτηρίων (2026-09-20). UX μόνο.**
  - **Το πρόβλημα:** η φόρμα ζητούσε να **πληκτρολογήσεις** κωδικούς ΚΑΔ και να διαλέξεις περιοχές από στενά
    `<select multiple>`. Με ~19.000 γραμμές ΚΑΔ ο κατάλογος δεν μπορεί να αποδοθεί στη σελίδα, και το «γράψε
    62010000 από μνήμης» δεν είναι προϊόν.
  - **Νέο:** `gemiapp/reference_search.py` + endpoint `api/reference/` (`login_required`, `require_GET`) και
    `templates/includes/reference_picker.html` — ένα αναζητήσιμο multi-select ανά κριτήριο (ΚΑΔ, περιφερειακή
    ενότητα, δήμος, νομική μορφή, ×2 για στόχευση και εξαιρέσεις).
  - **Αναζήτηση:** case-insensitive **και accent-insensitive** για ελληνικά. Ούτε το `UPPER` ούτε το `LIKE` του
    SQLite είναι αξιόπιστα σε ελληνικά (τα tests τρέχουν σε SQLite, η production σε PostgreSQL), οπότε η
    σύγκριση γίνεται σε Python πάνω σε normalised haystack με το **υπάρχον** `normalize_kad_search` — ίδια
    συμπεριφορά και στις δύο βάσεις. Ταιριάζει όταν **κάθε** token υπάρχει στο `source_id + description`·
    καθαρά αριθμητικό query είναι και prefix κωδικού. `ΕΣΤΙΑΣΗ` / `Εστίαση` / `εστιαση` ταυτόσημα· `ΧΙΟ` →
    `ΧΙΟΥ`. Ο δήμος βρίσκεται και μέσω της περιφερειακής του ενότητας.
  - **Κόστος:** ο normalised κατάλογος χτίζεται μία φορά και κρατιέται σε cache με κλειδί ένα **stamp** του
    πίνακα (πλήθος + newest pk + max updated_at, hashed): ένας reference sync τον ακυρώνει μόνος του και κανένα
    test δεν βλέπει γραμμές άλλου test. Αποτελέσματα φραγμένα (`MAX_LIMIT = 50`).
  - **Η αναπαράσταση ΔΕΝ άλλαξε — αυτό είναι το κρίσιμο.** Τα πεδία ΚΑΔ ποστάρουν ακόμη μία γραμμή
    `"<code> <version>"` το καθένα· τα υπόλοιπα ποστάρουν ακόμη primary keys reference γραμμών. Το
    `parse_radar_form` είναι **αυτούσιο**, άρα ο αποθηκευμένος ορισμός και ο matcher βλέπουν ό,τι έβλεπαν.
    Η έκδοση ΚΑΔ διατηρείται πάντα (και φαίνεται ως tag «ΚΑΔ 2026»).
  - **Progressive enhancement:** ο παλιός `<textarea>` / `<select multiple>` αποδίδεται ακόμη και είναι αυτό που
    ποστάρει **χωρίς JavaScript**· το script τον κρύβει **και τον απενεργοποιεί** (disabled control δεν
    υποβάλλεται) και αναλαμβάνουν τα chips. Καμία νέα frontend εξάρτηση — ~110 γραμμές vanilla JS δίπλα στον
    υπάρχοντα legacy KAD picker, με debounce, AbortController και πλοήγηση από πληκτρολόγιο.
  - **Chips:** `RadarForm.selections` δίνει σε κάθε κριτήριο τις τρέχουσες επιλογές **με ετικέτα καταλόγου**
    («47191002 — ΛΙΑΝΙΚΟ ΕΜΠΟΡΙΟ» αντί για γυμνό κωδικό ή pk). Γεμίζει και στις δύο διαδρομές — αποθηκευμένος
    ορισμός **και** απορριφθέν post — άρα οι επιλογές επιβιώνουν ένα σφάλμα validation.
  - **Layout:** τα κριτήρια σε πλέγμα `minmax(420px, 1fr)`, πάνελ αποτελεσμάτων 320px (240px σε κινητό), μία
    στήλη σε κινητό χωρίς οριζόντιο scroll. Ίδια primitives: paper, ink rule, amber επιλογή.
  - **Ασφάλεια:** το endpoint θέλει συνδεδεμένο χρήστη· τα δεδομένα είναι **πλατφόρμας** (κοινά για όλους), οπότε
    δεν είναι tenant-scoped — οι **εγγραφές** Radar μένουν αυστηρά organization-scoped μέσω
    `organization_access`, αμετάβλητες. Ο G5 guard (καμία αναφορά «organization» στο `gemiapp/views.py`)
    τηρείται: η νέα view δεν αγγίζει tenant δεδομένα.
  - **Καμία migration**, καμία αλλαγή σε matching/scoring/signals/billing/legacy CustomerRadar. 29 νέα tests
    (`test_reference_search.py` 24, `test_organization_radar_ui.PickerTests` 5)· **2.028 tests OK**.
    Επαληθεύτηκε και στον browser: «εστίαση» βρίσκει τα ΥΠΗΡΕΣΙΕΣ ΕΣΤΙΑΣΗΣ, «ΧΙΟ» βρίσκει «ΧΙΟΥ», το chip
    ποστάρει `999056101 kad_2026`, το Radar αποθηκεύεται και στο edit επιστρέφει ως chip.

- **Gemi Leads 2.0 — ενεργή ειδοποίηση operator για αποτυχίες ingestion ΓΕΜΗ (2026-09-20). Χωρίς Sentry.**
  - **Η απαίτηση, διατυπωμένη σωστά:** «**ERROR σε production από τα `gemiapp.ingestion.client` /
    `gemiapp.services` πρέπει να ειδοποιεί ενεργά έναν operator**». Η παλιά διατύπωση ονόμαζε το Sentry· γράφτηκε
    σε **αυτό** το branch στις 2026-09-15 (`74def43`, πακέτο A2) και **ποτέ** δεν ίσχυσε σε deployed κώδικα. Το ίδιο
    το Sentry είναι παλαιότερο (`f0885c1` το SDK, `da3f72b` το `SENTRY_DSN` στο `render.yaml`, Αύγουστος, στο
    `main`) αλλά **τίποτα δεν εξαρτάται από αυτό**: το `sentry_sdk.init` είναι πίσω από `if SENTRY_DSN:` και το
    `gemiapp` δεν το κάνει import πουθενά. **Το Sentry είναι μία έγκυρη υλοποίηση — δεν απαιτείται και δεν
    υπάρχει σε αυτό το deployment.**
  - **Γιατί υπάρχει η απαίτηση:** από την 0054 το `GemiClient._validate` **ξαναρίχνει** σε απάντηση που κόβει το
    A2 contract, χωρίς retry· το `fetch_companies` επικυρώνει κάθε σελίδα πριν γραφτεί οτιδήποτε, το
    `import_for_date` σημειώνει `ImportRun.status=failed` και ξαναρίχνει, και το `send_digests` **δεν τρέχει
    ποτέ**. Καμία ειδοποίηση = το μαθαίνει πρώτος ο πελάτης από το digest που δεν ήρθε.
  - **Υλοποίηση:** `AdminEmailHandler` του Django πάνω στο **υπάρχον** SMTP relay που ήδη στέλνει τα digests —
    καμία νέα υπηρεσία, κανένα νέο credential, καμία νέα εξάρτηση.
    - **Παραλήπτες:** `ADMINS = operator_admins(SUPERADMIN_EMAILS)` στο `config/settings.py`· **καμία διεύθυνση
      γραμμένη σε κώδικα** (το `render.yaml` ορίζει ήδη το `SUPERADMIN_EMAILS`). Άδειο μέσα → άδειο έξω: χωρίς
      `ADMINS` ο handler γυρίζει πριν φτιάξει μήνυμα, άρα «καμία ειδοποίηση», ποτέ σφάλμα.
    - **Δρομολόγηση:** το `LOGGING` δίνει `operator_console` (stderr) + `operator_email` **ακριβώς** στους δύο
      loggers, σε `WARNING` — ό,τι ακριβώς είχαν πριν (χωρίς `LOGGING`, έπεφταν στο `logging.lastResort`, που
      είναι WARNING/stderr). **Τίποτα δεν κρύβεται και τίποτα νέο δεν εκπέμπεται.** Το email handler είναι
      **ERROR only**, με το φίλτρο `require_debug_false` (σε development καμία ειδοποίηση), και δεν κρέμεται από
      κανέναν πρόγονο → **ένα record δίνει το πολύ ένα μήνυμα**. Το `propagate` μένει ανοιχτό (ώστε
      `assertLogs("gemiapp")` και τυχόν μελλοντικός root handler να βλέπουν το record).
    - **Απομόνωση αποτυχίας:** `config/operator_alerts.OperatorEmailHandler` στέλνει κάθε δική του αποτυχία στο
      `logging.Handler.handleError`. Το Django ήδη στέλνει με `fail_silently`· αυτό καλύπτει όλα τα υπόλοιπα,
      ώστε ένα χαλασμένο relay **να μην μπορεί** να βγει από το `logger.error()`, να μπει στο ingestion και να
      κρύψει το σφάλμα που ανέφερε. Το αρχικό σφάλμα φτάνει ούτως ή άλλως στο stderr.
    - **Tests:** ο runner αδειάζει τα `ADMINS` για όλο το suite, ώστε **κανένα** test να μην ειδοποιεί operator·
      όποιο test θέλει τη δρομολόγηση τη ζητά με `override_settings(ADMINS=...)`.
  - **Παθητικά τεκμήρια, αμετάβλητα:** Render stderr (WARNING/ERROR φτάνουν, INFO όχι — όπως και πριν),
    `ImportRun.status`/`error_message` στο `/superadmin/pipeline/`, γραμμές αποτυχίας django-q στο `/admin/`
    (όριο `save_limit: 50`), `diagnose_intraday`. Κρατούν την απόδειξη· **δεν ειδοποιούν κανέναν.**
  - **Απομένει (μόνο σε production):** μία επιβεβαίωση ότι φτάνει πράγματι email — `manage.py sendtestemail
    --admins` μετά το deploy, μέσα στο smoke checklist.
  - **Follow-up, σκόπιμα ΕΚΤΟΣ αυτής της εργασίας:** ημερήσιος έλεγχος αποτελέσματος (heartbeat) που ειδοποιεί
    όταν λείπει το αναμενόμενο `ImportRun` ή οι `DigestDelivery` — πιάνει και κάθε άλλη αιτία χαμένου digest
    (νεκρός worker, κολλημένο schedule, SMTP outage). Το `diagnose_intraday` έχει ήδη τα queries.
  - **Καμία migration**, καμία αλλαγή σε ingestion/retries/billing. 21 νέα tests
    (`gemiapp/test_operator_alerts.py`)· **1.994 tests OK**· `check`, `check --deploy` (με τους διακόπτες του
    `render.yaml`) και `makemigrations --check` καθαρά. **`G0_STATUS = PASSED_WITH_DOCUMENTED_SCOPE`,
    `G1_STATUS = PASSED`**, η απαίτηση A2 κλειστή. **`G4_STATUS = NOT_PASSED`**, LIVE **απαγορευμένη**, καμία
    merge, κανένα deploy.

- **Gemi Leads 2.0 — `G1_STATUS = PASSED` (τελική πρόβα parity στο staging, 2026-09-20 07:23–07:26 UTC).**
  Πλήρης καταγραφή: `docs/RELEASE_READINESS.md`.
  - **Η πρόβα:** rollback 0054→0031 → **αυθεντική baseline στο 0031** → forward 0031→0054 → rollback 0054→0031 →
    reapply 0031→0054. Πριν από **κάθε** καταστροφική εντολή επαναεπιβεβαιώθηκαν: PostgreSQL,
    `GEMI_LEADS_ENVIRONMENT=staging`, GEMI collector **απενεργοποιημένος** (η production έχει key· το staging όχι)
    και αμετάβλητο **αδιαφανές fingerprint** της συνδεδεμένης βάσης. **Η production δεν επικοινωνήθηκε ποτέ**,
    κανένα αίτημα ΓΕΜΗ, κανένας τοπικός worker/server συνδεδεμένος.
  - **Ορισμός digest (αυστηρότερος από πριν):** για κάθε dataset διαβάστηκε από το `information_schema` η **πλήρης
    λίστα στηλών του 0031** στη baseline και επαναχρησιμοποιήθηκε αυτούσια σε κάθε στάδιο — καμία στήλη 2.0 δεν
    μπορεί να μπει, καμία στήλη δεν παραλείπεται σιωπηλά:
    `md5(string_agg(md5(row(<κάθε στήλη 0031>)::text), '' ORDER BY id))`. Για το `companyactivity` ισχύει ο
    τεκμηριωμένος κανόνας συμβατότητας (στο 0054 μόνο οι `legacy_listed` γραμμές· στο 0031 δεν υπάρχει η στήλη).
    Καμία γραμμή, κανένα PII δεν τυπώθηκε· μόνο πλήθη και digests.
  - **Αποτέλεσμα: και τα 13 legacy datasets ταυτίστηκαν με τη baseline σε πλήθος ΚΑΙ digest σε ΚΑΘΕ στάδιο**
    (auth_user 17, usersubscription 17, customerradar 17, radarmatch 12.549, usercompanylead 12.542,
    digestpreference 17, digestdelivery 398, outreachsuppression 29, personsuppression 0, importrun 750,
    activitycode 9.911, **company 4.903**, **companyactivity 33.391**). Τα δύο τελευταία είναι **ίδια με την
    production baseline**, άρα η πρόβα έτρεξε σε production-shaped όγκο. Μετά το reapply, `legacy_listed = false`
    → **0** γραμμές.
  - **Χρόνοι:** rollback 20,64 s · **forward 32,40 s** (`0034` **3,85 s**, `0035` **4,25 s**) · rollback 20,58 s ·
    **reapply 35,93 s** (`0034` **4,22 s**, `0035` **4,68 s**). Οι μόνες δύο migrations που αγγίζουν legacy πίνακα
    μένουν κοντά στα 4 s — μαζί με τα δύο μη-concurrent partial unique indexes της `0035` πάνω σε 33.391 γραμμές.
  - **D37 lifecycle: 1 → 0 → 1 → 0 → 1.** Schedules 4 → 3 → 4 → 3 → 4, **μηδέν διπλότυπα funcs** σε κάθε στάδιο·
    τα ids των legacy schedules (1, 3, 22) αμετάβλητα· `check` καθαρό και `migrate --check` καθαρό στο 0054.
  - Τα staging-only 2.0 smoke fixtures καταστράφηκαν από τα rollbacks (εγκεκριμένο). **Κανένα legacy δεδομένο δεν
    άλλαξε.** Τα digests της 2026-09-19 αντικαθίστανται: κάλυπταν υποσύνολο στηλών.
  - **`G0_STATUS = PASSED_WITH_DOCUMENTED_SCOPE`, `G1_STATUS = PASSED`.** Απομένει **ένας** blocker για dark
    deploy: **Sentry** — αν το `SENTRY_DSN` είναι ορισμένο στο production και αν υπάρχει alert rule για ERROR των
    `gemiapp.ingestion.client` / `gemiapp.services`· **δεν επαληθεύεται από κώδικα**, μόνο από τα dashboards.
    `G6_STATUS = NOT_MEASURED` (όχι blocker). **`G4_STATUS = NOT_PASSED`**, LIVE **απαγορευμένη**, καμία merge,
    κανένα deploy, καμία production migration.

- **Gemi Leads 2.0 — Pre-dark-deployment closure (2026-09-20).** Πλήρης καταγραφή: `docs/RELEASE_READINESS.md`,
  που πλέον ταξινομεί **κάθε** ισχυρισμό ως **[A] repository-recorded**, **[B] externally observed** ή **[C] open**.
  - **`G0_STATUS = PASSED_WITH_DOCUMENTED_SCOPE`.** Αφαιρέθηκε το παράγωγο κριτήριο «ξεχωριστό staging **web +
    worker service**»: το `AGENTS.md` ζητά staging **βάση**, ξεχωριστό GEMI key για staging και γνωστό όγκο
    production — όχι δεύτερη υπηρεσία. Το πρακτικό staging είναι **τοπικές διεργασίες εφαρμογής → ξεχωριστή staging
    PostgreSQL** (Supabase, `GEMI_LEADS_ENVIRONMENT=staging`, όλα τα staging guards ενεργά). Ό,τι **δεν**
    δοκιμάζεται έτσι (Render build/boot/env wiring) καταγράφεται ρητά με τα υποκατάστατα που εκτελέστηκαν.
  - **`G1_STATUS = PARTIAL` — σκόπιμα ΔΕΝ δηλώνεται passed.** Forward 0031→0054, rollback 0054→0031, reapply και
    κύκλος D37 `0→1→0→1`: όλα πέρασαν στο staging **[B]**. Το snapshot parity (counts + md5, read-only 2026-09-20)
    δείχνει `gemiapp_company` = **4.903** και legacy-visible `gemiapp_companyactivity` = **33.391**, **ίδια με την
    production baseline** — καμία legacy γραμμή δεν δημιουργήθηκε ή χάθηκε από την αλυσίδα 2.0, και **0** γραμμές
    `legacy_listed = false`. **Όμως** δεν υπάρχει digest **πριν** το forward σε αυτή τη βάση, άρα το κριτήριο
    «ίδια aggregates πριν/μετά» δεν τεκμηριώνεται. Καμία parity απόδειξη δεν κατασκευάστηκε.
  - **Production volume (authorized read-only) [B]:** PostgreSQL 17.6, `gemiapp` στην 0031, 0032–0054 μη
    εφαρμοσμένες, κανένας πίνακας 2.0, `Company` = **4.903**, `CompanyActivity` = **33.391**, 3 django-q schedules,
    D37 = 0. Αυτό ορίζει και το μόνο πραγματικό lock risk: η `0035` χτίζει δύο partial unique indexes πάνω σε
    33.391 γραμμές (μη-concurrent, `ACCESS EXCLUSIVE` όσο τρέχει).
  - **Τοπική πρόβα εφαρμογής πάνω στο staging [B]:** `check` καθαρό· `check --deploy` με τους διακόπτες του
    `render.yaml` (`DJANGO_DEBUG=0`, μακρύ secret, πραγματικά allowed hosts) **καθαρό** — οι 6 προειδοποιήσεις με
    το τοπικό `.env` οφείλονται αποκλειστικά στο `DEBUG=1`· `config.wsgi.application` φορτώνει· η εφαρμογή σερβίρει
    `GET /` 200, `/accounts/login/` 200, `/dashboard/` 302. **Ο `qcluster` δεν μπορεί να τρέξει σε Windows**
    (το django-q2 απαιτεί `multiprocessing` start method `fork`)· αντ' αυτού επαληθεύτηκε ότι και οι 4 `func` του
    `apps.SCHEDULES` φορτώνουν και ότι ο schema guard του D37 δίνει True στην 0054. Μετά την πρόβα: 4 schedules,
    **D37 ακριβώς 1**, ουρά άθικτη, `importrun` αμετάβλητο. **Κανένα GEMI job, κανένα αίτημα ΓΕΜΗ** (ο scheduler
    απενεργοποιήθηκε πριν το boot· και τα 4 staging schedules ήταν past due).
  - **Build rehearsal [A]:** `npm ci` (73 πακέτα), `npm run build:css` (`app.css` 27.296 bytes), `collectstatic`
    (1 copied, 134 unmodified, 372 post-processed). Το `pip install` παραλείφθηκε σκόπιμα (θα άλλαζε το venv).
  - **Data safety:** στο `.gitignore` προστέθηκαν `*.dump`, `*.sql`, `*.sql.gz`. Κανένα dump/SQL δεν είναι tracked
    σε κανένα branch· τα δύο τοπικά production dumps παραμένουν untracked και **δεν** διαγράφηκαν/μετακινήθηκαν.
    Προσοχή: οι κανόνες προστατεύουν ένα checkout μόνο όταν το branch που τους φέρει είναι checked out εκεί.
  - **Sentry (πύλη A2) — ο ΕΝΑΣ χειροκίνητος blocker.** Ο κώδικας υποστηρίζει `SENTRY_DSN` (sentry-sdk 2.0.0,
    `DjangoIntegration`, default logging integration → ERROR = event) **[A]**· το `render.yaml` το δηλώνει
    `sync: false` **[A]**. Αν είναι όντως ορισμένο στο production και αν υπάρχει alert rule για ERROR των
    `gemiapp.ingestion.client` / `gemiapp.services` **δεν επαληθεύεται από το repo** **[C]**. Κρίσιμο: από την 0054
    το `GemiClient._validate` ξαναρίχνει σε παραβίαση συμβολαίου, άρα αλλαγή schema του ΓΕΜΗ **σταματά τον ημερήσιο
    import και το digest**· και επειδή δεν ορίζεται `LOGGING`, οι INFO γραμμές του `gemiapp` δεν φτάνουν στα logs.
  - **`G6_STATUS = NOT_MEASURED`** (δεν είναι dark-deploy blocker: κανένα 2.0 job δεν είναι προγραμματισμένο).
    **`G4_STATUS = NOT_PASSED`**, LIVE **απαγορευμένη**, καμία merge, κανένα deploy, καμία production migration.

- **Gemi Leads 2.0 — ο ντετερμινιστικός κύκλος SHADOW του G4 (2026-09-20).** Μία εντολή operator, μία σειρά, μία
  ελέγξιμη αναφορά: `gemiapp/g4_shadow_cycle.py` + `python manage.py run_g4_shadow_cycle`.
  - **Σειρά φάσεων (η σειρά είναι το συμβόλαιο):** `precheck` → `discovery` → `materialisation` →
    `opportunity_pipeline` → `replay` → `comparison`.
    - **A. precheck** — πριν από **οποιαδήποτε** εγγραφή και οποιοδήποτε αίτημα ΓΕΜΗ, fail closed: ingest κλειστό
      (`GEMI_DISCOVERY_V2_ENABLED=0`), discovery shadow flag ανοιχτό, το pipeline δέχεται **μόνο** SHADOW, κανένα
      σήμα LIVE στη βάση, οι πίνακες 2.0 υπάρχουν (introspection), ο cursor αρχικοποιημένος, ο collector ΓΕΜΗ
      ρυθμισμένος. Τίποτα δεν είναι hard-coded: environment/vendor διαβάζονται από τη ρύθμιση και μόνο
      αναφέρονται. Cursor σε `anomaly` → **δεν** μπλοκάρει (το σύνορο είναι ήδη παγωμένο), αναφέρεται.
    - **B. discovery** — ένα shadow run A10. **Η μόνη φάση που μιλά στο ΓΕΜΗ** (1 αίτημα ανά σελίδα, lane
      DISCOVERY, κοινό rate budget).
    - **C. materialisation** — B2 από τα επιλέξιμα ευρήματα, με το baseline του ce0cd4f.
    - **D. opportunity_pipeline** — **παρατηρείται, δεν επαναλαμβάνεται.** Το `record_company_signal` δηλώνει το
      pipeline με `transaction.on_commit`, άρα κάθε νέο σήμα επεξεργάζεται **ήδη μία φορά** μέσα στη φάση C· ο
      hook όμως πετά το `PipelineRun`. Νέο `collect_pipeline_runs()` στο `opportunity_pipeline` (context manager,
      ContextVar) **μόνο** συλλέγει αυτά τα runs: τίποτα δεν αλλάζει σε ό,τι τρέχει. Έτσι ο orchestrator παίρνει
      τις μετρικές **χωρίς** δεύτερο πέρασμα στο ίδιο σήμα.
    - **E. replay** — **off εκτός αν ζητηθεί** (`--replay-hours N`, `--replay-limit`, 1–1000). Σε υγιή κύκλο δεν
      έχει τι να κάνει· υπάρχει για να ανακτά δουλειά που **προηγούμενος** κύκλος έχασε (αποτυχία pipeline, Radar
      που φτιάχτηκε αργότερα, entitlement που επανήλθε). Εξαιρεί ρητά τα σήματα που μόλις επεξεργάστηκε η φάση D.
    - **F. report** — μία αναφορά: DISCOVERY (status, σελίδες/αιτήματα, examined/known/new, εξ αυτών ήδη τοπικές,
      late/invalid, duplicates, anomalies, cursor πριν→μετά), MATERIALISATION (candidates, created, existing,
      pending_no_company, baselines created/reused/unavailable, conflicts), PIPELINE και REPLAY (processed, radars
      considered/entitled/matched/insufficient_state/no_match, opportunities created/updated/unchanged/
      below_threshold/skipped, errors), G4 COVERAGE και, με `--compare-date`, η **υπάρχουσα** σύγκριση legacy/v2.
  - **G4 COVERAGE:** επιλέξιμα ευρήματα, πόσα έχουν `Company`, πόσα σήματα υλοποιήθηκαν, **coverage ratio**,
    **τεκμήριο race-loss** (`classification != known AND company_existed = True`), εκκρεμή `pending_no_company` ανά
    κατάταξη (late_publication / invalid_date / new_incorporation) και **ηλικία του παλαιότερου** εκκρεμούς
    τεκμηρίου. Καμία migration, κανένα μοντέλο μετρικών: console/log μόνο.
  - **Late publications:** παραμένουν `pending_no_company` — **δεν** γίνονται ingest εδώ. Μετρούνται ρητά, ώστε το
    κενό να γίνει **τεκμήριο G4** αντί για σιωπή.
  - **Idempotency:** καμία δική του εγγύηση — στηρίζεται στις υπάρχουσες (dedupe key B1, επαναχρησιμοποίηση
    baseline ce0cd4f, κλειδί (organization, Radar, company) του C8 και «ήδη συνδεδεμένο σήμα → unchanged»). Δεύτερη
    εκτέλεση: κανένα δεύτερο σήμα/snapshot/opportunity, κανένα ξαναγράψιμο capture· το discovery καταγράφει
    κανονικά νέο run και παρατηρήσεις (είναι το log του, όχι διπλή δουλειά).
  - **Αποτυχίες:** μία αποτυχημένη φάση → **non-zero** (`CommandError` που ονομάζει τη φάση), αλλά **τίποτα δεν
    αναιρείται**: ένα σήμα που έχει ήδη γίνει commit μένει, το `replay` το ανακτά. Discovery `failed`/`anomaly` =
    αποτυχημένη φάση, η υλοποίηση συνεχίζει (διαβάζει ήδη αποθηκευμένα τεκμήρια).
  - **`--dry-run`:** αληθινό, όχι προσποίηση — καμία εγγραφή (ούτε run/observations/cursor, ούτε σήματα, snapshots,
    opportunities). Το discovery **κάνει** τα αιτήματά του και η αναφορά λέει ρητά ότι, επειδή δεν αποθηκεύτηκαν τα
    ευρήματα, η υλοποίηση είδε μόνο τεκμήρια προηγούμενων εκτελέσεων.
  - **Κόστος ΓΕΜΗ:** μόνο το discovery. Υλοποίηση, baseline, matching, scoring, persistence και replay: **μηδέν**
    αιτήματα. Κανένα refresh/reference sync δεν προστέθηκε στον κύκλο.
  - **Operator-run μόνο: καμία εγγραφή στο `apps.SCHEDULES`**, κανένα django-q schedule, καμία migration, καμία
    αλλαγή σε billing, Organizations, κατάταξη Discovery, CustomerRadar, leads, matches, digests.
  - **`G4_STATUS = NOT_PASSED`** — η 14ήμερη παρατήρηση **δεν** ξεκίνησε και η LIVE παραμένει **απαγορευμένη**.
    23 νέα tests (`test_g4_shadow_cycle.py`, `TransactionTestCase` ώστε το on_commit να τρέχει αληθινά)·
    **1.973 tests OK** με μία κανονική εκτέλεση.

- **Gemi Leads 2.0 — Discovery v2: η «καινούρια» εταιρεία κρίνεται από το σύνορο, όχι από τον Company (2026-09-20).**
  Άρση της εξάρτησης ανάμεσα στην κατάταξη του Discovery v2 και στη **στιγμή εγγραφής** του legacy importer.
  - **Το ελάττωμα:** η κατάταξη γινόταν αποκλειστικά με «υπάρχει τώρα γραμμή `Company`;» τη στιγμή της σάρωσης.
    Η ίδια εγγραφή ΓΕΜΗ καταγραφόταν άλλοτε `known` και άλλοτε ως νέο εύρημα, ανάλογα με το αν είχε ήδη τρέξει ο
    importer (7 φορές την ημέρα). Μια `known` παρατήρηση **δεν** είναι επιλέξιμο τεκμήριο B2 και το payload δεν
    κρατιέται πουθενά, άρα η πρώτη θέαση χανόταν οριστικά: ο παραγωγός NEW_COMPANY ουσιαστικά δεν έβλεπε τίποτα.
  - **Ο νέος κανόνας:** το σύνορο (high-water mark) διαβάζεται **μία φορά, στην αρχή του run**. Ανά εγγραφή:
    `αριθμός > σύνορο` → νέο εύρημα, με την υπάρχουσα κατάταξη ημερομηνίας (`new_incorporation` /
    `late_publication` / `invalid_date`), **ανεξάρτητα** από το αν υπάρχει ήδη `Company`· `αριθμός <= σύνορο` →
    η παλιά σημασιολογία τοπικής ύπαρξης (τοπική → `known`, αλλιώς νέο εύρημα — έτσι βρίσκονται ακόμη όσες ο
    legacy importer δεν αποθήκευσε ποτέ). Η σύγκριση είναι **ανά εγγραφή** με αυτή τη μία τιμή, ποτέ «κολλημένη»
    σημαία «περάσαμε το σύνορο», ώστε μια εκτός σειράς εγγραφή να κρίνεται από τον δικό της αριθμό.
  - **Γιατί είναι ασφαλές:** το bootstrap γράφει σύνορο τον υψηλότερο αριθμό που **υπάρχει τοπικά** και κάθε run
    το προχωρά μόνο στον υψηλότερο αριθμό που πραγματικά εξέτασε, μόνο σε επιτυχία. Άρα κάθε αριθμός ≤ συνόρου
    ήταν είτε τοπικός στο bootstrap είτε εξετασμένος σε επιτυχημένο run· τίποτα από πάνω του δεν μπορεί να είναι
    παλιά εδραιωμένη εταιρεία.
  - **`company_existed`:** καταγράφεται **ακριβώς όπως πριν** (τοπική ύπαρξη τη στιγμή της σάρωσης). Για εγγραφή
    πάνω από το σύνορο είναι πλέον **διαγνωστικό, όχι κατάταξη**: `classification != known AND company_existed =
    True` σημαίνει «το Discovery είδε πρώτη φορά αυτό το αναγνωριστικό αν και ο legacy importer είχε ήδη
    αποθηκεύσει την εταιρεία» — ακριβώς οι θεάσεις που ο παλιός κανόνας πετούσε. Μετριούνται ανά run ως
    `rediscovered_local_records` (υποσύνολο του `new_records`). Τίποτα δεν κρύβεται και δεν ξαναγράφεται.
  - **Ασφάλεια ingest:** ο writer του ingest **δεν γράφει ποτέ πάνω σε υπάρχουσα τοπική γραμμή** — την προσπερνά,
    ό,τι κι αν του δοθεί, ώστε το μελλοντικό cutover να μην μπορεί να αντικαταστήσει legacy δεδομένα με payload
    του discovery. Η παρατήρηση καταγράφεται κανονικά. (Το ingest παραμένει κλειστό: `GEMI_DISCOVERY_V2_ENABLED=0`.)
  - **Αμετάβλητα:** ordering guardrails και anomalies, κανόνες προώθησης cursor, overlap stop (μετρά **μόνο**
    τοπικές εγγραφές ≤ συνόρου), `highest_known_gemi_number` (τοπική ύπαρξη — το σύνορο bootstrap πρέπει να είναι
    πραγματικά αποθηκευμένος αριθμός), όρια αιτημάτων, `known_records` = παρατηρήσεις `known` (ισχύει ακόμη
    `examined = known + new`). **Καμία αλλαγή** στη σημασιολογία `detected_at` του ce0cd4f, στον C5, στο B2.
  - **Σύγκριση legacy/v2:** εταιρεία που ξαναβρέθηκε πάνω από το σύνορο ενώ ο legacy την είχε ήδη → **BOTH**,
    ποτέ V2_ONLY. Οι σημασίες V2_ONLY (late_publication / invalid_incorporation_date / legacy_filter_miss) και
    LEGACY_ONLY μένουν ίδιες.
  - **Late publications: δεν λύθηκαν εδώ.** Εταιρεία χωρίς τοπική γραμμή μένει `pending_no_company` (κανένα σήμα,
    καμία πλαστή εταιρεία) — **μετρούμενο κενό του G4**, όχι σφάλμα. Η λύση σημαίνει δημιουργία `Company` εκτός
    του legacy importer, δηλαδή την πύλη του cutover.
  - **Καμία migration, κανένα επιπλέον αίτημα ΓΕΜΗ** (η απόφαση παίρνεται από δεδομένα που ήδη υπάρχουν στη
    σελίδα), καμία αλλαγή σε schedules, billing, Organizations, CustomerRadar, leads, matches, digests.
  - **`G4_STATUS = NOT_PASSED`** — η LIVE παραμένει **απαγορευμένη**. 13 νέα tests
    (`test_discovery_frontier.py`)· **1.950 tests OK** με μία κανονική εκτέλεση.

- **Gemi Leads 2.0 — NEW_COMPANY: κατάσταση τη στιγμή της ανίχνευσης (2026-09-19, Option A).** Διόρθωση της
  δομικής αιτίας: μια καινούρια εταιρεία δεν έχει τοπική κατάσταση όταν τη βλέπει το Discovery v2, οπότε κάθε
  Radar με ΚΑΔ/περιοχή/νομική μορφή έβγαινε `INSUFFICIENT_STATE`.
  - **Δύο διαφορετικές έννοιες:** *χρόνος παρατήρησης discovery* = το πρώτο τεκμήριο (το `started_at` του run της
    παλαιότερης επιλέξιμης παρατήρησης· μένει στο observation/run και συνδέεται με το σήμα μέσω
    `CompanySignalDiscoveryEvidence`, ποτέ δεν ξαναγράφεται). *`detected_at` του σήματος* = η πρώτη στιγμή που το
    Gemi Leads είχε **και** το τεκμήριο discovery **και** κανονική κατάσταση επαρκή για ντετερμινιστική αξιολόγηση
    Radar: `detected_at = max(χρόνος discovery, observed_at του baseline snapshot)`. Απαραίτητο για το ιστορικό
    matching: ο C5 διαβάζει μόνο κατάσταση με `observed_at <= detected_at` (αμετάβλητος, κανένα «latest» fallback).
  - **Baseline:** αν η εταιρεία έχει ήδη snapshot → επαναχρησιμοποιείται το πρώτο (καμία εγγραφή)· αλλιώς
    δημιουργείται με τον υπάρχοντα B3 writer (`normalize_company` → `record_company_snapshot`, ίδια schema/normalizer
    versions, state hash, ποιότητες) από το `Company.raw_data` του importer **μόνο** αν: περνά το A2 contract
    `company_search`, το `arGemi` είναι της ίδιας εταιρείας, δεν υπάρχει καταγεγραμμένη αλλαγή μέσω Django admin,
    και το A6 δίνει χρόνο παρατήρησης (`updated_at`, όπως το A6 `last_seen_at`). Σειρά σε **μία** συναλλαγή:
    baseline → `detected_at` → σήμα → discovery evidence· μετά το commit τρέχει το SHADOW pipeline.
  - **Χωρίς αξιόπιστη εγγραφή** (λείπει, κακοσχηματισμένη, άλλης εταιρείας, αποτυγχάνει το contract, admin-edited,
    αποτυχία normalisation): **καμία κατάσταση**, τίποτα δεν μαντεύεται· `detected_at` = χρόνος discovery (όπως
    πριν) και τα Radars με κριτήρια μένουν `INSUFFICIENT_STATE`.
  - **Idempotent:** υπάρχον σήμα → καμία κατάσταση/εγγραφή· επανεκτέλεση → κανένα διπλό snapshot/σήμα, κανένα
    ξαναγράψιμο capture. Το report μετρά baselines created / reused / unavailable. **Κανένα αίτημα ΓΕΜΗ**, καμία
    migration. Company/CompanyActivity μόνο διαβάζονται· legacy importer, CustomerRadar, leads, matches, digests,
    billing αμετάβλητα.
  - **Όρια:** πεδία που η εγγραφή δεν δίνει (π.χ. `null` δήμος/νομική μορφή, δραστηριότητες αβέβαιης ισχύος)
    μένουν άγνωστα → `INSUFFICIENT_STATE` για το αντίστοιχο κριτήριο. Η πρώτη επόμενη παρατήρηση του refresh
    συγκρίνεται με αυτό το baseline (μέτρηση στο G4).
  - **`G4_STATUS = NOT_PASSED`** — η LIVE παραμένει **απαγορευμένη**. 15 νέα tests (`test_new_company_state.py`)·
    **1.937 tests OK** με μία κανονική εκτέλεση.

- **Gemi Leads 2.0 — G2: SHADOW opportunity pipeline end-to-end (2026-09-19).** Η αλυσίδα
  `CompanySignal(SHADOW) → ενεργά OrganizationRadars (C5 pre-filter + v1 evaluation) → μόνο entitled οργανισμοί →
  C6 score + C7 breakdown → C8 materialize_opportunity` τρέχει πλέον από ένα κανονικό entry point:
  `gemiapp.opportunity_pipeline.process_company_signal(signal, as_of=None, dry_run=False)` → `PipelineRun`.
  Καμία επανυλοποίηση: μόνο οι υπάρχουσες C5–C8 συναρτήσεις (+ καθαρή εξαγωγή
  `explain_opportunity_scores_for_report`, ώστε να γίνεται ένα matching pass).
  - **Μόνο SHADOW:** LIVE signal → `skipped=live_not_enabled`. Κανένα promote/αλλαγή mode. **Οι customer επιφάνειες
    μένουν LIVE-only** (D29, λίστες, Saved, dashboard counts, ειδοποιήσεις)· ένα SHADOW-backed opportunity δεν
    φαίνεται πουθενά. Ένα SHADOW signal **δεν ξαναγράφει ποτέ** opportunity του οποίου το capture είναι LIVE-backed
    (`skipped_live_backed`) — ώστε να μην κρυφτεί ορατή ευκαιρία· το C8 αμετάβλητο.
  - **Entitlement:** μόνο Radars οργανισμών που είναι entitled τώρα· οι υπόλοιποι `skipped_not_entitled`, ούτε score
    ούτε εγγραφή. Ιστορικά opportunities δεν διαγράφονται όταν λήγει το entitlement. Ανενεργό Radar → τίποτα (C5).
  - **Idempotent:** μοναδικότητα C8 (organization, Radar, company)· signal ήδη συνδεδεμένο με opportunity →
    `unchanged` χωρίς recapture (ένα replay δεν «γερνά» το παγωμένο capture). Το replay κάνει μόνο ό,τι λείπει.
  - **Wiring (απόφαση):** το `record_company_signal`, όταν **δημιουργεί** signal, καταχωρίζει
    `transaction.on_commit(...)` → `process_signal_after_commit` — **σύγχρονα, μετά το commit** του signal και της
    evidence, στην ίδια διεργασία με τον (operator-run, batch) producer. Όχι django-q task: οι producers είναι
    χειροκίνητες εντολές (κανένα request path/schedule) και ένα task ανά signal θα γέμιζε την ORM ουρά
    (`queue_limit` 50) σε backfill και θα απαιτούσε worker. Αποτυχία pipeline → log + `errors`, ποτέ exception
    προς τον producer, το signal μένει ανέγγιχτο· rolled-back producer → τίποτα· replayed/υπάρχον signal → τίποτα.
    Κανόνες ανίχνευσης και dedupe αμετάβλητοι.
  - **Παρατηρησιμότητα (χωρίς schema):** μία γραμμή log ανά run (`gemiapp.opportunity_pipeline`): signal, mode,
    context, considered, entitled, skipped_not_entitled, matched, insufficient_state, no_match, created, updated,
    unchanged, below_threshold, skipped_live_backed, errors.
  - **Replay (operator):** `python manage.py process_shadow_signals [--since-hours 24] [--limit 100] [--after-id N]
    [--dry-run] [--verbose-runs]` — μόνο SHADOW, σειρά id, όριο 1–1000, ένα `as_of` ανά run, σύνοψη, ποτέ LIVE.
  - Κανένα notification/email/digest/outreach/assignment/task/audit event από το pipeline. Legacy (CustomerRadar,
    UserCompanyLead, RadarMatch, DigestPreference/Delivery) και billing (UserSubscription, Stripe) ανέγγιχτα.
  - Στοιχεία αναφοράς: χωρίς snapshot/reference, Radars με κριτήρια → `INSUFFICIENT_STATE` (μετριούνται)· Radars μόνο
    με τύπους γεγονότων δουλεύουν. Στο αντίγραφο της dev βάσης: 0 signals (discovery/refresh δεν έχουν τρέξει).
  - **`G4_STATUS = NOT_PASSED`** — η LIVE cutover παραμένει **απαγορευμένη**. 23 νέα tests
    (`test_shadow_pipeline.py`)· **1.922 tests OK** με μία κανονική εκτέλεση· **καμία migration**.
    `G0_STATUS = BLOCKED_NO_STAGING`, `G1_STATUS = BLOCKED`, **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`**.

- **Gemi Leads 2.0 — Organization Radar: δημιουργία / επεξεργασία από τον πελάτη (2026-09-19).** Το
  `OrganizationRadar` είναι πλέον customer-creatable/editable από τη σελίδα Radars του οργανισμού.
  - **Routes** (`organization_views`, πόρτα μόνο το `organization_access`): `/organizations/<id>/radars/new/`
    (`organization_radar_create`, GET/POST), `/organizations/<id>/radars/<radar_id>/edit/` (`organization_radar_edit`,
    GET/POST), `/organizations/<id>/radars/<radar_id>/active/` (`organization_radar_active`, POST `active=1|0`).
  - **Ρόλοι:** μόνο όσοι έχουν `manage_radars` στον πίνακα §64 — **OWNER και ADMIN**. Ο SALES_MANAGER, ο SALES_USER
    και ο VIEWER **δεν** αλλάζουν Radars (ίδιο 404)· οι ρόλοι με `view_radars` βλέπουν τη λίστα μόνο για ανάγνωση.
  - **Entitlement:** κάθε ενέργεια περνά από το compatibility layer (`0a1f094`)· μη entitled οργανισμός → legacy
    paywall, τίποτα δεν γράφεται. Ξένος οργανισμός/Radar → ίδιο 404.
  - **Πεδία = μόνο ό,τι υποστηρίζει ο matcher (C3/C5):** όνομα, ενεργό, ελάχιστο score (0–100, προαιρετικό), ΚΑΔ
    (ακριβής κωδικός + έκδοση· αν ο κωδικός υπάρχει σε 2008 και 2026 ζητείται ρητή έκδοση), περιφερειακές ενότητες,
    δήμοι, νομικές μορφές, οι υλοποιημένοι τύποι γεγονότων, εξαιρέσεις σε ΚΑΔ/ενότητα/δήμο/νομική μορφή.
  - **Validation:** `gemiapp/organization_radar_form.py` (μόνο πλατφορμικά στοιχεία αναφοράς ΓΕΜΗ, καμία εγγραφή)
    αντιστοιχίζει κάθε αναφορά σε **παρούσα** γραμμή και καλεί τον ίδιο κανόνα `validate_radar_definition` του C3·
    ο domain service ξαναελέγχει κατά την εγγραφή (`create_organization_radar` / `replace_organization_radar` /
    `set_organization_radar_active`, όλα-ή-τίποτα). Απορρίπτονται με ελληνικό μήνυμα: άγνωστοι/αποσυρμένοι/
    κακοσχηματισμένοι ΚΑΔ και αναφορές, διπλά κριτήρια, ίδιο κριτήριο ως στόχος και εξαίρεση, score εκτός 0–100,
    μη υλοποιημένοι/άγνωστοι τύποι γεγονότων, ενεργό Radar χωρίς θετικό κριτήριο, κενό/μακρύ όνομα.
  - **G5:** `get_authorized_radar_editor`, `create_/replace_authorized_organization_radar`,
    `set_authorized_organization_radar_active` (επανέλεγχος της δρώσας membership υπό κλείδωμα, `RadarRefused`). Η
    λίστα Radars δείχνει αναγνώσιμη περίληψη κριτηρίων, Ενεργό/Ανενεργό και — για owner/admin — «Νέο Radar»,
    επεξεργασία, ενεργοποίηση/απενεργοποίηση.
  - **Στοιχεία αναφοράς ΓΕΜΗ:** στο αντίγραφο της dev βάσης (και μέχρι να τρέξει το reference sync) οι πίνακες
    ΚΑΔ/περιοχών/νομικών μορφών είναι κενοί → η φόρμα προσφέρει μόνο τύπους γεγονότων, με ρητό μήνυμα.
  - **Το legacy `CustomerRadar` μένει ξεχωριστό** (`/radars/`)· καμία μετατροπή. **Η ενεργοποίηση pipeline ΔΕΝ έχει
    γίνει:** ένα ενεργό Radar είναι μόνο ρύθμιση — κανένα LIVE signal, καμία δημιουργία ευκαιριών, καμία
    ειδοποίηση/audit event/schedule από αποθήκευση ή (απ)ενεργοποίηση.
  - Guards που ενημερώθηκαν σκόπιμα: routes και imports του `organization_views` (το form module επιτρέπεται και
    ελέγχεται ότι δεν αγγίζει δεδομένα tenant), C3 guard για τα routes Radars στο `urls.py`.
  - 16 νέα tests (`test_organization_radar_ui.py`)· **1.899 tests OK** με μία κανονική εκτέλεση (`--parallel 4`)·
    **καμία migration**. `G0_STATUS = BLOCKED_NO_STAGING`, `G1_STATUS = BLOCKED`, **`PRODUCTION_MIGRATION_STATUS =
    BLOCKED_BY_G0_G1`**.

- **Gemi Leads 2.0 — Compatibility layer: UserSubscription → Organization (2026-09-19).** Κανόνας συμβατότητας μέχρι
  να αποφασιστεί η ιδιοκτησία του billing.
  - **Το billing μένει user-owned:** Stripe checkout, webhooks και σχήμα `UserSubscription` **αμετάβλητα**· καμία
    μεταφορά συνδρομών σε Organization ακόμη.
  - **Entitlement οργανισμού = παράγωγο, read-only** (`gemiapp/organization_entitlement.py`): Organization → η
    **μοναδική** owner `OrganizationMember` → User → `UserSubscription.has_entitlement` (ενεργό πληρωμένο πλάνο
    pro/business/enterprise/custom ή έγκυρη complimentary πρόσβαση — ο ίδιος κανόνας με κάθε πληρωμένη legacy
    λειτουργία, `radar_limit > 0`· SQL δίδυμο `entitlement_q`). Καμία νέα τιμή/πλάνο/όριο.
    `resolve_organization_entitlement(org)` (με λόγο) και `entitled_organizations()` (queryset).
  - **Fail closed:** χωρίς owner, με >1 owners, ανενεργός λογαριασμός owner, χωρίς γραμμή συνδρομής ή free/inactive/
    expired πλάνο → όχι entitled. Η συνδρομή άλλου μέλους **δεν μετράει ποτέ**· staff/superuser καμία παράκαμψη.
  - **Κανόνας πρόσβασης 2.0:** membership **και** entitlement. Ελέγχεται στο `get_organization_access_context` μέσα
    στο ίδιο ένα membership query (κάθε customer entry point περνά από εκεί). Μέλος μη entitled οργανισμού →
    `OrganizationNotEntitled` (υποκλάση της ενιαίας άρνησης, ίδιο μήνυμα) → στα views το legacy paywall: μήνυμα
    «Απαιτείται ενεργή συνδρομή του ιδιοκτήτη του οργανισμού.» + redirect στο `pricing`, χωρίς δεδομένα tenant.
    Μη μέλη → το ίδιο 404 όπως πριν. Η πλοήγηση δείχνει μόνο entitled οργανισμούς. Cross-tenant απομόνωση αμετάβλητη.
  - **Provisioning: μόνο χειροκίνητο/operator-run:** `python manage.py provision_organization_for_user <id | username |
    email> [--name "…"] [--dry-run]`. Μέσω `create_organization` (Organization + OrganizationProfile + owner
    membership), idempotent (υπάρχων μοναδικός owned οργανισμός → «unchanged»), `select_for_update` στον χρήστη.
    Αρνείται: άγνωστο/αμφίσημο αναγνωριστικό, ανενεργό λογαριασμό, >1 owned οργανισμούς, μέλος άλλου οργανισμού
    χωρίς να είναι owner. Αναφέρει το entitlement, **δεν** το χορηγεί. Δεν αγγίζει συνδρομή/Stripe, legacy Radars,
    leads, matches· δεν δημιουργεί OrganizationRadar/Opportunity.
  - **Καμία αυτόματη δημιουργία οργανισμού** σε signup, login, migration ή επίσκεψη σελίδας.
  - Στο αντίγραφο της dev βάσης ο υπάρχων «TEST ORGANIZATION» (owner: staff χρήστης #2, χωρίς συνδρομή) είναι πλέον
    **μη entitled** → οι σελίδες του δείχνουν paywall μέχρι να αποκτήσει ο owner entitlement (π.χ. complimentary).
  - Fixtures tests: οι owners των οργανισμών είναι πλέον πληρωμένοι (`test_organization_radars.entitle`).
  - 28 νέα tests (`test_organization_entitlement.py`)· **1.883 tests OK** με μία κανονική εκτέλεση (`--parallel 4`)·
    καμία migration. `G0_STATUS = BLOCKED_NO_STAGING`, `G1_STATUS = BLOCKED`, **`PRODUCTION_MIGRATION_STATUS =
    BLOCKED_BY_G0_G1`**.

- **Gemi Leads 2.0 — Customer workspace: dashboard & πλοήγηση (2026-09-19).** Οι ήδη υλοποιημένες λειτουργίες
  οργανισμού γίνονται προσβάσιμες από το κανονικό UI, χωρίς αλλαγή backend αρχιτεκτονικής, billing, scoring, signals,
  δημιουργίας ευκαιριών, tenant isolation ή migrations.
  - **Νέα routes (GET μόνο, `organization_views`, πόρτα μόνο το `organization_access`, κάθε άρνηση ίδιο 404):**
    `/organizations/<id>/` (`organization_dashboard`), `/organizations/<id>/opportunities/`
    (`organization_opportunities`, `?status=` active/κάθε §39/all — «Αποθηκευμένες» = `?status=saved` —,
    `?radar=`, `?cursor=`), `/organizations/<id>/tasks/` (`organization_tasks`), `/organizations/<id>/radars/`
    (`organization_radars`, μόνο ρόλοι με `view_radars` — ο SALES_USER παίρνει 404).
  - **G5 read models (`organization_access`, ενότητα «Customer workspace»):** `get_workspace_navigation`,
    `get_authorized_workspace_dashboard`, `get_authorized_workspace_opportunities`, `get_authorized_workspace_tasks`,
    `get_authorized_workspace_radars`. Ίδιο membership + capability, ίδιο SQL scope (ο SALES_USER μόνο τις δικές του
    αναθέσεις) και, όπως στο D29, **μόνο LIVE-backed ευκαιρίες**. Μετρήσεις = εταιρείες (μία κάρτα C9 ανά εταιρεία).
    Εργασίες = ανοιχτές εργασίες ορατών ευκαιριών (κανόνας ανάγνωσης D34). Όλα read-only, bounded queries.
  - **C9:** νέο προαιρετικό `latest_signal_mode` στο `get_opportunity_feed` (ίδιο SQL, πριν τη συσσώρευση, άρα το
    primary επιλέγεται μόνο ανάμεσα σε LIVE γραμμές). Χωρίς αυτό η συμπεριφορά είναι αμετάβλητη.
  - **Πλοήγηση:** context processor `gemiapp.organization_views.workspace_navigation` (lazy· μηδέν queries για
    ανώνυμους, ένα query μελών για συνδεδεμένους). Ομάδα «ΟΡΓΑΝΙΣΜΟΣ» στο rail (Πίνακας, Ευκαιρίες, Αποθηκευμένες,
    Εργασίες, Radars όπου επιτρέπεται, Ειδοποιήσεις), «Οργανισμός» στο mobile nav, και μπάρα οργανισμού (όνομα +
    ρόλος + tabs) σε κάθε σελίδα οργανισμού, συμπεριλαμβανομένων D29 και ειδοποιήσεων. Με πολλά memberships και
    χωρίς οργανισμό στο route: λίστα οργανισμών, ποτέ «μαντεψιά». **Χρήστης χωρίς membership: καμία αλλαγή**.
    Staff/superuser: καμία παράκαμψη. Ενεργή ενότητα με την εγκεκριμένη υπογραφή (inset amber rule).
  - Το legacy «Radars» του rail δεν επισημαίνεται πλέον στη σελίδα Radars οργανισμού· το «← Πίσω» του D29 οδηγεί
    στη λίστα ευκαιριών του οργανισμού. Το legacy `/dashboard/` (Signals) μένει η αρχική μετά το login.
  - Guards που ενημερώθηκαν σκόπιμα: λίστα organization routes και imports του `organization_views` (D29), routes
    εργασιών (D34), C3 guard για το `urls.py` (μόνο το route Radars του workspace), D37 guard για το nav.
  - Οπτικός έλεγχος (desktop + mobile, owner + sales user) με προσωρινό fixture `WSFIX` στο αντίγραφο της dev βάσης,
    που αφαιρέθηκε πλήρως· ο υπάρχων «TEST ORGANIZATION» δεν αγγίχτηκε.
  - 31 νέα tests (`test_customer_workspace.py`)· **1.855 tests OK** με μία κανονική εκτέλεση (`--parallel 4`).
    `G0_STATUS = BLOCKED_NO_STAGING`, `G1_STATUS = BLOCKED`, **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`**.

- **Gemi Leads 2.0 — Release readiness G0/G1 (2026-09-19).** Πλήρης καταγραφή: `docs/RELEASE_READINESS.md`.
  - **`G0_STATUS = BLOCKED_NO_STAGING`**: δεν υπάρχει staging service, staging βάση ή staging GEMI key. Ένα τοπικό
    αντίγραφο της dev βάσης **δεν** είναι staging. **`G1_STATUS = BLOCKED`** (εξαρτάται από το G0).
  - **Migration range:** `main`/`origin/main` τελειώνουν στην 0031· η production **συνάγεται** στην 0031 (Render
    `preDeployCommand: python manage.py migrate`) αλλά **δεν** έχει επαληθευτεί. 0032–0054 υπάρχουν μόνο σε αυτό το
    branch. **Κίνδυνος:** merge + deploy = αυτόματη εφαρμογή 0032–0054 σε production. Καμία merge πριν τα G0/G1.
  - **Staging guard (`config/environment.py`):** `GEMI_LEADS_ENVIRONMENT` = `production` (default όταν λείπει — καμία
    αλλαγή συμπεριφοράς) / `staging` / `development`· άλλη τιμή → δεν ξεκινά. Σε staging: GEMI key **μόνο** από
    `GEMI_STAGING_API_KEY` (το `GEMI_API_KEY` αγνοείται)· χωρίς αυτό `GEMI_COLLECTOR_ENABLED = False` και το
    `GemiClient.get` αρνείται κάθε αίτημα πριν σταλεί· email = `STAGING_EMAIL_BACKEND` ή console· Brevo key μόνο από
    `STAGING_BREVO_API_KEY`· outreach πάντα off· live Stripe key (`sk_live_`/`rk_live_`) → δεν ξεκινά.
  - **D37 schedule lifecycle:** η καταχώριση στο `post_migrate` είναι schema-aware (`requires_schema` →
    `gemiapp.notifications.notification_schema_ready`, introspection): **ακριβώς μία** γραμμή όσο υπάρχει ο πίνακας
    ειδοποιήσεων, **καμία** όταν δεν υπάρχει (rollback κάτω από 0054 → αφαιρείται αυτόματα). Τα τρία legacy schedules
    καταχωρίζονται ακριβώς όπως πριν. Επιπλέον το ίδιο το task ελέγχει τον πίνακα και επιστρέφει
    `{"skipped": "notification_schema_missing"}` (defence in depth). Γύρω από rollback σταματά ο worker.
    Το `apps.py` δεν αναφέρει organizations (G5 guard) — γι' αυτό το entry δείχνει στο `gemiapp.notifications`.
  - **Τοπική PostgreSQL πρόβα (όχι G0/G1):** throwaway PostgreSQL 17 στο 127.0.0.1, φορτωμένο από το dev αντίγραφο.
    Backup/restore σε απομονωμένη βάση, reverse 0054 → 0031, forward 0031 → 0054: όλα exit 0, legacy aggregates
    (counts + md5) ίδια σε κάθε βήμα. Cluster, dump και backup καταστράφηκαν.
  - **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.** Επόμενο βήμα: απομονωμένο staging (Render service +
    δική του PostgreSQL, `GEMI_LEADS_ENVIRONMENT=staging`, κανένα production secret).
  - *(Αναθεώρηση 2026-09-20: το staging υπάρχει ως ξεχωριστή Supabase PostgreSQL με τοπικές διεργασίες εφαρμογής·
    το κριτήριο «ξεχωριστό Render web+worker service» αφαιρέθηκε ως μη προβλεπόμενο από το `AGENTS.md`.
    `G0_STATUS = PASSED_WITH_DOCUMENTED_SCOPE`· `G1_STATUS = PASSED` μετά την τελική πρόβα parity της ίδιας ημέρας.
    Βλ. «G1_STATUS = PASSED (τελική πρόβα parity στο staging)».)*

- **Gemi Leads 2.0 — Phase D ΟΛΟΚΛΗΡΩΘΗΚΕ (`PHASE_D_STATUS = COMPLETE`, 2026-09-19).** Τα βήματα 29–37 είναι
  δεσμευμένα/επαληθευμένα. Αυτό **δεν** σημαίνει production-ready: `G0`/`G1` μπλοκάρουν τις production migrations
  (0032–0054), `G4_STATUS = NOT_PASSED`, `G5_STATUS = PASSED_FOR_CURRENT_ORGANIZATION_SURFACE`. **Η επόμενη δουλειά
  είναι release readiness (πύλες), όχι νέο feature πακέτο.**
- **Gemi Leads 2.0 — D37: In-app Notifications (2026-09-19).** §48 «notifications», «Unread counter». Migration
  `0054_organization_notification` (ένα `CreateModel`, **κανένα backfill**), μοντέλο `OrganizationNotification`,
  generator `gemiapp/notifications.py`, G5 read/mark services, σελίδα `organizations/notifications.html`.
  - Το D36 δεσμεύτηκε (`e344f0d`).
  - **Τύποι:** και οι πέντε του §48 είναι έγκυρες τιμές (NEW_OPPORTUNITY, PRIORITY_SIGNAL, RADAR_MATCH, TASK_DUE,
    ASSIGNMENT)· **emitters μόνο για ASSIGNMENT και TASK_DUE.** Οι τρεις τύποι pipeline είναι **reserved** — emitters
    αναβάλλονται μέχρι να περάσουν οι σχετικές πύλες (C8/Signals rollout, G0/G1, G4) και να εγκριθούν παραλήπτες.
  - **Σχήμα (typed, όχι JSON/κείμενο):** `organization` (CASCADE), `recipient` → `OrganizationMember` (**CASCADE**:
    διαγραφή μέλους → διαγράφονται οι ειδοποιήσεις του· επανένταξη = καθαρή αρχή), `notification_type`, `opportunity`
    (CASCADE), `task` (CASCADE), `source_audit_event` (SET_NULL), `due_on`, `created_at`, `read_at`. DB CHECKs για
    τύπο και σχήμα (TASK_DUE χωρίς task/due_on/opportunity, ASSIGNMENT χωρίς opportunity: αδύνατα)· partial unique:
    ένα TASK_DUE ανά (recipient, task, due_on), ένα ASSIGNMENT ανά (recipient, audit event).
  - **ASSIGNMENT:** μόνο σε πραγματική ανάθεση/επανανάθεση του D31, **μόνο στον νέο** `Opportunity.assigned_to`, ποτέ
    στον προηγούμενο, σε διευθυντές ή στον οργανισμό· καμία αν ο δρων αναθέτει στον εαυτό του. Στην ίδια συναλλαγή με
    την ανάθεση και το D36 event (αποτυχία → rollback όλων)· ο assignee κλειδώνεται μαζί με τον δρώντα.
  - **TASK_DUE:** django-q job `gemiapp.tasks.generate_task_due_notifications_task`, **καθημερινά 08:00
    Europe/Athens** (`cron 0 8 * * *` στο `gemiapp/apps.py` — η **μόνη** εγκεκριμένη εξαίρεση στο πάγωμα των
    schedules· Render άθικτο· **όχι ενεργό σε production** πριν τα G0/G1). Επιλέγει ανοιχτές εργασίες με assignee και
    `due_on <= timezone.localdate()` (due today και overdue ίδια ειδοποίηση, catch-up σε χαμένη μέρα), μόνο αν ο
    assignee βλέπει ακόμη την ευκαιρία κατά G5 (ενεργός, ίδιος οργανισμός, LIVE, ρόλος με πλήρη ορατότητα ή ο
    ανατεθειμένος πωλητής). Καμία εφεδρεία παραλήπτη. Set-based (batch 500, `ignore_conflicts`)· rerun → 0 νέες.
    Ολοκληρωμένη εργασία: καμία· μετά την ολοκλήρωση η υπάρχουσα μένει. Τελικές ευκαιρίες: ναι, αν η εργασία είναι
    ανοιχτή. **Καμία δημιουργία σε GET.**
  - **Ανάγνωση:** μόνο `(user, organization) → ακριβής membership → δικές της`, στο SQL. `GET
    /organizations/<id>/notifications/` (νεότερες πρώτα, έως 50), `POST .../notifications/<id>/read/`, `POST
    .../notifications/read-all/`· `read_at`, idempotent χωρίς εγγραφή όταν ήδη αναγνωσμένα· **κανένα delete,
    archive, retention** και κανένα audit event για ανάγνωση. Ευκαιρία που δεν είναι πλέον ορατή → η ειδοποίηση
    εμφανίζεται χωρίς στοιχεία/σύνδεσμο. Σύνδεσμος + unread count μόνο στη σελίδα D29 (όχι στο legacy nav).
  - **Κανένα email, digest (§46), real-time alert (§47), SMS, push, webhook.** Το `send_digests` άθικτο. Καμία
    ειδοποίηση για Save, status, σημείωση, εργασία, DNC.
  - Κύκλος migration στο αντίγραφο της dev βάσης: forward → 0 → fixture → parity → reverse → reapply, όλα PASS· το
    `migrate` καταχωρίζει (όπως κάθε `SCHEDULES` entry) τη γραμμή του D37 στο τοπικό `django_q_schedule`.
  - 29 νέα tests· **1.811 tests OK** με μία κανονική εκτέλεση (`--parallel 4`). **`PRODUCTION_MIGRATION_STATUS =
    BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — D36: Audit Log (2026-09-19).** Αμετάβλητο ιστορικό CRM ενεργειών (§52: actor, organization,
  action, entity, timestamp, metadata). Migration `0053_organization_audit_event` (ένα `CreateModel`, **κανένα
  backfill**), μοντέλο `OrganizationAuditEvent`, writer `organization_access._audit`, read model
  `get_authorized_company_audit_events`.
  - Το D35 δεσμεύτηκε (`30a7b66`), με το hardening: νέες ευκαιρίες εταιρείας που είναι ήδη suppressed **γεννιούνται
    `DO_NOT_CONTACT`** (C8, μέσω του authorization-free `gemiapp/contact_suppressions.py`).
  - **Σχήμα (typed, όχι JSON):** `organization` (CASCADE), `actor` → `OrganizationMember`, `action` (9 τιμές, DB
    CHECK), entity: `company`, `opportunity`, `note`, `task`, `suppression`· metadata: `previous_status`,
    `new_status`, `previous_assignee`/`new_assignee` (memberships), `reason`· `created_at`. Όλες οι αναφορές
    **SET_NULL** (το ιστορικό επιβιώνει)· κανένα κείμενο σημείωσης/τίτλος εργασίας, καμία επαφή/πρόσωπο/payload.
    **Append-only:** `save()` σε υπάρχουσα εγγραφή και `delete()` σηκώνουν σφάλμα· admin χωρίς add/change/delete.
  - **Ενέργειες:** opportunity_saved (D30), opportunity_assigned / opportunity_reassigned (D31),
    opportunity_status_changed (D32), note_added (D33), task_created / task_completed (D34), suppression_added και
    suppression_reapplied (D35· **ένα** company-level event χωρίς όνομα ευκαιρίας, ποτέ ένα ανά γραμμή).
  - **Μόνο πραγματικές αλλαγές:** ίδια συναλλαγή με τη μετάλλαξη (αν αποτύχει η εγγραφή, γίνεται rollback και της
    αλλαγής)· κανένα event για no-op (ήδη SAVED, ίδιος υπεύθυνος, ίδιο status, ήδη ολοκληρωμένη εργασία, ήδη
    suppressed) ή για άρνηση/refusal. Τα D30–D32 αποκτούν τον ίδιο επανέλεγχο της δρώσας membership υπό κλείδωμα με τα
    D33–D35 και τον κοινό `_mutation()` (FK σφάλμα στο commit → 404, ποτέ 500).
  - **Ορατότητα (read model):** ίδια με το D29· τα events των ευκαιριών που βλέπει η membership (LIVE) και τα
    company-level Do Not Contact events. Ο SALES_USER **δεν** βλέπει events αδελφών ευκαιριών. Νεότερα πρώτα
    (`-created_at, -id`), έως **50**, ένα bounded query. **Κανένα UI στο D29** (ο §52 δεν ορίζει οθόνη)· δεν είναι το
    B6 timeline. Καμία ειδοποίηση (**βήμα 37**).
  - Κύκλος migration στο αντίγραφο της dev βάσης: forward → 0 → fixture (όλες οι ενέργειες, no-ops, read model,
    διαγραφή actor) → parity → reverse → reapply (ξανά 0), όλα PASS.
  - 17 νέα tests· **1.782 tests OK** με μία κανονική εκτέλεση (`--parallel 4`). `G5_STATUS =
    PASSED_FOR_CURRENT_ORGANIZATION_SURFACE`· `G4_STATUS = NOT_PASSED`· **`PRODUCTION_MIGRATION_STATUS =
    BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — D35: Do Not Contact (2026-09-19).** Company-level operational suppression ανά οργανισμό
  (§51 «contact_suppressions», «Πρέπει να υπερισχύει οποιουδήποτε AI/Radar»). Migration
  `0052_organization_contact_suppression` (ένα `CreateModel`, κανένα backfill), μοντέλο
  `OrganizationContactSuppression`, `organization_access.apply_authorized_company_do_not_contact`, view
  `organization_views.company_do_not_contact`.
  - Το D34 (`a3ae6a3`) και η επιτάχυνση του suite (`922b49d`, κανονική εντολή `manage.py test --noinput --parallel 4`,
    ~40 s) δεσμεύτηκαν. Οι αποφάσεις D35 είναι του χρήστη (v1).
  - **Ταυτότητα:** `(organization, contact_type, contact_value)` unique· μόνο `contact_type = "company"` με
    `contact_value` = κανονικοποιημένος αριθμός ΓΕΜΗ (ο ίδιος κανόνας με το `ingestion.discovery.normalize_gemi_number`)·
    **ρητή, προσωρινή επέκταση** του χώρου ταυτοτήτων του §51· email/phone contact points **αναβάλλονται στο Phase G**.
    Χωρίς έγκυρο ΓΕΜΗ → άρνηση. Πεδία: `reason` (υποχρεωτικό· για εταιρεία μόνο explicit_objection, call_objection,
    compliance_registry, manual — **ποτέ** email_unsubscribe), `source` (πάντα `manual`), `created_by` →
    `OrganizationMember` (SET_NULL), `created_at`. DB CHECKs για τύπο/λόγο/πηγή/μη κενή τιμή. Κανένα όνομα, επαφή,
    πρόσωπο ή payload.
  - **Οργανισμός:** επηρεάζει μόνο τον ίδιο οργανισμό· η ίδια εταιρεία σε άλλο οργανισμό μένει ανέγγιχτη.
  - **Δικαίωμα `manage_contact_suppressions`:** μόνο OWNER και SALES_MANAGER (ποτέ SALES_USER, αφού αλλάζει και
    κρυφές αδελφές γραμμές)· ADMIN/VIEWER όχι. Είσοδος μόνο αν η εταιρεία έχει ορατή LIVE ευκαιρία· αλλιώς ίδιο 404.
  - **Επίδραση:** μία εγγραφή suppression και **κάθε** ευκαιρία του οργανισμού για την εταιρεία (όλα τα Radars,
    και κρυφές SHADOW γραμμές, χωρίς να αποκαλύπτονται) → `DO_NOT_CONTACT` (μία bulk UPDATE). Το D32 συνεχίζει να
    αρνείται τον στόχο `do_not_contact`· μόνο το D35 τον δημιουργεί. Ανάθεση, σημειώσεις, εργασίες (οι ανοιχτές μένουν
    ανοιχτές), score, signals, snapshots, κατάταξη feed: αμετάβλητα· το φίλτρο status του C9 βρίσκει τις γραμμές.
  - **Idempotent:** η υπάρχουσα εγγραφή κερδίζει (λόγος/δημιουργός/χρόνος δεν ξαναγράφονται), καμία περιττή UPDATE·
    ταυτόχρονο ίδιο insert → unique constraint → σύγκλιση στην υπάρχουσα, ποτέ 500. Κλειδώματα: ευκαιρίες της
    εταιρείας (αύξουσα σειρά) → δρώσα membership (επανέλεγχος) → insert. **Κανένα unsuppress/edit/expiry.**
  - **Enforcement:** `is_contact_suppressed(organization, contact_type, contact_value)` και
    `is_company_suppressed(organization, company)` ζουν στο authorization-free `gemiapp/contact_suppressions.py` (μία
    υλοποίηση· το G5 τα επανεξάγει) — κάθε μελλοντική ροή επικοινωνίας (email/τηλέφωνο/AI) πρέπει να τα ρωτά πρώτα.
    Το D35 δεν υλοποιεί καμία ροή επικοινωνίας.
  - **Μελλοντικές ευκαιρίες (hardening):** το C8 (`_locked_or_created`), **μόνο όταν δημιουργεί νέα γραμμή**,
    κλειδώνει τη γραμμή της εταιρείας (`FOR NO KEY UPDATE`) και ρωτά το `is_company_suppressed`· αν υπάρχει suppression
    για τον οργανισμό, η ευκαιρία **γεννιέται `DO_NOT_CONTACT`** (ποτέ πρώτα NEW). Match, score, breakdown, κατώφλι
    αμετάβλητα· άλλος οργανισμός → κανονικά `new`. Υπάρχουσα γραμμή: το C8 **ποτέ** δεν ξαναγράφει το status της (DNC
    μένει DNC, WON μένει WON). Η ενέργεια D35 παίρνει το ίδιο company lock πρώτα, άρα ταυτόχρονο C8 create και DNC
    σειριοποιούνται. Κόστος: +2 queries ανά **νέα** γραμμή (lock + lookup), 0 στις επανακαταγραφές. Χωρίς έγκυρο ΓΕΜΗ:
    κανένα lookup, κανονικά `new`.
  - **Legacy:** το `PersonSuppression` (πλατφορμικό, άρθρο 21, ανά όνομα προσώπου) και το `OutreachSuppression`
    (πλατφορμικό unsubscribe/bounce του legacy outreach email) μένουν ανεξάρτητα· ούτε διαβάζονται ούτε γράφονται.
  - Route `POST /organizations/<id>/opportunities/company/<company_id>/do-not-contact/` (`reason`, `confirm=yes`):
    login, CSRF, redirect στη σελίδα D29, `next=` αγνοείται· type/value/source/creator/status ποτέ από το request.
    Σελίδα D29: **ένα** company-level τμήμα «ΜΗ ΕΠΙΚΟΙΝΩΝΙΑ» (όχι ανά Radar, όχι στο dropdown του D32) με εξήγηση,
    select λόγου και υποχρεωτικό checkbox επιβεβαίωσης (ελέγχεται στον server)· μετά: «✓ Χωρίς επικοινωνία» με λόγο
    και ημερομηνία, χωρίς αναίρεση. 18 queries για τη σελίδα.
  - Κανένα audit log (**βήμα 36** θα καταγράψει «suppression added»), καμία ειδοποίηση (**βήμα 37**).
  - Κύκλος migration στο αντίγραφο της dev βάσης: forward → 0 → fixture (3 Radars στον A, 1 στον B) → parity →
    reverse → reapply (ξανά 0), όλα PASS.
  - 39 νέα tests· **1.765 tests OK** με τη μία κανονική εκτέλεση (`--parallel 4`). Σε αποτυχία μέσα σε parallel
    worker (χωρίς `tblib` δεν φαίνεται traceback) ξανατρέξε το module σειριακά. `G5_STATUS =
    PASSED_FOR_CURRENT_ORGANIZATION_SURFACE`· `G4_STATUS = NOT_PASSED`· **`PRODUCTION_MIGRATION_STATUS =
    BLOCKED_BY_G0_G1`.**


- **Επιτάχυνση του πλήρους test suite (2026-09-19).** Το D34 δεσμεύτηκε (`a3ae6a3`). Μέτρηση: η δημιουργία της test
  βάσης (in-memory SQLite, 51 migrations) κοστίζει ~4 s· ο **PBKDF2 hasher** (1.000.000 iterations) κόστιζε
  ~0,51 s ανά hash και τα `setUp` δημιουργούν χιλιάδες χρήστες με password — αυτό ήταν σχεδόν όλο το runtime.
  Νέο `config/fast_test_runner.py` (test-only `MD5PasswordHasher`, και στους parallel workers), `TEST_RUNNER` στο
  `settings.py`, `BackupCriticalTests` με ρητό PBKDF2 override, 2 guard tests (`test_fast_test_runner.py`: το
  production κρατά τον default hasher). Serial με fast hasher: 82 s· **`--parallel 4`: ~40 s**· `--parallel 8`: 36 s·
  `--keepdb`: κανένα κέρδος. Προηγούμενες σειριακές εκτελέσεις: 1.895–2.343 s. **1.726 tests OK** (1.724 + 2 guards),
  κανένα test/assertion δεν αφαιρέθηκε ή αποδυναμώθηκε. Κανονική εντολή και νέα πολιτική επαλήθευσης: βλ. «Τοπική
  εκκίνηση».


- **Gemi Leads 2.0 — D34: Tasks (2026-09-19).** Εργασίες CRM-lite ανά ευκαιρία C8 (§42 «tasks»). Migration
  `0051_opportunity_task` (ένα `CreateModel`, κανένα backfill), μοντέλο `OpportunityTask`,
  `organization_access.create_authorized_opportunity_task` / `complete_authorized_opportunity_task`, views
  `create_opportunity_task` / `complete_opportunity_task`.
  - Το D33 δεσμεύτηκε (`8624c52`). Το blueprint δεν όριζε το σχήμα· **οι αποφάσεις D34 είναι του χρήστη** (v1).
  - **Ανήκει σε μία ευκαιρία** (όχι εταιρεία/οργανισμό/μέλος/Radar), φέρει ρητά `organization` (CASCADE),
    `opportunity` (CASCADE), `title` (plain text, 1–200, trim, NUL απορρίπτεται, DB CHECK), `due_on` (**DateField**,
    μόνο `YYYY-MM-DD`, σήμερα ή μετά κατά `timezone.localdate()` — Europe/Athens, όχι UTC· καμία ώρα/υπενθύμιση),
    `created_by` / `assigned_to` / `completed_by` → **`OrganizationMember`** (SET_NULL), `completed_at`, `created_at`.
    Κατάσταση: OPEN όσο `completed_at IS NULL`, αλλιώς COMPLETED· κανένα boolean/enum/description/`updated_at`.
  - **Μόνο create + complete.** Καμία επεξεργασία, διαγραφή, ακύρωση, reopen, αλλαγή υπευθύνου. Ολοκλήρωση
    ολοκληρωμένης = **no-op χωρίς εγγραφή** (χρόνος/ολοκληρωτής αμετάβλητοι). «Εκπρόθεσμη» = παράγεται στην
    ανάγνωση (ανοιχτή και `due_on < σήμερα`· η σημερινή δεν είναι εκπρόθεσμη), δεν αποθηκεύεται.
  - **Υπεύθυνος εργασίας** (ανεξάρτητος από `Opportunity.assigned_to`, που δεν αλλάζει): ενεργός OWNER ή
    SALES_MANAGER του οργανισμού, ή **μόνο** ο SALES_USER στον οποίο είναι ανατεθειμένη η ευκαιρία — ποτέ άσχετος
    πωλητής, ADMIN ή VIEWER. Προεπιλογή: ο έγκυρος πωλητής της ευκαιρίας, αλλιώς ο δημιουργός. Ο SALES_USER
    δημιουργεί μόνο στη δική του ευκαιρία και μόνο για τον εαυτό του (άλλο id → άρνηση, όχι σιωπηλή αντικατάσταση).
  - **Δικαίωμα `manage_opportunity_tasks`:** OWNER, SALES_MANAGER, SALES_USER· ADMIN/VIEWER μόνο ανάγνωση.
    Ανάγνωση = όποιος βλέπει την ευκαιρία (ο πωλητής βλέπει και εργασίες του διευθυντή στη δική του ευκαιρία).
    Ολοκλήρωση: διευθυντές κάθε εργασία· ο SALES_USER **μόνο** όσες έχουν `assigned_to == membership του`. LIVE
    μόνο· SHADOW/ξένη/ανύπαρκτη/αταίριαστη εργασία → ίδιο 404. Επιτρέπονται και σε τελικές ευκαιρίες.
  - **Κλειδώματα:** ευκαιρία → εργασία (για ολοκλήρωση) → memberships (δρώσα + ονομαζόμενη) σε αύξουσα σειρά id, με
    επανέλεγχο της ακριβούς δρώσας membership (id/χρήστης/ρόλος)· ίδια σειρά με τον collector `SET_NULL`. Αφαίρεση
    μέλους πριν το κλείδωμα → 404· FK σφάλμα στο commit → 404, ποτέ 500.
  - Διαγραφή μέλους: δημιουργός → NULL («Πρώην μέλος»)· υπεύθυνος → NULL (ανοιχτή, «Χωρίς ανάθεση», μόνο διευθυντές
    την ολοκληρώνουν)· ολοκληρωτής → NULL, παραμένει ολοκληρωμένη. Επανένταξη = νέα membership, δεν παίρνει τίποτα.
  - Routes: `POST .../opportunities/<id>/tasks/` (`title`, `due_on`, προαιρετικό `assignee_membership_id`) και
    `POST .../opportunities/<id>/tasks/<task_id>/complete/`· login, CSRF, redirect στη σελίδα D29, `next=` αγνοείται.
  - Σελίδα D29: «ΕΡΓΑΣΙΕΣ» κάτω από τη δική τους γραμμή Radar, χωριστά από τις σημειώσεις· ανοιχτές πρώτα (προθεσμία,
    δημιουργία, id), μετά ολοκληρωμένες (νεότερη ολοκλήρωση)· **50** σε όλη τη σελίδα με ουδέτερη ειδοποίηση· φόρμα
    με selector μόνο για διευθυντές (ο πωλητής βλέπει «Υπεύθυνος: <όνομα>»)· κουμπί ολοκλήρωσης μόνο όπου επιτρέπεται.
    **17 queries** για 3 ευκαιρίες με σημειώσεις και 60 εργασίες, ανεξάρτητα από το πλήθος.
  - Καμία επίδραση σε status (ούτε αυτόματο FOLLOW_UP/CONTACTED), ανάθεση ευκαιρίας, score, σημειώσεις, B6 timeline,
    C9 feed. Κανένα audit log (**βήμα 36**), καμία ειδοποίηση/υπενθύμιση/`TASK_DUE`/scheduler (**βήμα 37**).
  - Κύκλος migration στο αντίγραφο της dev βάσης: forward → 0 εργασίες → fixture → parity → reverse → reapply
    (ξανά 0), όλα PASS.
  - 35 νέα tests· **1.724 tests OK** (δύο συνεχόμενες πλήρεις εκτελέσεις). `G5_STATUS =
    PASSED_FOR_CURRENT_ORGANIZATION_SURFACE`· `G4_STATUS = NOT_PASSED`· **`PRODUCTION_MIGRATION_STATUS =
    BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — D33: Notes (2026-09-19).** Εσωτερικές CRM σημειώσεις ανά ευκαιρία C8 (§41 «opportunity_notes»).
  Migration `0050_opportunity_note` (ένα `CreateModel`, κανένα backfill), μοντέλο `OpportunityNote`,
  `organization_access.add_authorized_opportunity_note`, view `organization_views.add_opportunity_note`.
  - Το D32 δεσμεύτηκε (`664cc69`).
  - **Ανήκει σε μία ευκαιρία** (όχι σε εταιρεία/card/Radar/signal) και φέρει ρητά `organization` (§22), που ο writer
    παίρνει πάντα από την ευκαιρία. **Author = `OrganizationMember`** (`SET_NULL`): διαγραφή μέλους → η σημείωση
    μένει με `author = NULL` («Πρώην μέλος»)· επανένταξη = νέα membership που δεν ξαναπαίρνει την υπογραφή.
    Organization/Opportunity `CASCADE`. Κανένα snapshot ονόματος/email.
  - **Append-only v1:** καμία επεξεργασία, διαγραφή, soft delete, mentions, pin, ιδιωτικές σημειώσεις· καμία dedupe
    (ίδιο κείμενο δύο φορές = δύο σημειώσεις). Admin μόνο ανάγνωση (ούτε delete).
  - **Κείμενο:** plain text, `\r\n`→`\n`, trim άκρων, εσωτερικές γραμμές διατηρούνται, **1–4000 χαρακτήρες** μετά
    το trim (και DB CHECK), NUL απορρίπτεται. Autoescape + `white-space: pre-wrap`, κανένα `|safe`. Τίποτα δεν
    αντιγράφεται αυτόματα από raw_data/επαφές/πρόσωπα· ό,τι γράψει ο χρήστης αποθηκεύεται όπως γράφτηκε.
  - **Δικαίωμα `add_opportunity_note`:** OWNER, SALES_MANAGER, SALES_USER (μόνο σε ευκαιρίες με
    `assigned_to_id == membership_id`)· ADMIN/VIEWER **μόνο ανάγνωση**. Ανάγνωση = όποιος βλέπει την ευκαιρία·
    κανένα ξεχωριστό path «όλες οι σημειώσεις». LIVE μόνο· SHADOW/ξένη/ανύπαρκτη/μη ανατεθειμένη → ίδιο 404.
    **Επιτρέπονται και σε τελικές ευκαιρίες** (WON/LOST/NOT_RELEVANT/DO_NOT_CONTACT).
  - `POST /organizations/<organization_id>/opportunities/<opportunity_id>/notes/`
    (`organization_add_opportunity_note`) με πεδίο `body`: login, POST, CSRF, `transaction.atomic` +
    `select_for_update(of=("self",))` στην ευκαιρία, redirect στη σελίδα D29 (το `next=` αγνοείται).
  - Σελίδα D29: σημειώσεις κάτω από τη **δική τους** γραμμή Radar, νεότερες πρώτα (`-created_at, -id`), με
    όνομα συντάκτη («Πωλητής #id» / «Πρώην μέλος», ποτέ email)· **50 πιο πρόσφατες** σε όλη τη σελίδα με
    ουδέτερη ειδοποίηση περικοπής· ένα query (15 συνολικά για 3 ευκαιρίες, ανεξάρτητα από το πλήθος).
  - Καμία επίδραση σε status, ανάθεση, score/breakdown, B6 timeline, C9 feed. Κανένα audit log (**βήμα 36**),
    καμία ειδοποίηση (**βήμα 37**).
  - Κύκλος migration στο αντίγραφο της dev βάσης: forward → 0 σημειώσεις → fixture → parity → reverse → reapply
    (ξανά 0), όλα PASS (αλλάζουν μόνο `auth_permission`/`django_content_type`, όπως σε κάθε `CreateModel`).
  - **Race διαγραφής μέλους:** μέσα στη μία συναλλαγή κλειδώνεται πρώτα η ευκαιρία και μετά η **ακριβής**
    membership (ίδιο id/οργανισμός/χρήστης/ρόλος) με `select_for_update`· αν λείπει ή άλλαξε → ίδιο 404, ποτέ
    σημείωση με `author=NULL`· FK σφάλμα στο commit → επίσης 404, όχι 500. Σειρά κλειδωμάτων ευκαιρία → μέλος, ίδια
    με τον collector `SET_NULL` του Django. Υπόλοιπο: μια ταυτόχρονη διαγραφή μέλους (σήμερα μόνο από staff admin ή
    cascade) μπορεί να αποτύχει στο δικό της commit αν η σημείωση γράφτηκε ενδιάμεσα — μελλοντική υπηρεσία
    αφαίρεσης μελών θα χρειαστεί ρητό σχεδιασμό κλειδωμάτων (ή DB-level ON DELETE SET NULL).
  - 33 νέα tests· **1.689 tests OK** (δύο συνεχόμενες πλήρεις εκτελέσεις). `G5_STATUS =
    PASSED_FOR_CURRENT_ORGANIZATION_SURFACE`· `G4_STATUS = NOT_PASSED`· **`PRODUCTION_MIGRATION_STATUS =
    BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — D32: Statuses (2026-09-18).** Οι γενικές καταστάσεις πωλήσεων του §39 ανά ευκαιρία C8.
  `organization_access.set_authorized_opportunity_status`, view `organization_views.change_opportunity_status`.
  **Χωρίς migration** (τελευταία παραμένει η `0049_opportunity_assignment`).
  - Το D31 δεσμεύτηκε (`6e36f4c`).
  - **Στόχοι D32, ακριβώς:** CONTACTED, INTERESTED, FOLLOW_UP, WON, LOST, NOT_RELEVANT. Το SAVED παραμένει του D30,
    το ASSIGNED του D31, το DO_NOT_CONTACT **αναβάλλεται στο βήμα 35** (μαζί με suppression)· NEW/VIEWED ποτέ
    στόχοι. **Κανένα αυτόματο VIEWED** — το GET της σελίδας D29 κάνει μόνο SELECT.
  - Το §39 δεν ορίζει γράφο, οπότε **κανένα γραμμικό funnel**: κάθε μη τελική ευκαιρία (new, viewed, saved,
    assigned, contacted, interested, follow_up) μπορεί να πάει σε κάθε στόχο D32. **Τελικές:** WON, LOST,
    NOT_RELEVANT, DO_NOT_CONTACT — κάθε αίτημα D32 (και ο ίδιος στόχος) απορρίπτεται· **κανένα reopen**.
    Ίδια κατάσταση σε μη τελική = **no-op χωρίς καμία εγγραφή** (ούτε `updated_at`). Άγνωστος/ειδικός στόχος →
    άρνηση χωρίς εγγραφή.
  - **Δικαίωμα `update_opportunity_status`** στο `ROLE_CAPABILITIES`: OWNER, SALES_MANAGER, SALES_USER· ADMIN/VIEWER
    όχι. Ο SALES_USER αλλάζει **μόνο** ευκαιρίες με `assigned_to_id == context.membership_id` (ίδιο scope ορατότητας
    του D31)· μη ανατεθειμένη, άλλου πωλητή, αδελφή Radar, SHADOW, ξένη, ανύπαρκτη → ίδιο 404. OWNER/SALES_MANAGER
    αλλάζουν κάθε ορατή LIVE ευκαιρία, **χωρίς** απαίτηση ανάθεσης.
  - `POST /organizations/<organization_id>/opportunities/<opportunity_id>/status/`
    (`organization_opportunity_status`) με πεδίο `status`: login, POST, CSRF, `transaction.atomic` +
    `select_for_update(of=("self",))`, redirect στη σελίδα D29 (το `next=` αγνοείται).
  - Η ανάθεση (`assigned_to`, `assigned_at`) **επιβιώνει** κάθε αλλαγής κατάστασης. Μετά από CONTACTED κ.λπ. το
    D31 (επανανάθεση) και το D30 (Save) εξακολουθούν να αρνούνται. Score, class, breakdown, evidence, signals,
    κατάταξη feed: αμετάβλητα· το υπάρχον φίλτρο status του C9 βλέπει τη νέα τιμή.
  - Σελίδα D29: φόρμα κατάστασης ανά γραμμή (select μόνο με στόχους D32 εκτός της τρέχουσας, ελληνικές
    ετικέτες) για OWNER/SALES_MANAGER και για τον ανατεθειμένο SALES_USER· τελικές γραμμές δείχνουν μόνο την
    κατάσταση. Ο SALES_USER δεν αποκτά Save/Assign.
  - **Προέλευση κατάστασης μέχρι το βήμα 36 (audit log): μόνο η τρέχουσα τιμή και το `updated_at`.** Κανένα
    ιστορικό, καμία ειδοποίηση (βήμα 37).
  - 27 νέα tests· **1.656 tests OK** (δύο συνεχόμενες πλήρεις εκτελέσεις). `G5_STATUS =
    PASSED_FOR_CURRENT_ORGANIZATION_SURFACE`· `G4_STATUS = NOT_PASSED`· **`PRODUCTION_MIGRATION_STATUS =
    BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — D31: Assign (2026-09-18).** Ανάθεση **μίας συγκεκριμένης ευκαιρίας C8** σε πωλητή.
  Migration `0049_opportunity_assignment` (μόνο δύο `AddField`, κανένα backfill),
  `organization_access.assign_authorized_opportunity`, view `organization_views.assign_opportunity`.
  - Το D30 δεσμεύτηκε (`c5c54cc`).
  - **Σχήμα:** `Opportunity.assigned_to → OrganizationMember` (`SET_NULL`, nullable, `related_name=
    "assigned_opportunities"`) και `Opportunity.assigned_at`. Η ταυτότητα ανάθεσης είναι η **membership**, ποτέ ο
    `User`: διαγραφή της membership → `assigned_to = NULL`, η κατάσταση μένει `assigned`, το `assigned_at` μένει·
    επανένταξη του ίδιου χρήστη = νέα membership που **δεν** ξαναβλέπει την ευκαιρία.
  - **Assignee:** μόνο ενεργός (`user.is_active`) **SALES_USER** του **ίδιου** οργανισμού· αλλιώς άρνηση χωρίς
    εγγραφή. **Actors:** OWNER, SALES_MANAGER (`assign_opportunities`)· ADMIN/VIEWER/SALES_USER → ίδιο 404.
    Μόνο LIVE `latest_signal`· SHADOW/ξένη/ανύπαρκτη → ίδιο 404.
  - **Μεταβάσεις:** πρώτη ανάθεση από `new`/`viewed`/`saved` → `assigned`· ίδιος assignee ενώ `assigned` = **no-op
    χωρίς καμία εγγραφή**· άλλος SALES_USER = επανανάθεση (μένει `assigned`, αλλάζει `assigned_at`)· contacted,
    interested, follow_up, won, lost, not_relevant, do_not_contact → άρνηση, ανέγγιχτη. **Κανένα unassign.**
  - **Ορατότητα SALES_USER:** `assigned_to_id == context.membership_id`, εφαρμόζεται στο SQL **πριν** τη
    συσσώρευση του C9 (`get_opportunity_feed(..., assigned_to_membership_id=...)`). Σε εταιρεία με πολλά Radars ο
    πωλητής βλέπει μόνο τη δική του ευκαιρία.
  - `POST /organizations/<organization_id>/opportunities/<opportunity_id>/assign/`
    (`organization_assign_opportunity`) με `assignee_membership_id`: login, POST, CSRF, `transaction.atomic` +
    `select_for_update(of=("self",))`, redirect στη σελίδα D29 (το `next=` αγνοείται).
  - Σελίδα D29: ανάθεση ανά γραμμή με **όνομα** (ποτέ email· χωρίς όνομα → «Πωλητής #id») και ημερομηνία· φόρμα
    Ανάθεση/Επανανάθεση (select με τους SALES_USER του οργανισμού) μόνο για OWNER/SALES_MANAGER και μόνο σε
    NEW/VIEWED/SAVED/ASSIGNED.
  - Score, class, breakdown, evidence, signals, primary: αμετάβλητα. Η δραστηριότητα του §40 («Assigned by …»)
    ανήκει στο audit log του **βήματος 36**· καμία ειδοποίηση (**βήμα 37**).
  - Κύκλος migration στο αντίγραφο της dev βάσης: forward → 0 αναθέσεις → fixture (ανάθεση, επανανάθεση, no-op,
    ορατότητα, διαγραφή membership, καθαρισμός) → parity → reverse → reapply, όλα PASS.
  - 22 νέα tests· **1.629 tests OK** (δύο συνεχόμενες πλήρεις εκτελέσεις). `G5_STATUS =
    PASSED_FOR_CURRENT_ORGANIZATION_SURFACE`· `G4_STATUS = NOT_PASSED`· **`PRODUCTION_MIGRATION_STATUS =
    BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — D30: Save (2026-09-18).** Η **πρώτη μετάλλαξη πελάτη** της αρχιτεκτονικής Organization.
  `organization_access.save_authorized_opportunity`, view `organization_views.save_opportunity`. **Χωρίς migration.**
  - Το D29 δεσμεύτηκε (`94299cc`). Απόφαση προϊόντος: **PATH A** — το blueprint δεν ορίζει το Save· αποφασίστηκε
    ρητά ότι Save = στενή ενέργεια κύκλου ζωής μίας συγκεκριμένης ευκαιρίας C8, **όχι** bookmark/shortlist.
  - **Στόχος: μία ρητή ευκαιρία C8** (ένα κουμπί ανά γραμμή Radar στη σελίδα D29)· ποτέ όλες οι ευκαιρίες μιας
    εταιρείας· το primary δεν έχει ειδική μεταχείριση.
  - **Μεταβάσεις:** `new → saved`, `viewed → saved`· `saved → saved` = **idempotent χωρίς καμία εγγραφή** (ούτε
    `updated_at`)· κάθε μεταγενέστερη κατάσταση §39 (assigned, contacted, interested, follow_up, won, lost,
    not_relevant, do_not_contact) **απορρίπτεται και μένει ανέγγιχτη** — ποτέ πισωγύρισμα. **Κανένα unsave**,
    κανένα γενικό endpoint κατάστασης: το βήμα 32 κατέχει τις γενικές μεταβάσεις. Κανένα auto-«viewed».
  - **Δικαίωμα: `manage_opportunity_workflow`** (OWNER, SALES_MANAGER)· ADMIN/VIEWER/SALES_USER → ίδιο 404. Ο
    πίνακας G5 δεν άλλαξε. Μόνο ευκαιρίες με LIVE `latest_signal`· SHADOW → 404 χωρίς διαρροή ύπαρξης.
  - `POST /organizations/<organization_id>/opportunities/<opportunity_id>/save/` (`organization_save_opportunity`):
    login, POST μόνο, CSRF, `transaction.atomic` + `select_for_update(of=("self",))` (το κλείδωμα αποδεικνύεται
    μόνο σε PostgreSQL). Redirect στη σελίδα D29 της εταιρείας της ευκαιρίας — ποτέ caller-supplied URL.
  - Αλλάζει **μόνο** το `Opportunity.status` (+ `updated_at` σε πραγματική μετάβαση). Score, class, breakdown,
    evidence, signals, snapshots, timeline, primary: αμετάβλητα. Κανένα `saved_at`/`saved_by`, κανένα audit log
    (βήμα 36). Το υπάρχον φίλτρο status του C9 βρίσκει φυσικά τις SAVED ευκαιρίες· καμία αλλαγή στο C9.
  - Σελίδα D29: `Αποθήκευση` (φόρμα POST) μόνο για NEW/VIEWED και μόνο σε ρόλους workflow· `✓ Αποθηκευμένο`
    (μη κλικαρίσιμο) για SAVED· τίποτα για μεταγενέστερες καταστάσεις.
  - 19 νέα tests· **1.607 tests OK** (δύο συνεχόμενες πλήρεις εκτελέσεις). `G5_STATUS =
    PASSED_FOR_CURRENT_ORGANIZATION_SURFACE`· `G4_STATUS = NOT_PASSED`· **`PRODUCTION_MIGRATION_STATUS =
    BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — D29: σελίδα ευκαιρίας εταιρείας (2026-09-18).** Η **πρώτη customer-facing επιφάνεια** της
  αρχιτεκτονικής Organization. `gemiapp/organization_views.py`, `gemiapp/company_opportunity_page.py`,
  `templates/organizations/company_opportunity.html`. **Χωρίς migration.**
  - Το G5 δεσμεύτηκε (`ab3645a`): `G5_STATUS = PASSED_FOR_CURRENT_ORGANIZATION_SURFACE`.
  - **Route με ρητό οργανισμό:** `/organizations/<organization_id>/opportunities/company/<company_id>/`
    (`organization_company_opportunity`), `@login_required` + GET μόνο. Καμία επίλυση οργανισμού από session ή
    middleware. Μία σελίδα **ανά (οργανισμός, εταιρεία)** (§36 «Κάθε company page»), ίδια ταυτότητα με την κάρτα του C9.
  - **Μόνη πόρτα: `organization_access.get_authorized_company_opportunity_page`.** Το `organization_views` είναι το
    μόνο product module που εισάγει το `organization_access`· κάθε άρνηση (ανύπαρκτος οργανισμός, μη-μέλος, ξένη ή
    ανύπαρκτη εταιρεία, SALES_USER χωρίς ανάθεση) → **ίδιο 404**, χωρίς διαρροή.
  - **Μόνο LIVE προς πελάτη:** ευκαιρίες των οποίων το τρέχον capture στηρίζεται σε SHADOW signal αφαιρούνται κατά την
    ανάγνωση (η γραμμή δεν αλλάζει)· το χρονολόγιο διαβάζει το B6 **μόνο σε LIVE**. Εταιρεία χωρίς ορατή LIVE
    ευκαιρία → 404. **`G4_STATUS = NOT_PASSED`** — το φίλτρο LIVE δεν είναι πέρασμα του G4.
  - «Γιατί αυτή η ευκαιρία» = **παγωμένο C8 capture** (5 συστατικά v1, reason codes, κανονικά στοιχεία) με ρητό
    «Υπολογισμός score: …— δεν ενημερώνεται αυτόματα». «Τρέχοντα στοιχεία» = **τελευταίο κανονικό snapshot B3**
    (κατάσταση, νομική μορφή, περιφέρεια/δήμος, ίδρυση μόνο αν VALID, επιβεβαιωμένα τρέχοντες ΚΑΔ με έκδοση) —
    ποτέ `Company.is_active`, legacy περιγραφές ή `CompanyActivity`· χωρίς snapshot εμφανίζεται ρητά «μη διαθέσιμα».
  - Primary ευκαιρία με την ίδια σειρά του C9 (`FEED_ORDER`)· όλες οι ορατές ευκαιρίες ανά Radar εμφανίζονται χωρίς
    συγχώνευση status. Χρονολόγιο: 20 πιο πρόσφατα LIVE γεγονότα.
  - **Καμία επαφή** (τηλέφωνο/email), πρόσωπα, διεύθυνση, ΑΦΜ ή payload· το τηλέφωνο μένει μόνο στο Dossier. Καμία
    ενέργεια (Save/Assign/Call/Follow-up/Dismiss) — ούτε ψεύτικα κουμπιά· κανένα auto-«viewed»· GET = μόνο SELECT.
  - Επανεξετάστηκαν σκόπιμα τρία guard tests (C1, G5, D29) που κωδικοποιούσαν «κανένα organization route ακόμη»:
    τώρα επιβάλλουν ότι organization routes υπάρχουν **μόνο** μέσω του `organization_views`.
  - Αντίγραφο dev: 0 οργανισμοί/ευκαιρίες → καμία πραγματική σελίδα· οπτικός έλεγχος με προσωρινό fixture που
    αφαιρέθηκε πλήρως. 23 νέα tests· **1.588 tests OK** (δύο συνεχόμενες πλήρεις εκτελέσεις).
    **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — G5: απομόνωση tenants / εξουσιοδότηση οργανισμού (2026-09-18).** `gemiapp/organization_access.py`.
  **Χωρίς migration, model, route, middleware ή UI.** `G5_STATUS = PASSED_FOR_CURRENT_ORGANIZATION_SURFACE`.
  - **Η Phase C ολοκληρώθηκε** (βήματα 20–28)· το C9 feed δεσμεύτηκε (`70d7485`)· το signup rate-limit test
    σταθεροποιήθηκε ξεχωριστά (`17de789`, μόνο test).
  - Κάθε απόφαση ξεκινά από το ρητό ζεύγος **(user, organization)** και το `OrganizationMember` που τα ενώνει — ποτέ
    `user.organization`, πρώτη/νεότερη membership, συνδρομή, Radar ή URL. Χωρίς membership, καμία πρόσβαση. Η
    membership ξαναδιαβάζεται σε κάθε κλήση (διαγραφή ή αλλαγή ρόλου ισχύει αμέσως)· κανένα cache.
  - `get_organization_access_context(user, organization)` → immutable `OrganizationAccessContext` (organization,
    user, membership, role), με ιδιωτικό σήμα έκδοσης: context φτιαγμένο με το χέρι απορρίπτεται. **Staff/superuser
    δεν έχουν παράκαμψη** στο προϊόν πελάτη· το admin site είναι ξεχωριστό.
  - **Ένα σφάλμα, `OrganizationAccessDenied`**, ίδιο μήνυμα για μη-μέλος, έλλειψη δικαιώματος, ξένο ή ανύπαρκτο
    αντικείμενο — καμία διαρροή ύπαρξης, καμία απαρίθμηση ids (§82: «Organization A requests opportunity B»).
  - **Κεντρικός πίνακας §64** (`ROLE_CAPABILITIES`, μόνο εκεί συγκρίνονται ρόλοι): OWNER όλα· ADMIN διαχείριση
    οργανισμού (ρυθμίσεις, μέλη, Radars) + ανάγνωση· SALES_MANAGER leads (workflow, ανάθεση) + ανάγνωση· VIEWER μόνο
    ανάγνωση· **SALES_USER μόνο ανατεθειμένες ευκαιρίες — η ανάθεση δεν υπάρχει ακόμη, άρα βλέπει καμία** (κενό feed,
    άρνηση σε κάθε id). Η εξάρτηση αυτή είναι ρητή (`ASSIGNMENT_DEPENDENT`) και θα διευρυνθεί από το πακέτο ανάθεσης.
  - Βοηθοί με scoping **στο SQL**: `organization_radars_for`, `organization_opportunities_for`, `get_authorized_radar`,
    `get_authorized_opportunity` (βάση της σελίδας D29), `get_authorized_opportunity_score_breakdown` (παγωμένο C8
    «Why this lead»), `get_authorized_opportunity_feed` (τυλίγει το C9 χωρίς να το αλλάζει· ξένο Radar id → ίδια άρνηση),
    `get_authorized_organization_profile`, `get_authorized_icp_criteria`, `organization_members_for` (ids/ρόλοι μόνο).
  - Τα C5–C7 (matching/scoring/live breakdown) αξιολογούν Radars **όλων** των οργανισμών by design: δεν πρέπει ποτέ
    να εξυπηρετούν αίτημα πελάτη. Ο κώδικας πελάτη διαβάζει μόνο μέσω του `organization_access`.
  - Πλατφόρμα ≠ tenant: IndustryTemplate και δεδομένα αναφοράς ΓΕΜΗ δεν γίνονται ποτέ organization-scoped.
    Legacy user-owned προϊόν (CustomerRadar, UserCompanyLead, UserSubscription, billing) αμετάβλητο.
  - 26 νέα tests· **1.565 tests OK** (δύο συνεχόμενες πλήρεις εκτελέσεις).
    **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`** — το G5 αφορά ασφάλεια tenants, όχι ετοιμότητα deploy.


- **Gemi Leads 2.0 — C9: read model του feed ευκαιριών (2026-09-18).** `gemiapp/opportunity_feed.py`, εντολή μόνο-ανάγνωσης
  `show_opportunity_feed --organization-id`. **Χωρίς model, migration ή εγγραφή.**
  - Το C8 (αποθήκευση ευκαιριών) ολοκληρώθηκε και δεσμεύτηκε (`afebd70`)· το flaky login rate-limit test
    σταθεροποιήθηκε ξεχωριστά (`799f0bc`, μόνο test: καρφωμένο ρολόι του limiter, ίδιο `5/m`).
  - Το C9 διαβάζει **μόνο** τις αποθηκευμένες ευκαιρίες του C8 — δεν κάνει matching, scoring, live breakdown,
    δημιουργία, rescoring ή αλλαγή status. Δείχνει το **παγωμένο** capture· **κανένα live decay**: το score αλλάζει
    μόνο όταν το C8 δεχτεί νέο qualifying signal· εκτίθεται το `scored_as_of`.
  - **Μία κάρτα ανά (organization, company)** (§34), χωρίς συγχώνευση γραμμών· κάθε υποκείμενη ευκαιρία μένει ως
    immutable child (Radar, score, class, status, reason, τελευταίο signal).
  - **Primary opportunity** (από όπου προέρχονται *όλα* τα πεδία της κάρτας): υψηλότερο παγωμένο score → πιο πρόσφατο
    `latest_signal.detected_at` → μικρότερο id. **Freshness = `latest_signal.detected_at`** (ποτέ `updated_at`, ρολόι
    ή επανυπολογισμός)· ξεχωριστά `primary_signal_detected_at` και `latest_company_signal_detected_at`.
  - **Πλήθος σχετικών signals = distinct** contributing signals της κάρτας (ένα signal από δύο Radars μετράει μία φορά).
  - **Φίλτρα §35** σε επίπεδο ευκαιρίας, **πριν** τη συνάθροιση (η κάρτα κρατά μόνο τις επιζώσες και διαλέγει primary
    ανάμεσά τους): score class, min/max score (inclusive), status, Radar (μόνο του οργανισμού· ξένο id απορρίπτεται),
    signal type (μόνο contributing signals), ΚΑΔ και περιοχή από το **παγωμένο scoring evidence** (ακριβές
    code+version / level+source id), ημερομηνία στο `latest_signal.detected_at` (inclusive, timezone-aware). AND μεταξύ
    διαστάσεων, OR μέσα σε διάσταση.
  - **«Today's Opportunities» = τίτλος οθόνης**, όχι φίλτρο ημερομηνίας· καμία απόκρυψη παλαιότερων. Κανένα φίλτρο
    λήξης (δεν υπάρχει κανόνας).
  - **Ταξινόμηση** score DESC → `primary_signal_detected_at` DESC → primary id ASC. **Keyset pagination σε SQL**
    (cursor = ordering tuple του primary)· default 50, μέγιστο 200· χωρίς διπλές/χαμένες κάρτες.
  - Scoping ανά οργανισμό **στο SQL**, ρητό Organization, κανένα user/session. 3 queries ανά σελίδα ανεξαρτήτως μεγέθους
    (+1 με φίλτρο Radar). Χωρίς όνομα εταιρείας, επαφές, πρόσωπα, διεύθυνση, ΑΦΜ ή payload στην κάρτα.
  - **Το G5 εξακολουθεί να μπλοκάρει** πρόσβαση πελάτη με πολλά μέλη: κανένα route/view/UI/API, καμία εξουσιοδότηση ρόλων.
    Legacy προϊόν, A9, B4, billing αμετάβλητα· το τηλέφωνο μένει μόνο στο Dossier.
  - Αντίγραφο dev: Organization 0, Opportunity 0 → **0 κάρτες**. 33 νέα tests· **1.539 tests OK** (δύο συνεχόμενες
    πλήρεις εκτελέσεις). **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — C8: μοντέλο Opportunity (2026-09-18).** Migration `0048_opportunity`, `gemiapp/opportunities.py`.
  **Το πρώτο persisted artifact πελάτη** της αρχιτεκτονικής 2.0.
  - Το C7 (score breakdown) ολοκληρώθηκε και δεσμεύτηκε (`2cf4bfb`). Αγωγός:
    `Signal → C5 match → C6 score → C7 breakdown → C8 opportunity`· μόνο το τελευταίο βήμα γράφει.
  - **Ταυτότητα: ένα row ανά (organization, radar, company)** με unique constraint — ποτέ ανά signal (§34: «ένα
    company card», όχι ένα ανά γεγονός). Κάθε qualifying signal προσαρτάται ως `OpportunitySignal`·
    `first_signal` = το γεγονός που άνοιξε την ευκαιρία (δεν ξαναγράφεται), `latest_signal` = αυτό του τρέχοντος
    capture. Το ενιαίο `signal_id` του §29 χωρίστηκε ρητά στα δύο.
  - **Threshold — εδώ ενεργοποιείται για πρώτη φορά:** `score_threshold` null ⇒ κάθε επιβεβαιωμένο match περνά·
    αλλιώς `score >= threshold`. **Καμία default τιμή δεν εφευρέθηκε** (το blueprint δεν ορίζει). Το threshold δεν
    αλλάζει ποτέ το score. Μη επιλέξιμη αξιολόγηση **δεν γράφει τίποτα** και δεν πειράζει υπάρχουσα ευκαιρία.
  - **Παγωμένο capture:** score, κλάση, `score_rule_version`, `match_rule_version`, `scored_as_of`,
    `primary_reason_code` και **πέντε** `OpportunityScoreComponent` + `OpportunityScoreEvidence` γράφονται τη στιγμή
    της σύλληψης. Το `get_opportunity_score_breakdown` **διαβάζει** μόνο — μεταγενέστερη επεξεργασία Radar δεν
    αλλάζει ό,τι ειπώθηκε στον πελάτη (αποδεδειγμένο σε tests και στο αντίγραφο dev).
  - **Συνάθροιση:** νέο qualifying signal ⇒ προσάρτηση + **νέο capture** (το πιο πρόσφατο κερδίζει, ακόμη κι αν
    είναι χαμηλότερο — όχι «max ever»)· `created_at` και `first_signal` διατηρούνται· κανένα contributing signal δεν
    διαγράφεται. Κρατείται **μόνο το τρέχον** capture (το blueprint ζητά τρέχον score)· ανά γεγονός μένει το score
    του στο `OpportunitySignal`.
  - **Idempotency/concurrency:** ίδιο signal+Radar ξανά ⇒ καμία εγγραφή (ούτε `updated_at`)· unique constraints σε
    (organization, radar, company), (opportunity, signal), (opportunity, component code)· `transaction.atomic` με
    `select_for_update` και fallback σε IntegrityError. Το SQLite δεν αποδεικνύει κλειδώματα PostgreSQL.
  - **Status §39** (new, viewed, saved, assigned, contacted, interested, follow_up, won, lost, not_relevant,
    do_not_contact): αποθηκεύεται και επικυρώνεται, **χωρίς πολιτική μεταβάσεων** (ανήκει στο workflow package).
    `reason` = **σταθερός κωδικός** (`primary_reason_code`, το reason code του ισχυρότερου awarded component) —
    ποτέ κείμενο, ποτέ AI. `expires_at`: υπάρχει γιατί το §29 το αναφέρει, **αλλά το blueprint δεν ορίζει κανόνα
    λήξης**, οπότε δεν εφευρέθηκε κανένας και δεν συμπληρώνεται ποτέ· κανένα cleanup job.
  - **Χωρίς feed/UI/route/task/schedule/δίκτυο** (το §35 είναι το C9). Καμία σχέση με `UserCompanyLead`,
    `RadarMatch`, `CustomerRadar`, A9, B4, συνδρομές ή billing — καμία migration δεδομένων από αυτά. Admin μόνο
    ανάγνωσης. Καμία επαφή/τηλέφωνο/πρόσωπο/διεύθυνση/ΑΦΜ/payload στα δεδομένα· hotfix τηλεφώνου άθικτο.
  - Αντίγραφο dev: **και οι 4 νέοι πίνακες 0** μετά από forward/reverse/reapply· 4 content types, 16 permissions·
    parity (behaviour/catalogue/monitoring) PASS. 29 νέα tests· **1.506 tests OK** (δύο συνεχόμενες πλήρεις
    εκτελέσεις). **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — C7: score breakdown (2026-09-18).** `gemiapp/opportunity_score_breakdown.py`· η υπάρχουσα εντολή
  `show_organization_radar_score --breakdown` δείχνει την ανάλυση. **Χωρίς migration, χωρίς αποθήκευση.**
  - Το C6 (scoring) ολοκληρώθηκε και δεσμεύτηκε (`c13bad2`). Το C7 **εξηγεί** ένα έγκυρο `OpportunityScore` — δεν
    ξαναϋπολογίζει score και δεν ξανακρίνει matching. Μία μόνο πηγή αλήθειας για πόντους: το C6.
  - **Το §32 είναι παράδειγμα του απορριφθέντος §30** (δείχνει «+7 Contactability», σύνολο 92/100). Το C7 εξηγεί το
    **πραγματικό συμβόλαιο v1** (ΚΑΔ 30, σήμα 25, γεωγραφία 15, νομική μορφή 10, φρεσκάδα 20 = 100).
  - **Ακριβώς πέντε συστατικά, σταθερή σειρά:** `industry_fit`, `signal_relevance`, `geographic_fit`,
    `legal_form_fit`, `freshness`. Καμία γραμμή για contactability, χαρακτηριστικά/ηλικία εταιρείας ή template fit —
    ούτε καν μηδενική.
  - Κάθε γραμμή: `awarded_points/max_points`, status (`awarded`/`not_awarded`), **σταθερός `reason_code`** (π.χ.
    `exact_kad_match`, `radar_accepts_any_signal_type`, `fresh_within_72h`, `stale_over_30d`) και **τυποποιημένα
    κανονικά στοιχεία** (ΚΑΔ κωδικός+έκδοση, επίπεδο+source id περιοχής, source id νομικού τύπου, τύπος σήματος,
    χρόνοι φρεσκάδας με ακέραια ηλικία σε δευτερόλεπτα). Οι ελληνικές ετικέτες είναι **μόνο παρουσίαση**, ποτέ ταυτότητα.
  - **Αμετακίνητες σταθερές:** άθροισμα γραμμών == score, μέγιστα == 100, κλάση μέσω του ίδιου helper του C6, ίδιο
    `as_of` με τον υπολογισμό, μόνο `opportunity_score:v1` + `organization_radar_match:v1`. Σε οποιαδήποτε διαφωνία
    **σφάλμα**, ποτέ σιωπηλή «διόρθωση».
  - Δύο προσθετικές αλλαγές χωρίς αλλαγή σημασιολογίας: το `OpportunityScore` μεταφέρει πλέον το `as_of`, και το C5
    απέκτησε τον καθαρό βοηθό **μόνο ανάγνωσης** `confirmed_criteria` (χρησιμοποιεί τα ίδια predicates του C5, ώστε τα
    στοιχεία να μην μπορούν να αποκλίνουν από το match). Τα βάρη, ο τύπος και το matching δεν άλλαξαν.
  - Καθαρός builder **χωρίς κανένα query**· end-to-end helper = 15 queries ανεξαρτήτως πλήθους αποτελεσμάτων.
  - Κανένα Opportunity/feed/UI/route/task/schedule/δίκτυο· το `score_threshold` και η επιλεξιμότητα παραμένουν για το
    επόμενο πακέτο. `CustomerRadar`, RadarMatch, digests, A9, B4, billing αμετάβλητα· hotfix τηλεφώνου άθικτο.
  - Αντίγραφο dev: CompanySignal 0, OrganizationRadar 0 → **0 πραγματικές αναλύσεις**. 30 νέα tests·
    **1.477 tests OK** (δύο συνεχόμενες πλήρεις εκτελέσεις). **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — C6: opportunity scoring (2026-09-18).** `gemiapp/opportunity_scoring.py`, εντολή μόνο-ανάγνωσης
  `show_organization_radar_score --signal-id --as-of`. **Χωρίς migration, χωρίς αποθήκευση score.**
  - Το C5 (matching engine) ολοκληρώθηκε (`e498c88`). Το C6 βαθμολογεί **μόνο επιβεβαιωμένο C5 MATCH**·
    `INSUFFICIENT_STATE`/`NO_MATCH` απορρίπτονται, δεν γίνονται «χαμηλό score».
  - **Τα §30/§32 του blueprint είναι ρητά παραδείγματα και ΔΕΝ είναι το κανονιστικό συμβόλαιο.** Το πραγματικό
    **συμβόλαιο v1** (`opportunity_score:v1`, αλλαγή βαρών/σημασιολογίας = `:v2`) είναι:

    | Συστατικό | Μέγιστο |
    | --- | ---: |
    | Industry / ΚΑΔ fit | 30 |
    | Signal relevance | 25 |
    | Geographic fit | 15 |
    | Legal form fit | 10 |
    | Freshness | 20 |
    | **Σύνολο** | **100** |

  - **Τα «available contact» και «company characteristics» ΔΕΝ ανήκουν στο v1** — ούτε ως μόνιμα μηδενικά συστατικά.
    Τα στοιχεία επαφής (τηλέφωνο/email) **δεν βαθμολογούν ποτέ**· δεν υπάρχει εγκεκριμένος ντετερμινιστικός κανόνας για
    «χαρακτηριστικά εταιρείας» (ηλικία, κεφάλαιο, μέγεθος, πρόσωπα) — μελλοντικός ορισμός απαιτεί νέα έκδοση κανόνα.
  - Ενιαίος κανόνας στόχευσης: **πόντοι μόνο όταν το Radar στόχευσε ρητά τη διάσταση και η κανονική κατάσταση την
    επιβεβαίωσε** (τα `matched_dimensions` του C5). Radar χωρίς κριτήρια σήματος ταιριάζει αλλά παίρνει 0 relevance.
  - **Industry fit = μόνο ακριβής ταυτότητα ΚΑΔ (κωδικός + έκδοση)**· ποτέ IndustryTemplate, prefix, περιγραφή,
    κείμενο προφίλ, crosswalk 2008→2026 ή AI.
  - **Freshness = μονότονη απόσβεση** πάνω στο `detected_at` με ρητό `as_of` (άνω όρια συμπεριλαμβανόμενα):
    ≤24ω → 20, ≤72ω → 18, ≤7 ημέρες → 15, ≤14 → 10, ≤30 → 5, πέραν → 0. Ποτέ πλαστά effective timestamps ή κρυφό
    `timezone.now()`· `as_of` πριν την ανίχνευση απορρίπτεται.
  - Κλάσεις §31 αμετάβλητες (90–100 priority, 75–89 high, 55–74 medium, 0–54 low) και **και οι τέσσερις είναι εφικτές**
    (fixtures: 100, 90, 75, 65, 30).
  - Καθαρός υπολογιστής **χωρίς κανένα query**· batch = ένα πέρασμα matching (9 queries ανεξαρτήτως πλήθους matches).
    Το `score_threshold` δεν αλλάζει το score και δεν εκτίθεται `meets_threshold`· το ICP δεν συμμετέχει· τα exclusions
    είναι veto του matching, όχι ποινή.
  - Κανένα Opportunity/lead/feed/notification, κανένα score breakdown (§32 = C7), κανένα UI/API/task/schedule/δίκτυο.
    `CustomerRadar`, RadarMatch, digests, A9, B4, billing αμετάβλητα. Το hotfix τηλεφώνου άθικτο. G0/G1/G5 αμετάβλητα.
  - Αντίγραφο dev: CompanySignal 0, OrganizationRadar 0 → **0 πραγματικά scores**. 30 νέα tests·
    **1.447 tests OK** (δύο συνεχόμενες πλήρεις εκτελέσεις). **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — C5: matching engine για Organization Radars (2026-09-17).** `gemiapp/organization_radar_matching.py`,
  εντολή μόνο-ανάγνωσης `show_organization_radar_matches` (`--signal-id` ή ρητό `--mode shadow|live`). **Χωρίς migration.**
  - Το C4 (industry templates) ολοκληρώθηκε (`ca18b7c`)· το flaky A6 digest test σταθεροποιήθηκε (`96d3151`, μόνο test:
    καρφωμένο ρολόι του `TimestampSigner`, καμία αλλαγή σε digests/unsubscribe).
  - Τα `OrganizationRadar` έχουν πλέον **εσωτερικό, ντετερμινιστικό matcher**: `find_matching_organization_radars(signal)`
    → immutable `MatchedOrganizationRadar`. Κανόνας `organization_radar_match:v1` (αλλαγή σημασιολογίας = `:v2`).
  - Ροή: context από κανονικό ιστορικό → ένα SQL query υποψηφίων (συντηρητικό superset, ποτέ στενότερο) → batch φόρτωση
    ορισμών (ένα query ανά πίνακα κριτηρίων) → καθαρός evaluator (MATCH / NO_MATCH / INSUFFICIENT_STATE).
  - Κατάσταση εταιρείας: SNAPSHOT_DIFF → `current_snapshot` του B5 evidence· NEW_COMPANY → τελευταίο snapshot με
    `observed_at <= detected_at`· άλλες πηγές → unsupported. Ποτέ `Company.is_active`, legacy περιγραφές,
    `CompanyActivity`, `raw_data`, τηλέφωνο/επαφές.
  - OR μέσα σε διάσταση, AND μεταξύ διαστάσεων, κενή διάσταση = χωρίς περιορισμό (κενά signal types = κάθε υλοποιημένος
    τύπος), exclusion = veto. Άγνωστο γεγονός → INSUFFICIENT_STATE, ποτέ «δεν ταιριάζει». ΚΑΔ = ακριβές (κωδικός, έκδοση).
  - `score_threshold`, ICP και industry templates **αγνοούνται**. **Καμία αποθήκευση matches**, κανένα scoring/opportunity,
    κανένα UI/API/task/schedule/δίκτυο.
  - **Το `CustomerRadar` μένει ο production matcher**· καμία ενσωμάτωση σε A9 monitoring ή B4 planner· RadarMatch,
    digests, leads, billing αμετάβλητα. Το hotfix τηλεφώνου άθικτο. G0/G1/G5 αμετάβλητα.
  - Αντίγραφο dev: CompanySignal 0, CompanySnapshot 0, OrganizationRadar 0 → 0 πραγματικά matches.
    39 νέα tests· **1.417 tests OK** (δύο συνεχόμενες πλήρεις εκτελέσεις). **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**


- **Gemi Leads 2.0 — C4: θεμέλιο industry templates (2026-09-17).** Migration `0047_industry_template`,
  `gemiapp/industry_templates.py`:
  - **Το production hotfix τηλεφώνου μεταφέρθηκε στο feature branch** (cherry-pick του `5f9ee19` ως `1710a7c`·
    κώδικας/template/tests ίδια με production). Το C4 δεν αγγίζει τίποτα από αυτό.
  - **Έλεγχος δυνατοτήτων:** το ΓΕΜΗ (`/metadata/activities`) δεν δίνει parent/level/group· το A5 δεν αποθηκεύει
    κάτι τέτοιο· τα prefixes κωδικών **δεν** είναι αποδεδειγμένη ιεραρχία (μικτοί 4/7/8ψήφιοι, trailing zeros δεν
    σημαίνουν γονέα)· δεν υπάρχει επαληθευμένη ταξινόμηση κλάδων. ⇒ **PATH B**.
  - `IndustryTemplate` (slug σταθερή ταυτότητα, name/description παρουσίαση, active προεπιλογή False) →
    `IndustryTemplateKad` = **ακριβές `GemiKad`** (κωδικός + έκδοση). ΚΑΔ 2008 και 2026 ξεχωριστά· κανένα prefix,
    καμία ιεραρχία, κανένα crosswalk, καμία ταύτιση περιγραφών. **Δεν είναι industry groups.**
  - Το industry-group κριτήριο του C2 ICP **παραμένει αναβληθέν** — δεν υπάρχει κανονική ταξινόμηση.
  - **Κανένα seeded template:** τα παραδείγματα του §27 δεν είναι ελεγμένες αντιστοιχίσεις ΚΑΔ.
  - Ενεργό template θέλει ≥1 ΚΑΔ· retired ΚΑΔ δεν προστίθεται (υπάρχουσα αντιστοίχιση μένει). Υπηρεσίες ατομικές.
  - **Καμία δυναμική σχέση template → Radar/ICP:** μελλοντικό UI θα **αντιγράφει** ελεγμένα κριτήρια
    (`template_kad_proposal`, μόνο ανάγνωση). Admin μόνο ανάγνωσης· κανένα UI/API/task/δίκτυο.
  - Επίσης: διορθώθηκε test του A10 που εξαρτιόταν από την τρέχουσα ημερομηνία (`0bbbb05`).
  - Αντίγραφο dev: GemiKad 0, templates 0, αντιστοιχίσεις 0. 41 νέα/σχετικά tests (C4 + τηλέφωνο)·
    **1.378 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**

- **Hotfix — τηλέφωνο ΓΕΜΗ στο Company Dossier (2026-09-16).** Το `templates/companies/detail.html` δείχνει
  πεδίο «ΤΗΛΕΦΩΝΟ ΓΕΜΗ» με `tel:` link, ή «Δεν υπάρχει διαθέσιμο τηλέφωνο στο ΓΕΜΗ», πίσω από την ίδια πύλη
  συνδρομής με το email. Read-through μέσω `Company.gemi_phones` → `gemiapp/company_contact.py`, που διαβάζει
  **μόνο** το top-level πεδίο `phone` του αποθηκευμένου `raw_data` (ποτέ `persons`, `fax`, `objective`, ποτέ
  regex στο JSON). Καμία migration, καμία αλλαγή importer, κανένα αίτημα ΓΕΜΗ στο άνοιγμα, καμία καταγραφή.
  Κάλυψη στο dev copy: ~89% των εταιρειών έχουν εμφανίσιμο τηλέφωνο.

- **Gemi Leads 2.0 — C3: θεμέλιο Organization Radar (2026-09-16).** Migration `0046_organization_radar`,
  `gemiapp/organization_radars.py`:
  - **Ολοκληρώθηκαν C1 (οργανισμοί), C2 (ICP) και C3 (Organization Radar).** Το `OrganizationRadar` (§25) ανήκει
    σε οργανισμό (πολλά ανά οργανισμό) και είναι **αδρανής** ρύθμιση: τίποτα δεν το διαβάζει.
  - **Το `CustomerRadar` παραμένει το production Radar** (forms, matching, RadarMatch, digests, όρια συνδρομής,
    A9 monitoring, B4 planner). Κανένας συγχρονισμός, καμία backfill, καμία αντιστοίχιση — θέλει ρητό cutover.
  - Πεδία: name (≤80, διπλά επιτρέπονται), active (**προεπιλογή False**), score_threshold (προαιρετικό 0–100 κατά
    §30/§31· αποθηκεύεται μόνο, **κανένα scoring/σύγκριση**).
  - Κριτήρια: `radar_kads` (GemiKad = κωδικός + έκδοση), `radar_regions` (νομός/δήμος με ρητό level),
    `radar_legal_forms` (GemiLegalType), `radar_signal_types` (μόνο υλοποιημένοι detectors), `radar_exclusions`
    (δομημένα: ΚΑΔ, νομός, δήμος, νομική μορφή· όχι signal types, όχι ελεύθερο κείμενο).
  - Ίδια ταυτότητα δεν μπορεί να είναι και στόχος και αποκλεισμός (service· η DB απορρίπτει διπλά ανά πίνακα).
  - **Ενεργό Radar** χρειάζεται ≥1 θετικό κριτήριο (ΚΑΔ/περιοχή/μορφή/τύπο σήματος)· signal-type-only επιτρέπεται,
    exclusions-only όχι. Ενεργοποίηση = μόνο ρύθμιση: **κανένα** matching, RadarMatch, monitoring, task ή κλήση.
  - Το ICP δεν αντιγράφεται ούτε περιορίζει Radar. Υπηρεσίες ατομικές, με ρητό Organization· Radar άλλου
    οργανισμού απορρίπτεται. Admin μόνο ανάγνωσης· κανένα UI/URL/API/middleware/task.
  - Τα υπάρχοντα flows μένουν **User-owned**· κανένα tenant cutover· το **G5** μπλοκάρει ακόμη multi-member χρήση.
    Η εκκρεμής απαίτηση για τηλέφωνο στο Dossier (παρακάτω) παραμένει.
  - Αντίγραφο dev: όλοι οι πίνακες OrganizationRadar = 0. 30 νέα tests· **1.337 tests OK**.
    **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**

- **Gemi Leads 2.0 — C2: ICP οργανισμού (2026-09-16).** Migration `0045_organization_icp`,
  `gemiapp/organization_icp.py`:
  - **Ολοκληρώθηκαν C1 (θεμέλιο οργανισμών) και C2 (ICP).** Υπάρχει πλέον **εσωτερικά** η πρώτη
    organization-owned πληροφορία: ένα `OrganizationICP` ανά οργανισμό (§24), δημιουργείται ρητά.
  - Κριτήρια με κανονική ταυτότητα ΓΕΜΗ: **ΚΑΔ** (`GemiKad` = κωδικός + έκδοση, ποτέ prefix/περιγραφή),
    **περιοχές** (νομός ή δήμος με ρητό level και ξεχωριστό FK), **νομικές μορφές** (`GemiLegalType`),
    **κατάσταση** (`GemiCompanyStatus`, **ποτέ** `Company.is_active`), **ηλικία** σε μήνες (min/max, ανοιχτά
    άκρα), **τύποι σημάτων** (μόνο όσοι έχουν υλοποιημένο detector).
  - **Αποκλεισμοί** = polarity INCLUDE/EXCLUDE στα ΚΑΔ/περιοχές/μορφές/καταστάσεις· η ταυτότητα αγνοεί το
    polarity, άρα ούτε διπλά ούτε «include και exclude» (DB uniqueness + μήνυμα service).
  - **Δεν υλοποιήθηκαν σκόπιμα:** industry groups (δεν υπάρχει κανονική ταξινόμηση — θέλει πρώτα το πακέτο
    industry templates και ιεραρχία ΚΑΔ §9) και priorities (το §24 δεν ορίζει κλίμακα· **κανένα βάρος/score**).
  - Άδειο ICP = **μη ρυθμισμένο**, ποτέ «ταιριάζουν όλες». Ενημέρωση μόνο με `replace_organization_icp`
    (όλα ή τίποτα). Admin μόνο ανάγνωσης.
  - **Καμία** αντιστοίχιση/scoring/lead/opportunity/σήμα/monitoring από το ICP· κανένα ICP από Radars,
    προφίλ ή χρήστες. Τα υπάρχοντα flows (Radars, leads, digests, billing) μένουν **User-owned**· κανένα
    tenant cutover, κανένα UI/URL/middleware/task. Το **G5** μπλοκάρει ακόμη multi-member χρήση.
  - Αντίγραφο dev: όλοι οι πίνακες ICP = 0. 31 νέα tests· **1.307 tests OK**.
    **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**

- **Gemi Leads 2.0 — C1: θεμέλιο οργανισμών (2026-09-16).** Migration `0044_organization_foundation`,
  `gemiapp/organizations.py`:
  - Μοντέλα `Organization`, `OrganizationMember` (ρόλοι §22/§64: owner, admin, sales_manager, sales_user,
    viewer· unique (organization, user)), `OrganizationProfile` (§23: business, products, target_customers,
    location — δηλωμένο ελεύθερο κείμενο, **όχι** targeting, όχι ICP/Radar).
  - **Κανένα δεδομένο δεν έχει γίνει organization-owned.** Radars, leads, RadarMatch, DigestPreference,
    DigestDelivery και `UserSubscription`/billing/entitlements μένουν **User-owned**. Κανένα FK προς
    organization σε υπάρχον μοντέλο· καμία δημιουργία οργανισμού για υπάρχοντες χρήστες (dev copy: 0/0/0).
  - Organization ≠ GEMI `Company`: καμία σχέση μεταξύ τους.
  - Δημιουργία **μόνο** μέσω `create_organization` (ατομικά: organization + OWNER + profile).
  - **Multi-member οργανισμοί μπλοκαρισμένοι από το G5 (tenant isolation):** το `add_organization_member`
    είναι εσωτερικό και δεν καλείται από πουθενά· το admin δεν προσθέτει/αλλάζει τίποτα. Κανένα URL, view,
    middleware, session/current organization, πρόσκληση ή UI.
  - Το επόμενο πακέτο δεν πρέπει να περάσει κατευθείαν σε organization-owned production queries: η
    μεταφορά ιδιοκτησίας θέλει δικό της, ρητό migration/cutover πακέτο (και μετά G5 για πολλά μέλη).
  - 21 νέα tests· **1.276 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**

- **Gemi Leads 2.0 — B6: read model χρονολογίου εταιρείας (2026-09-16).** `gemiapp/company_timeline.py`,
  `manage.py show_company_timeline` (εσωτερικό, μόνο ανάγνωση). **Κανένα νέο μοντέλο, καμία migration.**
  - **Read model, όχι πίνακας:** το `CompanySignal` μένει η μόνη πηγή αλήθειας· το `get_company_timeline`
    διαβάζει σήματα + evidence B2/B5 και επιστρέφει frozen `CompanyTimelineEntry`. Ένα entry ανά σήμα.
  - **Σειρά:** `detected_at` DESC, μετά `id` DESC. Το effective time εκτίθεται χωριστά και δεν μετακινεί
    ποτέ entry (π.χ. καθυστερημένη δημοσίευση).
  - **Mode ρητό:** προεπιλογή SHADOW, ή LIVE· ανάμειξη μόνο με το ρητό `ALL_MODES` (εσωτερικό debugging).
  - **group_key:** `snapshot:<current snapshot id>` για B5, `discovery:<signal id>` για NEW_COMPANY,
    `signal:<id>` όταν η προέλευση δεν είναι COMPLETE. Δεν αποθηκεύεται τίποτα.
  - **Provenance:** COMPLETE / MISSING / INVALID / UNSUPPORTED· ένα σήμα με πρόβλημα **εμφανίζεται**
    χωρίς subject facts αντί να κρυφτεί ή να σπάσει το timeline.
  - Μόνο αναγνωριστικά (source ids, ΚΑΔ/έκδοση)· **καμία** επίλυση περιγραφών (ανεξάρτητο από A5).
  - **Keyset pagination** `(detected_at, id)` με opaque cursor· default 50, max 200. **2 queries** ανά
    σελίδα ανεξαρτήτως μεγέθους· ποτέ join στο `Company` (ούτε `raw_data`).
  - Κανένα URL/view/template/task/schedule. Αντίγραφο dev: 0 σήματα ⇒ 0 timeline events.
  - 34 νέα tests· **1.255 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`** (το B6 δεν έχει
    δική του migration· εξαρτάται από τις 0039–0043).

- **Gemi Leads 2.0 — B5: ανίχνευση αλλαγών snapshots και σήματα Tier-1 (2026-09-16).** Migration
  `0043_company_signal_snapshot_evidence`, `gemiapp/snapshot_change_signals.py`,
  `manage.py materialize_snapshot_change_signals`:
  - **Detector** (`detect_snapshot_changes`, καμία query/εγγραφή) και **materializer** χωριστά. Συγκρίνεται
    μόνο ο **άμεσος** προκάτοχος (observed_at, id). Baseline → κανένα σήμα. Διαφορετικό/μη υποστηριζόμενο
    schema version → `incompatible_snapshot_schema`, τίποτα δεν μαντεύεται.
  - **Ένα αλλαγμένο hash δεν είναι ποτέ σήμα από μόνο του**: οι κανόνες είναι field-aware.
  - **STATUS / LEGAL_FORM / LOCATION (δήμος):** μόνο known → διαφορετικό known· null ↔ known σιωπηλό.
    STATUS: effective = `lastStatusChange` μόνο αν VALID· οι άλλοι δύο: precision NONE. Νομός, πόλη, ΤΚ μόνα
    τους δεν είναι LOCATION_CHANGED.
  - **KAD:** ταυτότητα παρουσίας `(code, kad_version)`· τύπος/λεκτικό/περίοδος δεν μετράνε. Effective =
    η μία VALID ημερομηνία στην οποία συμφωνούν οι εγγραφές (dtFrom για add, dtTo για remove), αλλιώς NONE.
  - **Version quality:** ίδιος κωδικός null ↔ known version → καταστολή (όχι ψεύτικο remove+add).
  - **Μετάβαση ΚΑΔ 2008→2026** (`KAD_TAXONOMY_TRANSITION_DATE = 2026-03-01`): καταστέλλεται μόνο
    removal 2008 με dtTo ακριβώς 2026-03-01 όταν το τρέχον snapshot έχει εγγραφή 2026 με dtFrom 2026-03-01,
    και addition 2026 με dtFrom 2026-03-01 όταν το προηγούμενο έχει εγγραφή 2008 με dtTo 2026-03-01.
    Χωρίς crosswalk, περιγραφές ή fuzzy matching· άλλες ημερομηνίες δεν καταστέλλονται ποτέ.
  - **Ταυτότητα γεγονότος v1:** `{"transition": {from_state, to_state, observed_at UTC}, "subject": ...}` —
    rerun = ίδιο σήμα, A→B→A→B = ξεχωριστή δεύτερη εμφάνιση. `detected_at = current.observed_at`.
  - **Πάντα SHADOW**, SNAPSHOT_DIFF, confidence 1.0000· καμία παράμετρος mode, κανένα `--live`.
  - `CompanySignalSnapshotEvidence`: signal OneToOne (CASCADE), previous/current snapshot **PROTECT**,
    `subject_kind` + source ids ή (activity_code, kad_version)· καμία JSON, καμία περιγραφή.
  - Το B4 **δεν** καλεί τον detector· δεν υπάρχει task ούτε schedule.
  - Αντίγραφο dev βάσης: 0 snapshots ⇒ **0 transitions, 0 σήματα** (dry run και πραγματική εκτέλεση).
  - 62 νέα tests· **1.221 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**

- **Gemi Leads 2.0 — B4: refresh collector παρακολουθούμενων εταιρειών (2026-09-16).** Migration
  `0042_gemi_refresh_run`, `gemiapp/ingestion/refresh.py`, `manage.py run_gemi_company_refresh`:
  - **Αλυσίδα:** due monitoring (A9) → plan → GemiClient lane `MONITORED_REFRESH` (A1) → A2 → `observed_at`
    → A3 → snapshot (B3) → ενημέρωση check state (A9). **Κανένα σήμα**: το B4 ξέρει μόνο ότι «άλλαξε η
    κατάσταση» — το *τι* άλλαξε ανήκει στο B5.
  - **Planner χωρίς δίκτυο** (`build_company_refresh_plan`): με ~7 αιτήματα/λεπτό συνολικά, ένα detail
    αίτημα ανά εταιρεία είναι αδύνατο. Προτεραιότητα: (1) Radar search, (2) direct detail μόνο για
    ACTIVE_OPPORTUNITY / RECENT_SIGNAL, (3) `deferred_no_efficient_strategy`.
  - **Μετάφραση Radar → GEMI query:** μόνο κριτήρια που μεταφράζονται ακριβώς (8ψήφιοι ΚΑΔ, ids νομών και
    νομικών μορφών από το A5, `isActive`). Ό,τι δεν μεταφράζεται **πέφτει**, άρα το query μόνο ευρύνεται
    και καμία due εταιρεία δεν χάνεται. Το `name` δεν στέλνεται ποτέ (άγνωστη σημασιολογία upstream).
  - **Merge μόνο στο `activities`** (OR μέσα στο ίδιο κριτήριο)· καμία καρτεσιανή συγχώνευση δύο διαστάσεων.
  - **Πύλη αποδοτικότητας:** εκτίμηση σελίδων από **τοπικά** δεδομένα· μια ομάδα γίνεται δεκτή μόνο αν
    χωράει στο `GEMI_REFRESH_MAX_PAGES_PER_QUERY` **και** κοστίζει λιγότερα αιτήματα από τους στόχους της.
  - **Η αναζήτηση είναι μόνο μηχανισμός λήψης:** εταιρείες που επιστρέφονται χωρίς να είναι due στόχοι
    αγνοούνται πλήρως. Καμία εγγραφή σε RadarMatch, κριτήρια Radar, λόγους monitoring ή leads.
  - **Η απουσία δεν είναι τεκμήριο:** στόχος που λείπει από **πλήρη** αναζήτηση είναι `search_miss`,
    παραμένει due και **δεν** κλιμακώνεται αυτόματα σε detail. Σε ημιτελή ομάδα δεν μετράει καν ως miss.
  - **Χρόνος:** ένα `run_at` για την επιλογή due· κάθε HTTP απάντηση έχει δικό της `observed_at`
    (`as_of` = τοπική ημερομηνία του). Το ρολόι είναι injectable για τα tests.
  - **A6:** μόνο `first_seen_at` (ποτέ προς τα εμπρός) και `last_seen_at`. Το `last_synced_at` μένει
    **άθικτο**: το B4 δεν κάνει cutover της canonical αποθήκευσης. Καμία legacy στήλη δεν γράφεται.
  - **Αντίγραφο dev βάσης (plan-only, ΜΗΔΕΝ δίκτυο):** στις 2026-09-16 09:00Z **0 due**· στις
    2026-09-24 09:00Z **2.564 due** → **όλες deferred, 0 αιτήματα**: τα dev Radars δεν μεταφράζονται
    (κενοί A5 πίνακες, 4ψήφιοι ΚΑΔ, ένα Radar χωρίς κριτήρια). Εξολοκλήρου αναμενόμενο, όχι σφάλμα.
  - **Δεν είναι στο `apps.SCHEDULES`** και δεν υπάρχει flag: ασφάλεια = καμία χρονοδρομολόγηση +
    ρητή κλήση + G0/G1.
  - 78 νέα tests· **1.159 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**

- **Gemi Leads 2.0 — B3: ελαχιστοποιημένα snapshots εταιρειών (2026-09-16).** Migration
  `0041_company_snapshot`, `gemiapp/company_snapshots.py`:
  - `CompanySnapshot`: η κανονική **επιχειρηματική κατάσταση** μιας εταιρείας σε μία παρατήρηση — όχι το
    payload του ΓΕΜΗ, όχι το `raw_data`, όχι σήμα. Χτίζεται **μόνο** από την A3 `NormalizedCompany`.
  - **Κατάσταση v1 (όλα μπαίνουν στο hash):** status id, lastStatusChange + ποιότητα, legal type id,
    GEMI office id, νομός/δήμος/πόλη/ΤΚ, ημερομηνία σύστασης + ποιότητα, τρέχουσες δραστηριότητες και
    `unknown_current_activity_count`. **Ταυτότητα με source ids, ποτέ περιγραφές**: αλλαγή λεκτικού δεν
    είναι αλλαγή κατάστασης.
  - **Τρέχουσες δραστηριότητες:** A3/A7 `is_current` — True μέσα, False έξω, **None ποτέ** ως
    επιβεβαιωμένα τρέχουσα (μετριέται χωριστά). **Δεν** εφαρμόζεται ο περιορισμός KAD-2026 του A7: μια
    τρέχουσα ΚΑΔ 2008 παραμένει και η έκδοση ΚΑΔ είναι μέρος ταυτότητας, γιατί το B5 χρειάζεται αυτό το
    τεκμήριο για να ξεχωρίσει πραγματική αλλαγή από τη μετάβαση 2008→2026.
  - **`state_hash`:** SHA-256 πάνω σε canonical JSON (ταξινομημένα κλειδιά, compact separators, ISO
    ημερομηνίες, χωρίς floats). **Κανένα metadata** δεν συμμετέχει (id, observed_at, created_at, source
    record, εκδόσεις, baseline)· ίδια κατάσταση σε άλλη στιγμή ⇒ ίδιο hash.
  - **Writer:** πρώτη κατάσταση → baseline· ίδια κατάσταση → **καμία** νέα γραμμή, μόνο το
    `last_observed_at` προχωρά· διαφορετική → νέα μη-baseline γραμμή. Παρατήρηση παλαιότερη από το τρέχον
    διάστημα **απορρίπτεται**. Το `observed_at` δίνεται πάντα ρητά (καμία κρυφή `now()`).
  - **A → B → A δίνει 3 γραμμές**: δεν υπάρχει unique (company, state_hash), ώστε η επιστροφή σε
    προηγούμενη κατάσταση να μένει ορατή.
  - Καμία backfill από `raw_data` (θα ήταν πλαστό baseline), κανένα task, κανένα σήμα. Το B4 θα φέρνει
    φρέσκες παρατηρήσεις και θα καλεί αυτόν τον writer. *(Αναθεώρηση 2026-09-19: εξαίρεση μόνο για το
    detection-time baseline μιας εταιρείας που υλοποιείται ως NEW_COMPANY, από επικυρωμένη, ταυτοποιημένη,
    A6-χρονοσημασμένη εγγραφή του importer — βλ. «NEW_COMPANY: κατάσταση τη στιγμή της ανίχνευσης».)*
  - Αντίγραφο dev βάσης: **0 snapshots** μετά τη migration· η προσομοίωση fixture (A→A→B→B→A) έδωσε 3
    γραμμές με ίδιο hash A1/A2 και εξαφανίστηκε με το reverse.
  - **Δύο πεδία τύπου δραστηριότητας, σκόπιμα:** το `activity_type` είναι η canonical A7 τιμή (μέσω `normalize_kad_search`: χωρίς τόνους, κεφαλαία)· το `source_activity_type` είναι η A3 `normalize_text`, που κάνει NFC και
    συμπτύσσει κενά αλλά **διατηρεί πεζά/κεφαλαία και τόνους**. Άρα διαφορά μόνο σε κενά/μορφή Unicode δεν αλλάζει την κατάσταση,
    ενώ διαφορά μόνο σε πεζά/κεφαλαία ή τόνους **δίνει νέα γραμμή κατάστασης** με αμετάβλητο canonical type· το B3
    καταγράφει το γεγονός, η κρίση ανήκει στο B5.
  - 33 νέα tests· **1.081 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**
- **Gemi Leads 2.0 — B2: παραγωγός σημάτων NEW_COMPANY (2026-09-16).** Migration
  `0040_company_signal_discovery_evidence`, `gemiapp/new_company_signals.py`,
  `manage.py materialize_new_company_signals`:
  - **Σημασία:** NEW_COMPANY = «το Gemi Leads είδε για πρώτη φορά αυτή την εταιρεία», με βάση τα ευρήματα του
    Discovery v2. **Δεν** σημαίνει «σύσταση σήμερα»: μια καθυστερημένη δημοσίευση είναι εξίσου έγκυρο
    NEW_COMPANY.
  - **Επιλέξιμα ευρήματα:** παρατηρήσεις A10 με κατάταξη `new_incorporation`, `late_publication`,
    `invalid_date` (όλες σημαίνουν «δεν υπήρχε τοπικά»). Οι `known` **ποτέ**. *(Αναθεώρηση 2026-09-20: οι τρεις
    κατατάξεις σημαίνουν πλέον «το Discovery είδε πρώτη φορά αυτό το αναγνωριστικό» — πάνω από το σύνορο, ή
    χωρίς τοπική γραμμή στο/κάτω από αυτό. Το αν υπήρχε `Company` **δεν** κρίνει την επιλεξιμότητα· ο B2 δεν
    διαβάζει καν το `company_existed`.)*
  - **Ταυτότητα:** σταθερό `event_key = {"event": "first_observed"}` → ένα γεγονός ανά εταιρεία για πάντα·
    εκτός ταυτότητας: ημερομηνία σύστασης, run/observation id, detected_at, κατάταξη, mode, έκδοση κανόνα.
  - **`detected_at`** = `started_at` του **παλαιότερου** run με επιλέξιμη παρατήρηση (οι παρατηρήσεις έχουν
    μόνο `created_at`, που είναι η στιγμή μαζικής εγγραφής στο τέλος του run). Ποτέ η ώρα της εντολής, ποτέ
    το `Company.imported_at`· επανεκτέλεση δεν το μετακινεί. *(Αναθεώρηση 2026-09-19: αυτός είναι πλέον ο χρόνος
    παρατήρησης discovery· το `detected_at` του σήματος = max(αυτός, observed_at του baseline).)*
  - **`effective_date`** = η ημερομηνία σύστασης μόνο όταν η A3 ποιότητα είναι `valid` (ακρίβεια DATE, χωρίς
    πλασματική ώρα)· αλλιώς **καμία** ώρα πηγής (NONE) — η ημερομηνία ανίχνευσης δεν την αντικαθιστά ποτέ.
  - **Εταιρεία που δεν υπάρχει ακόμη** (φυσιολογικό στο shadow): καμία δημιουργία Company, κανένα σήμα· η
    παρατήρηση μένει ως εκκρεμές τεκμήριο και υλοποιείται στην επόμενη εκτέλεση, με το αρχικό `detected_at`.
  - **Προέλευση:** `CompanySignalDiscoveryEvidence` (σήμα ↔ πρώτη επιλέξιμη παρατήρηση, PROTECT)· δεν
    αντικαθίσταται ποτέ από μεταγενέστερη παρατήρηση.
  - **Πάντα SHADOW.** Δεν υπάρχει παράμετρος, flag ή επιλογή εντολής που να παράγει LIVE· καμία σύνδεση με
    Radars, digests, monitoring ή ειδοποιήσεις. Ο κανόνας `new_company:v1` είναι πλέον `implemented`.
  - **Αντίγραφο dev βάσης: 0 παρατηρήσεις Discovery** → 0 υποψήφιες, 0 σήματα (καμία κατασκευή ιστορικού).
  - 23 νέα tests· **1.048 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**
- **Gemi Leads 2.0 — B1: θεμέλιο Signals (2026-09-16).** Πρώτο πακέτο του Stage B. Migration
  `0039_company_signal`, `gemiapp/company_signals.py`:
  - `CompanySignal`: ένα επιχειρηματικό γεγονός ανά εταιρεία — **γεγονός συστήματος, όχι πελάτη** (καμία
    σχέση με χρήστη). Πεδία: company (PROTECT), signal_type, source_type, rule_version, dedupe_key (unique),
    confidence (decimal 0–1 με db constraint), mode (shadow/live), effective_date/effective_at/
    effective_precision, detected_at, created_at/updated_at. **Κανένα payload, καμία PII.**
  - **Ταυτότητα γεγονότος:** SHA-256 πάνω σε (έκδοση κλειδιού, τύπος, αριθμός ΓΕΜΗ, `event_key` του
    producer). **Εκτός** ταυτότητας: pk, detected_at, job id, τυχαίες τιμές, mode και rule_version — ώστε
    retry, επανεπεξεργασία, νέα έκδοση κανόνα και προαγωγή shadow→live να μη διπλασιάζουν το γεγονός.
  - **Registry κανόνων** (`SIGNAL_RULES`): ένα σημείο για τα `name:vN`. Τύπος χωρίς κανόνα = ταξινομία μόνο
    και **δεν καταγράφεται**. Μόνο `new_company:v1` είναι δηλωμένος (υλοποίηση στο B2).
  - **Χρόνος:** `detected_at` = πότε το είδε το Gemi Leads (δεν ξαναγράφεται ποτέ). Ο χρόνος της πηγής
    κρατιέται στην ακρίβεια που δίνει η πηγή — ημερομηνία ως ημερομηνία, **ποτέ πλασματικά μεσάνυχτα** —
    με db constraint που επιβάλλει τη συνέπεια precision/πεδίων.
  - **Shadow/Live** ρητά, με explicit `promote_company_signal` (τίποτα δεν προάγει αυτόματα).
  - **Κανένας producer, κανένα schedule, μηδέν γραμμές μετά τη migration.** Τα Signals **δεν** συνδέονται με
    Radars/digests/ειδοποιήσεις — αυτό ξεκινά στο B2.
  - 27 νέα tests· **1.025 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**
- **Gemi Leads 2.0 — A10: Discovery v2 σε shadow (2026-09-16).** Τελευταίο πακέτο του Stage A. Migration
  `0038_gemi_discovery`, `gemiapp/ingestion/discovery.py`, `manage.py run_gemi_discovery_v2`,
  `manage.py bootstrap_gemi_discovery_v2`, `run_gemi_discovery_v2_shadow_task` (**όχι** στο `apps.SCHEDULES`):
  - **Το πρόβλημα:** ο σημερινός importer κρατά μόνο όσες εταιρείες έχουν `incorporationDate` == ημέρα-στόχο,
    οπότε χάνει οριστικά τις καθυστερημένες δημοσιεύσεις (το spike είδε 2026-09-09 και 2025-12-15 ανάμεσα
    στις νεότερες εγγραφές). Το Discovery v2 σελιδοποιεί με `-arGemi` και ψάχνει ό,τι είναι πέρα από το
    σύνορο (high-water mark) που έχει ήδη παρατηρηθεί.
  - **Shadow:** ο legacy importer παραμένει η μοναδική πηγή εταιρειών, digests και matching. Το shadow
    γράφει **μόνο** στους πίνακες discovery· καμία Company/CompanyActivity, κανένα monitoring, κανένα Signal.
    Το ingest mode απαιτεί `GEMI_DISCOVERY_V2_ENABLED` (=0) και δεν είναι προγραμματισμένο.
  - **Μοντέλα:** `GemiDiscoveryCursor` (σύνορο, status, bootstrap provenance, αποτυχίες, ανωμαλίες),
    `GemiDiscoveryRun` (πλήθη, σύνορο πριν/μετά, stop reason, anomalies, policy), `GemiDiscoveryObservation`
    (ανά εταιρεία: αναγνωριστικό, ημερομηνία σύστασης και ποιότητα A3, κατάταξη). Μόνο ids, ημερομηνίες και
    πλήθη — κανένα payload, καμία PII. Το `ImportRun` δεν αλλάζει.
  - **Guardrails:** κάθε εκτέλεση μετρά ordering εντός σελίδας και στα όρια σελίδων, διπλότυπα και άκυρα
    αναγνωριστικά. Παραβίαση σειράς = **anomaly**: ο cursor **δεν** προχωρά και μένει για έλεγχο. Ο cursor
    προχωρά μόνο σε επιτυχή εκτέλεση (ικανοποιημένο overlap, χωρίς blocking anomaly, χωρίς page limit).
  - **Bootstrap:** ποτέ `cursor = MAX(local gemi_number)`. Επαληθευμένη σάρωση με ελάχιστο πλήθος γνωστών
    εγγραφών· σύνορο = ο υψηλότερος αριθμός ΓΕΜΗ που **υπάρχει ήδη τοπικά**, ώστε τίποτα από πάνω του να μη
    μείνει κρυφό (το backlog καταγράφεται).
  - **Σύγκριση legacy/v2** ανά ημέρα: BOTH / LEGACY_ONLY / V2_ONLY με αιτία (late_publication,
    invalid_incorporation_date, legacy_filter_miss). Καταγράφονται και οι ήδη γνωστές εγγραφές που είδε η
    σάρωση, ώστε το LEGACY_ONLY να σημαίνει πραγματικά «το v2 δεν έφτασε ποτέ σε αυτήν».
  - **Πύλη cutover:** ≥14 ημέρες shadow, έλεγχος διαφορών, καμία ανεξήγητη ανωμαλία σειράς, αποδεκτό budget
    αιτημάτων και ποσοστά απωλειών/διπλοτύπων. Τίποτα δεν το ενεργοποιεί αυτόματα.
  - *(Αναθεώρηση 2026-09-20: η κατάταξη «νέο εύρημα vs known» δεν εξαρτάται πια από την ύπαρξη `Company` τη
    στιγμή της σάρωσης αλλά από το σύνορο στην αρχή του run· το `company_existed` γίνεται διαγνωστικό. Βλ.
    «Discovery v2: η “καινούρια” εταιρεία κρίνεται από το σύνορο».)*
  - 30 νέα tests (πολυήμερη προσομοίωση με fixtures: καθυστερημένη δημοσίευση, εκτός σειράς εγγραφή,
    διπλή σελίδα, αποτυχία στη μέση)· **998 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**
- **Gemi Leads 2.0 — A9: σύνολο παρακολούθησης εταιρειών και πολιτική ανανέωσης (2026-09-16).** Migration
  `0037_company_monitoring`, `gemiapp/ingestion/monitoring.py`, `manage.py recompute_gemi_company_monitoring`,
  `recompute_gemi_company_monitoring_task` (**όχι** στο `apps.SCHEDULES`):
  - `CompanyMonitoring`: μία γραμμή ανά εταιρεία (κοινή για όλους τους πελάτες) με state
    active/decaying/inactive, priority, primary_reason, `next_check_at` και πεδία collector
    (`last_checked_at` κ.λπ.) που **δεν γράφονται** από τον υπολογισμό. `CompanyMonitoringReason`: μία γραμμή
    ανά εταιρεία+λόγο με ιστορικό (first_active_at, activated_at, deactivated_at, activation_count),
    expires_at και source_ids (π.χ. ids Radars). Κανένα προσωπικό δεδομένο.
  - Λόγοι: **NEW_COMPANY** (A6 `first_seen_at` εντός 30 ημερών και έγκυρη ημερομηνία σύστασης όχι παλαιότερη
    των 30 ημερών από την πρώτη παρατήρηση· αποκλείει το bulk import παλιών εταιρειών), **ACTIVE_RADAR_MATCH**
    (τα Radars και το predicate του ζωντανού matcher, με το cutoff `monitor_from`), **MANUAL** (μόνο service,
    χωρίς UI). **ACTIVE_OPPORTUNITY** και **RECENT_SIGNAL**: δεσμευμένα, δεν συμπληρώνονται (το
    `UserCompanyLead` δεν είναι opportunity· Signals δεν υπάρχουν).
  - Πολιτική (ένα σημείο, `MonitoringPolicy`): opportunity critical, signal high, radar high, new normal,
    manual normal (δεν υπερισχύει)· decaying low. Ανανέωση: 1/3/7/30 ημέρες. Χωρίς λόγο: decaying για 30
    ημέρες, μετά inactive (το ιστορικό μένει). `next_check_at` ντετερμινιστικό (SHA-256 jitter ανά εταιρεία).
  - **Ο importer δεν γράφει ακόμη το A6 `first_seen_at`**: πριν από recompute τρέξε
    `backfill_gemi_company_metadata` μέχρι να το κάνει η canonical ingestion.
  - Αντίγραφο dev βάσης (as_of 2026-09-16 09:00 UTC): 2.564 παρακολουθούμενες από 17.799· NEW_COMPANY 2.554,
    ACTIVE_RADAR_MATCH 224, πολλαπλοί λόγοι 214· high 224 / normal 2.340· δεύτερος υπολογισμός 0 αλλαγές·
    matching, previews, dashboard, επιλογέας ΚΑΔ ίδια πριν/μετά.
  - 28 νέα tests· **968 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**
- **Gemi Leads 2.0 — A8: συμφιλίωση καταλόγου ΚΑΔ (2026-09-15).** Migration `0036_activitycode_kad_links`,
  `gemiapp/ingestion/kad_catalogue.py`, `manage.py reconcile_gemi_kad_catalogue`:
  - Το `ActivityCode` (ζωντανός κατάλογος: επιλογέας ΚΑΔ, κριτήρια Radars, fallback του importer) **δεν
    αλλάζει**. Νέος πίνακας-γέφυρα `ActivityCodeKadLink`: σύνδεση με κάθε `GemiKad` (A5) με ακριβώς τον ίδιο
    κωδικό, μία ανά έκδοση ΚΑΔ· η περιγραφή καταγράφεται μόνο ως τεκμήριο (`description_matches`).
  - Κατάταξη ανά ΚΑΔ: `kad_2026` / `kad_2008_only` / `both_versions` / `other_version_only` / `unresolved`
    (αιτία: `no_reference_data`, `not_in_reference`, `retired_in_reference`). Read-only κατάταξη και των
    αποθηκευμένων κριτηρίων Radars· **τίποτα δεν ξαναγράφεται ή διαγράφεται**.
  - Flag **`GEMI_KAD_PICKER_CURRENT_TAXONOMY_ONLY=0`** (default): ο επιλογέας ΚΑΔ ψάχνει όλο το `ActivityCode`
    όπως πριν· με 1 μόνο όσα συνδέονται με παρόντα ΚΑΔ 2026. Το `GEMI_MATCH_CURRENT_ACTIVITIES_ONLY` μένει 0.
  - Crosswalk ΚΑΔ 2008 → 2026: **unresolved** (το ΓΕΜΗ δεν δημοσιεύει· τίποτα δεν συμπεραίνεται). Ιεραρχία
    ΚΑΔ: **δεν παράγεται** (π.χ. το 01.00.00.00 είναι «ΑΓΡΟΤΗΣ ΕΙΔΙΚΟΥ ΚΑΘΕΣΤΩΤΟΣ», όχι το τμήμα 01).
  - **Αντίγραφο dev βάσης: `GemiKad` άδειο** (το A5 sync δεν έχει τρέξει ποτέ ζωντανά) → όλα τα 10.467
    `unresolved/no_reference_data`, 0 σύνδεσμοι. Τεκμήρια: 816 fallback ΚΑΔ, από τους οποίους 783 εμφανίζονται
    στις εταιρείες μόνο ως ΚΑΔ 2008· τα 4 κριτήρια Radars είναι demo τετραψήφιοι κωδικοί (52.29, 62.01,
    43.21, 56.10). Επιλογέας, κατάλογος, κριτήρια, matching, previews, dashboard ίδια πριν/μετά.
  - 23 νέα tests· **940 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**
- **Gemi Leads 2.0 — A7: κανονικά μεταδεδομένα `CompanyActivity` και ασφαλές upsert (2026-09-15).**
  Migration `0035_companyactivity_canonical_metadata`, `gemiapp/ingestion/activities.py`,
  `manage.py backfill_gemi_company_activities`, `manage.py report_gemi_activity_matching_parity`:
  - Νέα πεδία: `activity_type_normalized` (primary/secondary/auxiliary/other/unknown· το `activity_type`
    μένει όπως δημοσιεύεται), `kad_version` (όπως δημοσιεύεται), `date_from`/`date_to` με
    `date_from_quality`/`date_to_quality` (A3), `is_current` με `current_as_of` (ημέρα της παρατήρησης
    ΓΕΜΗ, όχι «σήμερα»), `source_key`, `in_latest_source`, `legacy_listed`.
  - Ταυτότητα: (εταιρεία, `source_key`) = hash(κωδικός, έκδοση ΚΑΔ, τύπος, dtFrom). Οι ΚΑΔ 2008/2026 και
    οι περίοδοι του ίδιου κωδικού είναι ξεχωριστές γραμμές· το dtTo και η περιγραφή ενημερώνονται στη θέση
    τους. Ο παλιός unique (εταιρεία, κωδικός, τύπος) ισχύει πλέον μόνο για `legacy_listed` γραμμές.
  - **Ο importer δεν κάνει πια delete/recreate**: diff/upsert, σταθερά primary keys, καμία διαγραφή·
    δραστηριότητες που εξαφανίζονται κρατιούνται με `legacy_listed=False`.
  - **`legacy_listed`** = ακριβώς οι γραμμές που θα κρατούσε ο παλιός importer. Όλα τα legacy reads
    (Radar matching, radar preview, dashboard φίλτρο ΚΑΔ, καρτέλα εταιρείας, Superadmin φίλτρα/μετρητής)
    φιλτράρουν σε αυτό, οπότε οι επιπλέον κανονικές γραμμές δεν αλλάζουν τίποτα ορατό.
  - Flag **`GEMI_MATCH_CURRENT_ACTIVITIES_ONLY=0`** (default). Με 1: συμμετέχουν μόνο `is_current=True`,
    `in_latest_source=True`, `kad_version=kad_2026`· άγνωστη τρέχουσα κατάσταση ή μη συμφιλιωμένες
    γραμμές δεν συμμετέχουν. **Δεν ενεργοποιείται.**
  - Αντίγραφο dev βάσης: 119.564 δραστηριότητες πηγής, 119.211 legacy γραμμές συμφιλιώθηκαν στη θέση
    τους, 353 νέες γραμμές (άλλη έκδοση/περίοδος), 4 μη συμφιλιωμένες (demo) κρατήθηκαν, 0 διαγραφές·
    δεύτερο backfill και replay του importer 0 αλλαγές. Legacy-visible γραμμές, matching, previews,
    dashboard, matches, leads ίδια πριν/μετά. Προσομοίωση Radar ενός ΚΑΔ με flag=1: −2.230 ζεύγη
    (εταιρεία, ΚΑΔ) (−1,87%), 514 εταιρείες, 1.335 κωδικοί (812 θα έμεναν χωρίς εταιρεία).
  - Το reverse της 0035 είναι αυτόνομο: ένα reverse-only βήμα της ίδιας της migration διαγράφει μόνο τις
    παράγωγες γραμμές `legacy_listed=False` (ξαναφτιάχνονται με backfill), κρατά κάθε legacy-visible γραμμή
    και επαναφέρει τον παλιό unique. Κανένα χειροκίνητο βήμα.
  - 41 νέα tests (μαζί με εκτελούμενο test forward → backfill → reverse → reapply)· **917 tests OK**.
    **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**
- **Gemi Leads 2.0 — A6: κωδικοί αναφοράς και lifecycle στο `Company` (2026-09-15).** Migration
  `0034_company_gemi_metadata` (μόνο 9 nullable `AddField`, χωρίς FK, χωρίς indexes),
  `gemiapp/ingestion/company_metadata.py`, `manage.py backfill_gemi_company_metadata`:
  - Νέα πεδία: `status_source_id`, `legal_type_source_id`, `gemi_office_source_id`,
    `prefecture_source_id`, `municipality_source_id` (το `id` του αντικειμένου στο `raw_data`, με τον
    κανόνα αναγνωριστικών A2/A3· ποτέ από περιγραφή), `incorporation_date_quality` (A3 `DateQuality` της
    ημερομηνίας της πηγής· το `incorporation_date` δεν αλλάζει), `first_seen_at`, `last_seen_at`,
    `last_synced_at`. **Καμία υπάρχουσα λειτουργία δεν τα διαβάζει**· importer, Radars, digests,
    αναζήτηση και exports αμετάβλητα. Το `is_active` **δεν** διορθώθηκε.
  - Lifecycle: `first_seen_at` ← `imported_at`, `last_seen_at` ← `updated_at`, **μόνο** όταν το
    `raw_data` είναι εγγραφή ΓΕΜΗ της ίδιας εταιρείας (`arGemi` = `gemi_number`) και δεν υπάρχει
    προσθήκη/αλλαγή στο `django_admin_log`· αλλιώς null. Το `last_synced_at` μένει null μέχρι να υπάρξει
    canonical ingestion. Ποτέ `now()`, ποτέ ψεύτικες προεπιλογές.
  - Backfill: batches ανά pk (`--batch-size`, `--start-id`, `--dry-run`), συναλλαγή ανά batch,
    `bulk_update` μόνο στα νέα πεδία (το `updated_at` δεν αγγίζεται), ποτέ δεν σβήνει τιμή, idempotent,
    μετράει τα χαλασμένα rows χωρίς να σταματά, αφήνει τα σφάλματα βάσης να σταματήσουν την εκτέλεση.
    Εκτυπώνει μόνο πλήθη· δεν καλεί το ΓΕΜΗ.
  - **Ο importer δεν συμπληρώνει ακόμη τα νέα πεδία**: εταιρείες που εισάγονται μετά το backfill μένουν
    null μέχρι νέο backfill ή canonical ingestion.
  - Αντίγραφο dev βάσης (17.799 εταιρείες): 17.789 συμπληρώθηκαν (τα 10 demo rows έμειναν null), 8
    χωρίς νομό/δήμο, ποιότητα ημερομηνίας valid=17.789, 0 row errors, δεύτερη εκτέλεση 0 αλλαγές,
    forward/backfill/reverse/reapply με τους 46 προϋπάρχοντες πίνακες αμετάβλητους και parity report
    ίδιο πριν/μετά. 25 νέα tests· **876 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**
- **Gemi Leads 2.0 — A5: GEMI reference tables και sync (2026-09-15).** Migration
  `0033_gemi_reference_data`, `gemiapp/ingestion/reference_data.py`, `manage.py sync_gemi_reference_data`:
  - Επτά νέοι πίνακες αναφοράς, **ξεχωριστοί** από το `ActivityCode` και τα strings των Company/Radars
    (τίποτα στη ζωντανή εφαρμογή δεν τους διαβάζει ακόμη): `GemiKad` (ταυτότητα = κωδικός + `kad_version`,
    οι ΚΑΔ 2008 και 2026 δεν συγχωνεύονται), `GemiPrefecture`, `GemiMunicipality` (το upstream
    `prefectureId` ως απλό αναγνωριστικό, χωρίς foreign key, λόγω της ασυμφωνίας Αττικής 52–55),
    `GemiCompanyStatus` (με το `isActive` της πηγής), `GemiLegalType`, `GemiOffice` (χωρίς διεύθυνση και
    στοιχεία επικοινωνίας), `GemiDecisionSubject`, και `GemiReferenceSyncRun` για καταγραφή εκτελέσεων.
  - Sync: fetch και A2 validation και των 7 endpoints μέσω του κοινού GemiClient (χαμηλότερο lane) →
    υπολογισμός αλλαγών στη μνήμη → **μία συναλλαγή** για όλες τις οικογένειες. Αποτυχία σε οποιοδήποτε
    endpoint ή στη βάση = καμία αλλαγή. Όσα εξαφανίζονται από την πηγή δεν διαγράφονται
    (`is_present=False`, `retired_at`) και επανέρχονται στην ίδια γραμμή.
  - Αν μια οικογένεια επιστρέψει κενή ή με λιγότερο από το μισό πλήθος, γίνεται warning και δεν
    αποσύρεται τίποτα (εκτός με `--force-retire`). `--dry-run` χωρίς καμία εγγραφή (ούτε source records).
  - Λειτουργεί με `GEMI_SOURCE_RECORDS_ENABLED=0`· με 1 καταγράφει και provenance. Το
    `sync_gemi_reference_data_task` **δεν** έχει μπει στο `apps.SCHEDULES`.
  - Migration δοκιμάστηκε σε αντίγραφο της dev βάσης (forward → reverse → forward): όλοι οι
    προϋπάρχοντες πίνακες ίδιοι· προστέθηκαν μόνο τα 8 content types και 32 permissions των νέων μοντέλων.
    26 νέα tests· **851 tests OK**. **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**

- **Gemi Leads 2.0 — A4: minimised GEMI source records (2026-09-15).** Νέο μοντέλο `GemiSourceRecord`
  (migration `0032_gemi_source_records`) και `gemiapp/ingestion/source_records.py`:
  - **Feature flag `GEMI_SOURCE_RECORDS_ENABLED`, προεπιλογή `0`**: όσο είναι 0 δεν γράφεται τίποτα και
    ο importer συμπεριφέρεται ακριβώς όπως πριν. Όταν γίνει 1, κάθε επικυρωμένη επιτυχής απάντηση
    καταγράφεται μετά το A2 και πριν φτάσει στον importer, και η καταγραφή είναι υποχρεωτική: αν
    αποτύχει, η εισαγωγή σταματά (`GemiSourceRecordError`) πριν γραφτεί εταιρεία.
  - Αποθηκεύονται μεταδεδομένα (endpoint, canonical allow-listed παράμετροι, fetch time, HTTP status,
    gateway request id, versions), SHA-256 του πλήρους JSON σώματος (υπολογισμένο στη μνήμη, με
    ταξινομημένα keys) και sanitised payload **μόνο με ρητό opt-in**
    (`GEMI_SOURCE_RECORDS_STORE_PAYLOAD`, προεπιλογή `0` όσο ο όγκος production είναι άγνωστος): για
    εταιρείες η εγγραφή του A3 χωρίς
    `name`/`street`/`street_number`. Ποτέ persons, email, phone, fax, afm, API key ή headers.
  - Retries στο ίδιο παράθυρο (1 ώρα) δεν δημιουργούν διπλές εγγραφές (unique `observation_key`)·
    ίδιο payload σε επόμενο παράθυρο = νέα παρατήρηση.
  - Retention short/standard/audit = 7/30/365 ημέρες (τεχνικές προεπιλογές, όχι νομική πολιτική)·
    `manage.py purge_gemi_source_records` (batches, `--dry-run`, logs μόνο πλήθη)· το
    `purge_gemi_source_records_task` **δεν** έχει μπει στο `apps.SCHEDULES`.
  - Read-only Django admin. Καμία σύνδεση με χρήστες/οργανισμούς (system provenance).
  - Migration δοκιμάστηκε σε αντίγραφο της dev βάσης: forward → reverse → forward, 35/37 πίνακες
    ίδιοι σε όλες τις καταστάσεις, στους άλλους δύο προστέθηκαν μόνο το content type και τα 4
    permissions του νέου μοντέλου. 27 νέα tests· **825 tests OK**.
  - **`PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`.**

- **Gemi Leads 2.0 — A3: Normaliser v1 (2026-09-15).** `gemiapp/ingestion/normalizer.py`
  (`GEMI_NORMALIZER_VERSION = 1`, `normalize_company(record, *, as_of)`): καθαρή, ντετερμινιστική
  συνάρτηση μόνο με standard library (χωρίς Django/βάση/δίκτυο/env/ρολόι) που μετατρέπει μια
  επικυρωμένη (A2) εγγραφή εταιρείας σε `NormalizedCompany` (frozen dataclasses):
  - ids και περιγραφές reference data χωριστά· null/κενή περιγραφή → `None`, ποτέ το κείμενο "None"·
  - ημερομηνίες με `DateQuality` (valid / missing / invalid / out_of_range)· εύρος στο `DatePolicy`:
    από 1830-01-01, γεγονότα έως `as_of` + 1 ημέρα, όρια περιόδων δραστηριοτήτων έως 2100-12-31·
    καμία ημερομηνία δεν αντικαθίσταται με τη σημερινή·
  - δραστηριότητες με κωδικό, περιγραφή, τύπο, `kad_version`, `dtFrom`, `dtTo` και `is_current`
    (χωρίς dtTo ή dtTo μετά το `as_of` → τρέχουσα)· KAD 2008 και 2026 δεν συγχωνεύονται·
    ντετερμινιστική ταξινόμηση και αφαίρεση πανομοιότυπων·
  - εξαιρούνται persons, phone, fax, email, url, poBox, afm, objective, capital, stocks, branch κ.ά.·
    `name`, `street`, `street_number` σημειώνονται `not_approved` για ιστορικές δομές·
  - `is_active` μόνο όταν το δηλώνει η πηγή, αλλιώς `None`.
  - **Καμία αλλαγή σε importer, Radars, digests ή billing** — ο normaliser δεν καλείται ακόμη.
  - 36 νέα tests (`gemiapp/test_gemi_normalizer.py`)· **798 tests OK**· καμία migration.

- **Gemi Leads 2.0 — A2: επικύρωση σχήματος απαντήσεων ΓΕΜΗ (2026-09-15).** Κάθε επιτυχής απάντηση
  που καταναλώνει η εφαρμογή ελέγχεται πριν φτάσει στον importer, με ρητά, versioned contracts στο
  `gemiapp/ingestion/schemas.py` (`GEMI_RESPONSE_SCHEMA_VERSION = 1`):
  - families: company search (wired στο `search_companies`), company detail και 7 reference-data
    families (activities, prefectures, municipalities, company statuses, legal types, GEMI offices,
    decision subjects) — οι τελευταίες έτοιμες για τους μελλοντικούς collectors, χωρίς νέο job·
  - ελέγχονται μόνο πεδία από τα οποία εξαρτάται η εφαρμογή (π.χ. `arGemi`, `searchResults`,
    `activities[].activity.id`, ημερομηνίες, reference ids)· άγνωστα πεδία αγνοούνται, nulls
    επιτρέπονται όπου τεκμηριώθηκαν/παρατηρήθηκαν·
  - αποτυχία → `GemiResponseValidationError` (invalid_structure / missing_field / wrong_type /
    invalid_container), χωρίς retry, με endpoint, family, schema version, location, request id —
    ποτέ τιμές payload. Log σε ERROR (Sentry event όπου υπάρχει `SENTRY_DSN`) και
    `ImportRun.status=failed` με το μήνυμα.
  - Καμία εγγραφή εταιρείας από μη έγκυρη σελίδα: το `fetch_companies` επικυρώνει κάθε σελίδα πριν
    γραφτεί οτιδήποτε· στο backfill μένουν μόνο οι προηγούμενες έγκυρες σελίδες.
  - 29 νέα tests (`gemiapp/test_gemi_validation.py`)· **762 tests OK**· καμία migration.

- **Gemi Leads 2.0 — A1: κοινός GemiClient (2026-09-15, branch `feature/gemi-2-a1-gemi-client`,
  βάση `main` + `chore/norva-legal-identity`).** Κάθε κλήση στο ΓΕΜΗ περνά πλέον από το
  `gemiapp/ingestion/client.py::GemiClient`:
  - κοινός rate budget στο `shared` database cache: slots των 10,33 s, το πολύ 7 αιτήματα σε
    οποιοδήποτε κυλιόμενο λεπτό, σε όλα τα workers/clusters/instances (το gateway επιτρέπει
    8/λεπτό ανά κλειδί)·
  - priority lanes: DISCOVERY > DIGEST_IMPORT > MONITORED_REFRESH > DOCUMENTS·
  - κοινό cooldown από `Retry-After` / `RateLimit-Reset`· bounded retries για 429, transient 5xx
    και timeouts (`GEMI_MAX_ATTEMPTS`, προεπιλογή 4)·
  - το 404 μιας κενής αναζήτησης είναι κενό αποτέλεσμα· parsing των τριών μορφών σφάλματος·
    το κλειδί δεν εμφανίζεται ποτέ σε logs ή σφάλματα.
  - Το `services._get(path, params)` παραμένει το seam των importers, με ίδια υπογραφή και ίδια
    queries. Το `import_companies_since_date` τρέχει στο lane MONITORED_REFRESH και δεν ξαναδοκιμάζει
    πλέον επ' άπειρον.
  - Νέα settings: `GEMI_RATE_LIMIT_PER_MINUTE` (≤7), `GEMI_REQUEST_TIMEOUT_SECONDS`,
    `GEMI_MAX_ATTEMPTS`. Καμία migration, καμία αλλαγή σε billing, Radars ή UI.
  - 41 νέα tests (`gemiapp/test_gemi_client.py`)· **733 tests OK**· `check` και
    `makemigrations --check` καθαρά. Συμπεριφορά του API: `docs/GEMI_API_CAPABILITY_REPORT.md`.

- **Final consolidation pass — όλες οι υπόλοιπες επιφάνειες (2026-09-10).** Μεταφέρθηκαν στο
  Signal Ledger system και οι 24 εναπομείνασες σελίδες: 11 Superadmin subpages
  (Subscriptions, Radars, Leads, GEMI Pipeline, Email Digests, System Health, Audit Log,
  Accounts, User/Radar/Lead detail), 8 auth/account σελίδες (Login, Signup, password reset ×4,
  resend verification, verify pending), οι 2 legal σελίδες, τα unsubscribe/resume-checkout και
  το cookie consent banner. Προστέθηκαν τα `.auth-*`, `.prose-page`, `.cookie-banner`,
  `.admin-panel`, `.admin-btn`, `.admin-modal` primitives στο `product-ui.css`.
  - Οι μεταφορές έγιναν με transformers που **επαληθεύουν** ότι το ορατό κείμενο και η
    ακολουθία των Django tags μένουν ίδια (`.recovery/migrate_admin*.py`), ώστε να μην αλλάξει
    καμία λογική, καμία διεύθυνση και κανένα νομικό κείμενο.
  - Έλεγχος διαδρομών: `.recovery/route_audit.py` κάνει GET σε κάθε προσβάσιμη σελίδα και
    ελέγχει status, διαρροή template source, legacy classes και σύνδεση του `product-ui.css`.
    **ALL ROUTES CLEAN.**
  - Εκκρεμότητα προς γνώση: τα `.h-18` / `.pt-18` παράγονται πλέον μόνο επειδή το
    `gemiapp/**/*.py` είναι στα content globs του Tailwind και το ίδιο το `tests.py` τα
    αναφέρει (`test_the_nav_height_class_is_generated`). Κανένα template δεν τα χρησιμοποιεί.
  - `check`, `makemigrations --check`, `build:css`, `collectstatic` καθαρά· **646 tests OK**.
    Καμία εντολή deployment δεν εκτελέστηκε.

- **Forensic recovery & visual system consolidation (2026-09-09).** Εντοπίστηκε ότι το εγκεκριμένο
  Signal Ledger UI (`static/css/product-ui.css` + τα authenticated templates) **δεν υπήρχε καθόλου στο
  `main`** — ζούσε μόνο στο branch `fix/outreach-hard-bounces` (`e55f8a3`), το οποίο δεν είχε ποτέ γίνει
  merge. Το `main` είχε το compliance shutdown αλλά το παλιό generic SaaS UI. Γι' αυτό κάθε προηγούμενη
  προσπάθεια στις δημόσιες επιφάνειες επανέφερε `rounded-card` / `shadow-soft` / stat cards: αυτά ήταν
  όντως τα primitives του `main`.
  - Δημιουργήθηκε το branch `recovery/visual-consolidation` και έγινε merge του `fix/outreach-hard-bounces`.
    Επαληθεύτηκε ότι το UI branch είναι **superset** ως προς το compliance: ίδια migration `0031`, ίδια
    fail-closed defaults, ίδια 3 σημεία ελέγχου `outreach_enabled()`, κανένα αρχείο δεν χάθηκε.
  - Το `product-ui.css` επεκτάθηκε με δύο **scoped** layers, `body.public-body` και `body.admin-body`,
    ώστε τίποτα από τις δημόσιες/admin επιφάνειες να μην μπορεί να αγγίξει τα εγκεκριμένα authenticated
    screens. Τα `.product-*` primitives επαναχρησιμοποιούνται αυτούσια.
  - **Landing** (`templates/home.html`): δημόσιο header από το ίδιο brand mark· hero proof = το Signals
    register (`.product-signal`), όχι terminal panel· αλυσίδα EVENT → INFORMATION → CRITERIA → RELEVANCE
    → LEAD· συνθετικά `.demo` παραδείγματα.
  - **Pricing** (`templates/pricing.html`): κάθε πλάνο είναι operational definition row στη γραμματική
    των Radars. Πλήρης factual audit — βλ. πίνακα παρακάτω.
  - **Superadmin**: το rail είναι το product rail με ένα επιπλέον επίπεδο ομαδοποίησης. Το outreach
    μεταφέρθηκε από «Growth» σε **COMPLIANCE / ARCHIVE** και οι φόρμες send/test/queue **δεν
    αποδίδονται πλέον καθόλου** (ήταν disabled αλλά με ενεργό POST target).
  - Το `SAMPLE_LEADS` διατηρεί το συνθετικό σύνολο που αφαίρεσε ονόματα φυσικών προσώπων.
  - `check`, `makemigrations --check`, `build:css`, `collectstatic` καθαρά· **646 tests OK**.
    Καμία εντολή deployment δεν εκτελέστηκε.

### Pricing claim → source of truth

| Ισχυρισμός | Πηγή αλήθειας | Κατάσταση |
|---|---|---|
| Pro / Business / Enterprise / Custom | `UserSubscription.TIERS` (`models.py`) | επιβεβαιωμένο |
| Όρια Ραντάρ 5 / 10 / 15 / 15 | `RADAR_LIMITS` (`models.py`) | επιβεβαιωμένο |
| €19 / €49 / €99 | `PLAN_PRICES` (`superadmin/services.py`) + README | επιβεβαιωμένο |
| Custom «κατόπιν συμφωνίας», εκτός checkout | `SELECTABLE_TIERS` (`billing.py`) | επιβεβαιωμένο |
| Ενημερώσεις ανά 3 ώρες, 08:00 - 23:00 | `DigestDelivery.FREQUENCIES` + `apps.SCHEDULES` cron | επιβεβαιωμένο |
| Priority Alerts μόνο Enterprise/Custom | `TOP_TIERS` (`services.py`) | επιβεβαιωμένο |
| Ημερήσιο digest 09:00 | `apps.SCHEDULES` cron `0 9 * * *` | επιβεβαιωμένο |
| Εξαγωγή CSV σε κάθε πληρωμένο πλάνο | `views.export_csv` (`radar_limit > 0`) | επιβεβαιωμένο |
| 9.651 κωδικοί ΚΑΔ | `gemiapp/data/kad_2025.json` | επιβεβαιωμένο |
| «Οι πληρωμές δεν είναι ακόμη ενεργές» | `LEGAL_BILLING_ACTIVE` (default `0`) | επιβεβαιωμένο |
| API Webhooks / dedicated database sync | — καμία υλοποίηση | **αφαιρέθηκε** |
| «Απεριόριστα Leads» | — δεν υπάρχει τέτοιο όριο στο μοντέλο | **αφαιρέθηκε** |

- **Ολοκληρώθηκε ο έλεγχος και η παραγωγή review captures για τις εμπορικές επιφάνειες & το Superadmin (2026-09-09).** Δημιουργήθηκαν τα νέα captures στο `docs/gemi-leads-ui-study/screenshots/` και στα artifacts: `marketing_superadmin_desktop.png` & `marketing_superadmin_users_desktop.png` (INTERNAL REVIEW ONLY), `marketing_pricing_desktop.png` (PUBLIC MARKETING SAFE), `marketing_landing_desktop.png` (PUBLIC MARKETING SAFE), `marketing_landing_full_desktop.png`, `marketing_landing_mobile.png` & `marketing_landing_full_mobile.png`. Διενεργήθηκε πλήρης έλεγχος copy στη δημόσια landing page, επιβεβαιώνοντας ότι δεν υπάρχουν υπόνοιες ή αναφορές σε αυτόματη αποστολή cold outreach email προς νέες εταιρείες. Το Signal Ledger UI και το backend διατηρήθηκαν 100% ανέγγιχτα.
- **Ολοκληρώθηκε η δημιουργία του privacy-safe marketing/demo dataset & review captures για το NORVA (2026-09-09).** Δημιουργήθηκε η νέα reversible εντολή διαχείρισης `seed_demo_marketing_data` (`gemiapp/management/commands/seed_demo_marketing_data.py`), η οποία εισάγει 4 συνθετικά, 100% εικονικά παραδείγματα ελληνικών επιχειρήσεων (`ΑΙΓΑΙΟ LOGISTICS ΜΟΝ. Ι.Κ.Ε.`, `HELLAS CLOUD & DATA LABS Α.Ε.`, `GREEN GRID SOLAR SOLUTIONS Ι.Κ.Ε.`, `KALYPSO HOSPITALITY & TRADING Ε.Ε.`) με εικονικά GEMI (`999000101000`..`999000104000`), εικονικά ΑΦΜ, εικονικές διευθύνσεις/emails, KAD records, διαχειριστές και αντιπροσωπευτικές καταστάσεις lead/matches/notes. Υποστηρίζεται και παράμετρος `--clean` για άμεση, μη καταστροφική αφαίρεση. Δημιουργήθηκαν τα νέα review captures στο `docs/marketing-demo-data.md` και στα artifacts: `marketing_signals_desktop` (1440x900), `marketing_radars_desktop` (1440x900), `marketing_dossier_desktop` (1440x900), `marketing_leads_desktop` (1440x900), `marketing_signals_mobile` (390x844) και `marketing_dossier_mobile` (390x844). Το production Signal Ledger UI διατηρήθηκε ακέραιο χωρίς καμία εικαστική αλλαγή.
- **Ολοκληρώθηκε ο έλεγχος συμμόρφωσης, ασφάλειας και οριστικής απενεργοποίησης του cold outreach subsystem (2026-09-09).** Ο fail-closed master switch `OUTREACH_ENABLED` (default `0`) και το `OUTREACH_DAILY_SEND_CAP` (`0`) ελέγχουν κάθε σημείο εισόδου. Με τη νέα migration `0031_cancel_pending_outreach.py`, όλες οι παλιές εγγραφές `pending` και `sending` μεταφέρθηκαν οριστικά σε `status="cancelled"` («Ακυρώθηκε») με αιτιολογία compliance shutdown, ώστε να μην μπορούν να ξανασταλούν ακόμα κι αν άλλαζε μελλοντικά το `OUTREACH_ENABLED`. Οι εντολές διαχείρισης `requeue_dropped_outreach` και `prune_bot_suppressions` θωρακίστηκαν με `outreach_enabled()` checks. Δημιουργήθηκε το επίσημο έγγραφο περιστατικού/συμμόρφωσης στο `docs/compliance/cold-outreach-shutdown.md` με checklists για Brevo Dashboard και Render Deployment, data-retention classification και prerequisites. Προστέθηκαν νέα regression tests στο `gemiapp/tests.py` και όλα τα **646 tests** περνούν καθαρά (`OK`), μαζί με καθαρά `manage.py check` και `makemigrations --check`.
- **Ολοκληρώθηκε η παραγωγική υλοποίηση του εγκεκριμένου Signal Ledger UI direction για όλες τις authenticated σελίδες.** Το νέο visual system εφαρμόστηκε στα `templates/base.html`, `templates/dashboard.html`, `templates/includes/dashboard_rows.html`, `templates/radars/list.html`, `templates/radars/detail.html`, `templates/radars/form.html`, `templates/companies/detail.html`, `templates/leads/list.html` και `templates/settings.html` με τη χρήση του νέου scoped `static/css/product-ui.css`. Διατηρήθηκε ακέραια η backend λογική, τα routes, τα API contracts, το infinite scroll, τα φίλτρα, η ασφάλεια (POST+CSRF), η προσβασιμότητα και τα billing state machines. Δημιουργήθηκαν νέα review captures στο `docs/gemi-leads-ui-study/screenshots/production-*.png` για desktop (1440x900) και mobile (390x844). Όλα τα 642 tests περνούν καθαρά (`OK`).
- **Το Signal Ledger εγκρίθηκε ως governing UI direction και ολοκληρώθηκε focused refinement pass χωρίς αλλαγή production UI ή backend.** Το review package στο `docs/gemi-leads-ui-study/` διατηρεί το chronological signal register, selected-signal panel, Signals/Radars/Leads language, EVENT → CRITERIA → LEAD logic και amber relevance accent. Το refinement βελτιώνει state grammar, selected-signal reading order, product-specific navigation/source metadata και τη mobile ροή. Αναμένεται visual review των refined captures πριν από οποιαδήποτε production υλοποίηση.
- **Τα cold outreach emails προς νέες εταιρείες ΓΕΜΗ έχουν διακοπεί από 2026-09-09.** Ο νέος fail-closed master switch `OUTREACH_ENABLED` έχει προεπιλογή `0` και μπλοκάρει server-side κάθε manual queue, test outreach, background send και daily pending drain. Το `OUTREACH_DAILY_SEND_CAP` είναι επίσης `0` σε defaults, local `.env` και `render.yaml`. Οι υπάρχουσες `pending` εγγραφές διατηρούνται σε αναμονή και το ιστορικό δεν διαγράφεται. Digest, verification και password-reset emails δεν επηρεάζονται.
- **Ολοκληρώθηκε focused UI art-direction study για το authenticated Gemi Leads χωρίς αλλαγή production UI ή backend.** Το review package βρίσκεται στο `docs/gemi-leads-ui-study/` και περιλαμβάνει audit της τρέχουσας Django/Tailwind αρχιτεκτονικής, τρεις διακριτές κατευθύνσεις, desktop/mobile mockups, anti-AI έλεγχο, implementation risks και standalone low-fidelity prototype για Dashboard/Signals, Radar view και Lead detail. Προτεινόμενη κατεύθυνση: **Signal Ledger**. Αναμένεται visual review πριν από οποιαδήποτε production υλοποίηση.
- **Η εφαρμογή είναι σε BETA και οι πληρωμές ΔΕΝ είναι ενεργές.** Δύο ανεξάρτητες σημαίες: `BETA_MODE` (προεπιλογή `1`) εμφανίζει το beta badge/banner, `LEGAL_BILLING_ACTIVE` (προεπιλογή `0`) κρατά κλειστό το billing. Όσο το billing είναι κλειστό, το `create_checkout_session` απαντά με redirect στο pricing και τα κουμπιά checkout εμφανίζονται ανενεργά — καμία χρέωση δεν είναι δυνατή, ούτε με απευθείας POST. Η πρόσβαση δίνεται αποκλειστικά με complimentary access από το Superadmin.
- Το πραγματικό `GEMI_API_KEY` φορτώνεται τοπικά από `.env` μέσω `python-dotenv`.
- Το εμπορικό όνομα της εφαρμογής είναι «Gemi Leads» και το production domain που έχει κατοχυρωθεί είναι `gemileads.gr`.
- Το επταήμερο μετρά ακριβώς 7 ημερολογιακές ημέρες.
- Το dashboard φορτώνει τα αποτελέσματα με infinite scroll ανά 20 εγγραφές.
- Τα εμφανιζόμενα metrics στην αρχική και στο dashboard προέρχονται από τη βάση, όχι από demo αριθμούς.
- Ο επίσημος κατάλογος ΚΑΔ 2025 έχει 9.651 κωδικούς (`gemiapp/data/kad_2025.json`). Η βάση περιέχει επιπλέον όσους GEMI-only κωδικούς εμφανίστηκαν στα πραγματικά δεδομένα, οπότε το `ActivityCode.objects.count()` είναι μεγαλύτερο. **Δημόσια επικοινωνείται μόνο ο αριθμός 9.651**, που είναι ο επαληθεύσιμος.
- Οι αριθμοί εγγραφών (εταιρείες, leads, χρήστες) ζουν στην production βάση. Μην τους αντιγράφεις εδώ ως σταθερές: παλιώνουν αμέσως και έχουν ήδη δώσει λάθος νούμερα σε δημόσιο κείμενο.
- Το dashboard και οι φόρμες Radar έχουν προσβάσιμο autocomplete ΚΑΔ με debounce, αναζήτηση αριθμού/ελληνικών όρων, keyboard navigation, chips και έως 25 επιλογές.
- Τα φίλτρα ΚΑΔ εφαρμόζονται στο dashboard και στο CSV export με λογική OR.
- Το dashboard υποστηρίζει επιλογή χρονικού διαστήματος «Από–Έως» με συμπεριληπτικά όρια· το ίδιο εύρος εφαρμόζεται και στο CSV export.
- Η Φάση 1 — Core Radars έχει ολοκληρωθεί.
- Η Φάση 2 — Lead Inbox έχει ολοκληρωθεί.
- Πλάνα: **Free** (€0) — προβολή νέων εγγραφών, βασικά στοιχεία επιχείρησης, ημερήσιο email digest 09:00· **0 Ραντάρ, χωρίς CSV, χωρίς intraday** (βλ. «Free: ημερήσιο email digest»). Paid tiers: Pro (€19/μήνα, 5 Ραντάρ), Business (€49/μήνα, 10 Ραντάρ), Enterprise/Real-Time (€99/μήνα, 15 Ραντάρ) και Custom (κατόπιν συμφωνίας, 15 Ραντάρ), με τα χαρακτηριστικά τους αμετάβλητα. *(Ιστορικό: από 2026-08-18 έως 2026-09-26 το προϊόν ήταν paid-only.)* Τα όρια ορίζονται **αποκλειστικά** στο `RADAR_LIMITS` (`gemiapp/models.py`) και μπορούν να παρακαμφθούν ανά λογαριασμό με `custom_radar_limit`.
- Ολοκληρώθηκε το **Custom Superadmin Control Center** στο `/superadmin/`: Dedicated responsive layout, Executive SaaS KPI Metrics & Charts (MRR/ARR calculation), Users Management (deactivate/reactivate, complimentary Pro/Business grant), Subscriptions Overview, Global Radars (Effective Matching Status breakdown), Global Leads & Snapshots (με προστασία απομόνωσης ιδιωτικών σημειώσεων), GEMI Pipeline Operations & Manual Run trigger, Digest Deliveries Log & Retry, Non-destructive System Health monitoring, Audit Log (`AdminAuditLog`), και User Impersonation με καθολικό top banner & ασφαλή επαναφορά identity.
- Υπάρχουν **525 tests** (`manage.py test`, καθαρό **`OK`** — μηδέν `expectedFailure`, μηδέν γνωστά ανοιχτά billing bugs). Επαληθεύτηκε 2026-08-25. Το `manage.py check` και το `makemigrations --check` είναι καθαρά.
- **Phase 5g ολοκληρώθηκε: τελικό polish του billing UI/subscription lifecycle — καθαρό, μη αντικρουόμενο UI για κάθε δυνατό billing state, χωρίς καμία αλλαγή σε billing mechanics/webhook semantics/entitlement predicates.** Νέο `gemiapp/templatetags/billing_tags.py`: filter `stripe_status_label` (raw Stripe status → human-readable Greek label, με safe fallback "Άγνωστη κατάσταση" για άγνωστο status — ποτέ raw string στον χρήστη) και filter `billing_state` (μία κανονική, mutually-exclusive lifecycle κατάσταση ανά subscription — `payment_problem` > `scheduled_cancellation` > `scheduled_downgrade` > `normal_active` > `terminal` > `complimentary_only` > `none` — βασισμένη αποκλειστικά σε ήδη υπάρχοντα πεδία/taxonomies, καμία νέα Stripe κλήση, καμία επιρροή σε entitlement). `templates/settings.html` ξαναγράφτηκε γύρω από αυτό το state machine, με ξεχωριστό, σωστό branch για κάθε state — inclusive πλέον και του `trialing`/`past_due`/`unpaid`/`incomplete` (πρώτα αγνοούνταν εντελώς, βλ. residual note παρακάτω) και του terminal (`canceled`/`incomplete_expired`, δείχνει «Επιλογή νέου πλάνου»). `templates/pricing.html`: διορθώθηκε πραγματικό UX bug — τα per-tier checkout buttons ήταν κρυμμένα για κάθε complimentary-only χρήστη (χωρίς καμία πραγματική Stripe subscription), παρότι έπρεπε να μπορεί να αγοράσει κανονικά· αφαιρέθηκε το complimentary-gating branch από τις 3 paid-tier κάρτες (το πάνω banner εξακολουθεί να δείχνει το complimentary access). Νέο `templates/includes/pending_change_notice.html`: ενιαίο, DRY μήνυμα (link στο `/settings/`) όπου πριν υπήρχε γενικό placeholder «Υπάρχει ήδη προγραμματισμένη αλλαγή» — πλέον διαφοροποιεί ρητά `active_until` («Η συνδρομή θα λήξει στις Χ») από `scheduled_tier` («Προγραμματισμένη αλλαγή → Υ στις Ζ»), reused σε 4 σημεία στο pricing.html. Tests: `PricingLifecycleUITests` (12), `SettingsLifecycleUITests` (9), `BillingStatusLabelTests` (3), `BillingMessageContentTests` (2) = 26 νέα· 2 παλιότερα tests (Phase 5e/5f) ενημερώθηκαν ώστε να ελέγχουν το νέο, ισοδύναμο copy («Προγραμματισμένη αλλαγή») αντί για τη λέξη «μετά» που αφαιρέθηκε. Καμία migration. Full suite: **525 tests, `OK`, μηδέν expected failures.** **Backend inconsistency που αποκάλυψε το audit (τεκμηριωμένο, ΔΕΝ διορθώθηκε — θα άλλαζε entitlement semantics, εκτός scope):** `UserSubscription.ALLOWED_PAID_STATUSES = ("active",)` σημαίνει ότι `has_active_paid_subscription` είναι `False` για `trialing`/`past_due`/`unpaid`/`incomplete`, οπότε το **pricing.html** (σε αντίθεση με το νέο settings.html) εξακολουθεί να δείχνει σε αυτούς τους χρήστες τα κανονικά checkout buttons αντί για κατάσταση διαχείρισης — αν πατήσουν, το ήδη υπάρχον `_blocks_new_checkout` server-side guard (Phase 4) μπλοκάρει σωστά και τους στέλνει στο `/settings/` με μήνυμα, άρα καμία διπλή χρέωση, μόνο ένα UX rough edge στο pricing (το settings.html είναι πλέον σωστό για αυτά τα statuses μέσω του `billing_state`).
- **Phase 5f ολοκληρώθηκε: ακύρωση/release μιας ήδη προγραμματισμένης downgrade.** Νέο `POST /api/stripe/cancel-scheduled-downgrade/` → φρέσκο `SubscriptionSchedule.retrieve()` (επιβεβαιώνει ότι ανήκει στο σωστό subscription, status ∈ {active, not_started}, future phase ταιριάζει με το local `scheduled_tier`) → `SubscriptionSchedule.release()` (ΟΧΙ `.cancel()`· η underlying subscription συνεχίζει κανονικά). Boundary race (πάτημα κοντά στο period end): status="completed" → ρητό μήνυμα "η αλλαγή έχει ήδη εφαρμοστεί", ΟΧΙ ψευδής επιτυχία, καμία automatic upgrade-πίσω. Idempotency σε τέταρτο ξεχωριστό namespace (`cancel_downgrade_attempt_nonce`, prefix `cdr_`). Νέο webhook `subscription_schedule.released`/`.canceled` καθαρίζει ΜΟΝΟ τα 3 scheduled πεδία (ποτέ tier/status/entitlement) — με out-of-order guard (αγνοεί stale event αν το local `stripe_schedule_id` ήδη δείχνει νεότερο schedule). **⚠️ Production-ready μόνο μετά από πραγματικό Stripe TEST MODE verification — δεν έχει γίνει ακόμη, βλ. ιστορικό.**
- **Phase 5e ολοκληρώθηκε: scheduled downgrade (Business→Pro, Enterprise→Business, Enterprise→Pro) μέσω Stripe `SubscriptionSchedule`, στο τέλος της τρέχουσας billing period.** Το `POST /api/stripe/change-plan/` (ίδιο endpoint με Phase 5d) πλέον δρομολογεί αυτόματα σε upgrade ή downgrade branch με βάση `TIER_RANK` — ο client εξακολουθεί να στέλνει μόνο `target_tier`. Downgrade: `SubscriptionSchedule.create(from_subscription=...)` (ή reuse υπάρχοντος schedule αν το fresh `Subscription.retrieve()` δείχνει ήδη ένα — αυτό ΕΙΝΑΙ το recovery mechanism για create-succeeds/modify-fails, καμία νέα schema χρειάστηκε) → `SubscriptionSchedule.modify(phases=[current_phase, future_phase], end_behavior="release")`, `proration_behavior="none"` και στις δύο φάσεις, boundary = πραγματικό `current_period_end` του μοναδικού item (ΟΧΙ υπολογισμένο). Ποτέ synchronous τοπική εγγραφή `scheduled_tier`/`scheduled_change_at`/`stripe_schedule_id`/tier/status/entitlement. Νέο webhook `subscription_schedule.updated` κάνει την projection (fail-closed σε malformed/άγνωστο shape)· το ήδη υπάρχον `customer.subscription.updated` handler καθαρίζει αυτόματα τα τρία scheduled πεδία όταν το tier που φτάνει ταιριάζει με το `scheduled_tier` (η μετάβαση ολοκληρώθηκε). **⚠️ Production-ready μόνο μετά από πραγματικό Stripe TEST MODE verification — δεν έχει γίνει ακόμη, βλ. ιστορικό.**
- **Phase 5d ολοκληρώθηκε: immediate strict-upgrade (Pro→Business, Pro→Enterprise, Business→Enterprise) με proration.** Νέο `POST /api/stripe/change-plan/` (`gemiapp/billing.py:change_plan`) — `stripe.Subscription.modify(items=[{"id": item_id, "price": target_price_id}], proration_behavior="always_invoice", payment_behavior="error_if_incomplete")`, idempotency key σε δικό του namespace (`plan_change_attempt_nonce:`, prefix `pc_`). Ποτέ synchronous τοπική αλλαγή tier/status/entitlement — αποκλειστικά webhook-driven, ίδια φιλοσοφία με Phases 2/5b/5c. **⚠️ Production-ready μόνο μετά από πραγματικό Stripe TEST MODE verification — δεν έχει γίνει ακόμη, βλ. ιστορικό.** Downgrade παραμένει εντελώς εκτός scope (Phase 5e).
- **Phase 5c ολοκληρώθηκε: το `UserSubscription.active_until` έχει πλέον live webhook wiring.** Σημαίνει αποκλειστικά «γνωστή μελλοντική ημερομηνία λήξης entitlement λόγω ήδη προγραμματισμένης cancellation» — `None` για κανονική ανανεούμενη συνδρομή (ακόμη κι αν υπάρχει `current_period_end`), τιμή μόνο όταν `cancel_at_period_end=True` στο Stripe. Ενημερώνεται αποκλειστικά από το `customer.subscription.updated`/`.deleted` webhook (`gemiapp/billing.py:_handle_subscription_updated_or_deleted`), ποτέ synchronously από τα Phase 5b `cancel_subscription`/`resume_subscription` endpoints. Δεν συμμετέχει πουθενά σε entitlement (`has_entitlement`/`effective_tier`/`radar_limit`) — αυτά συνεχίζουν να βασίζονται αποκλειστικά στο `status`. Το `/settings/` δείχνει πλέον είτε "Ακύρωση στο τέλος περιόδου" (χωρίς scheduled cancellation) είτε "Η συνδρομή σου θα λήξει στις Χ" + "Συνέχιση συνδρομής" (με scheduled cancellation) — ποτέ και τα δύο μαζί.
- **Το production-readiness audit για το Stripe billing ολοκλήρωσε τις Phases 0-4, και προχωράει η Phase 5 (upgrade/downgrade lifecycle): 5a (schema) και 5b (cancel-at-period-end/resume) ολοκληρωμένα· Phase 5c (upgrade/downgrade/webhook wiring) δεν έχει ξεκινήσει.** Νέα endpoints `POST /api/stripe/cancel-subscription/` και `POST /api/stripe/resume-subscription/` (`gemiapp/billing.py:cancel_subscription/resume_subscription`) κάνουν `stripe.Subscription.modify(cancel_at_period_end=True/False)` με idempotency key σε ξεχωριστό namespace από το Phase 3 checkout nonce· **καμία** synchronous τοπική αλλαγή σε tier/status/entitlement — αυτό παραμένει αποκλειστικά webhook-driven (Phase 5c). Νέο κουμπί «Ακύρωση στο τέλος περιόδου» στο `/settings/`· resume button σκόπιμα **δεν** προστέθηκε ακόμη (καμία τοπική προβολή του `cancel_at_period_end` μέχρι τη Phase 5c) αλλά το `/billing/resume/` endpoint υπάρχει, testable, απλά χωρίς UI hook προς το παρόν. Το `UserSubscription` απέκτησε 3 νέα, πλήρως αδρανή πεδία (migration `0021`): `scheduled_tier`, `scheduled_change_at`, `stripe_schedule_id` — projection/UX cache του μελλοντικού Stripe Subscription Schedule, καμία επιρροή σε `effective_tier`/`has_entitlement`/`radar_limit` (pinned με tests). Το `active_until` (dead field από πριν) απέκτησε ρητή, τεκμηριωμένη σημασιολογία — «γνωστή ημερομηνία λήξης entitlement λόγω προγραμματισμένου τερματισμού (cancel_at_period_end), ΟΧΙ renewal date» — αλλά ακόμη καμία runtime wiring (Phase 5c+). `StripeWebhookEvent` (migration `0020`) δίνει idempotency/audit trail σε κάθε webhook delivery (Phase 1). Το P0 «χρεώθηκε αλλά δεν έχει entitlement» bug διορθώθηκε στη Phase 2. Η Phase 3 πρόσθεσε Stripe idempotency key + client-side double-submit guard στο checkout. Η Phase 4 πρόσθεσε server-side guard στο `create_checkout_session` που μπλοκάρει δεύτερο Stripe Checkout Session όσο υπάρχει ήδη ζωντανή Stripe subscription (`LIVE_STRIPE_SUBSCRIPTION_STATUSES`/`TERMINAL_STRIPE_SUBSCRIPTION_STATUSES` στο `gemiapp/billing.py`) — κατευθύνει στο `/settings/` (Διαχείριση Συνδρομής). Γνωστό residual risk (τεκμηριωμένο, όχι διορθωμένο): δύο **πραγματικά ταυτόχρονα** πρώτα-checkout requests από διαφορετική session/συσκευή για user χωρίς καμία προηγούμενη συνδρομή δεν μπλοκάρονται μεταξύ τους (ο local guard δεν βλέπει ακόμη το πρώτο Stripe Session πριν έρθει το webhook) — θα χρειαζόταν lightweight `checkout_in_progress` σήμανση, σκόπιμα εκτός Phase 4.
- **Προϋπόθεση για να τρέξουν τα tests:** το `settings.py` χρησιμοποιεί `CompressedManifestStaticFilesStorage` χωρίς εξαίρεση για DEBUG, άρα κάθε `{% static %}` απαιτεί staticfiles manifest. Σε καθαρό clone τρέξε **και τα τρία**: `pip install -r requirements.txt`, `npm ci && npm run build:css`, `manage.py collectstatic --noinput`. Χωρίς αυτά εμφανίζονται ~126 ψευδή errors (`Missing staticfiles manifest entry`) που μοιάζουν με σπασμένο κώδικα ενώ είναι κενό περιβάλλον.
- Το `Company.search_name` είναι denormalized, indexed πεδίο (accent-stripped, uppercase) που ενημερώνεται αυτόματα στο `save()`. Όλες οι αναζητήσεις επωνυμίας των Radars γίνονται πάνω σε αυτό, στη βάση.
- Τα δικαιώματα συνδρομής εκφράζονται και ως database predicates (`paid_subscription_q`, `complimentary_q`, `entitlement_q`, `effective_tier_q` στο `gemiapp/models.py`), ώστε τα φίλτρα του Superadmin να μη φορτώνουν όλους τους χρήστες στη μνήμη. Υπάρχει test που επαληθεύει την ισοδυναμία τους με τα Python properties σε 375 συνδυασμούς.
- Σε production (`DJANGO_DEBUG=0`) ενεργοποιούνται αυτόματα HTTPS redirect, HSTS, secure/HttpOnly cookies, `X_FRAME_OPTIONS=DENY` και `SECURE_PROXY_SSL_HEADER`. Τα `CSRF_TRUSTED_ORIGINS` παράγονται από το `DJANGO_ALLOWED_HOSTS`.

## Σημαντικές αποφάσεις

- **Beta χωρίς billing:** όσο `LEGAL_BILLING_ACTIVE=0`, η εφαρμογή δεν χρεώνει κανέναν. Το Stripe παραμένει ολόκληρο στον κώδικα και ενεργοποιείται με μία μεταβλητή περιβάλλοντος — δεν αφαιρέθηκε, ώστε το άνοιγμα των πληρωμών να μη χρειάζεται νέα υλοποίηση. Ο έλεγχος γίνεται **και** server-side στο `create_checkout_session`, όχι μόνο κρύβοντας τα κουμπιά.
- Χωρίς ενεργή πληρωμένη συνδρομή (`has_active_paid_subscription == True`), ο χρήστης δεν έχει πρόσβαση στην παραγωγή νέων leads. Το όριο ενεργών Ραντάρ είναι 0.
- **Το intraday (3ωρο) digest απαιτεί `effective_tier` σε `enterprise` ή `custom`.** Ένας χρήστης με complimentary **Pro** ή **Business** παίρνει το ημερήσιο digest αλλά **ποτέ** 3ωρο. Αυτό εξηγεί «δεν έλαβα ΠΟΤΕ 3ωρο email» — όχι «έλαβα στα άλλα slots αλλά όχι στις 11:00», που έχει διαφορετικές αιτίες (δες παρακάτω).
- **Ένα μεμονωμένο slot που σιωπά είναι συχνά σωστή συμπεριφορά.** Όταν το ΓΕΜΗ δεν έχει καμία νέα εγγραφή από την προηγούμενη αποστολή, το `send_digests` κάνει `continue` χωρίς email: ένα άδειο real-time alert δεν έχει αξία. Οι πραγματικές αιτίες αποτυχίας ενός slot είναι: δεν έτρεξε το task, απέτυχε το import, ή σκοτώθηκε στο timeout.
- **Το intraday δεν γράφει γραμμή `DigestDelivery` όταν παραλείπει.** Το unique constraint είναι `(user, digest_date, frequency)`, δηλαδή **μία γραμμή την ημέρα** για όλα τα 3ωρα slots — μια γραμμή «skipped» θα έσβηνε την καταγραφή μιας προηγούμενης επιτυχούς αποστολής της ίδιας ημέρας. Συνέπεια: για το τι συνέβη σε ένα **συγκεκριμένο slot**, πηγή αλήθειας είναι ο πίνακας `ImportRun` (μία γραμμή ανά εκτέλεση) μαζί με το `last_sent_company_id`, όχι το `DigestDelivery`.
- Τα ιστορικά δεδομένα των χρηστών (παλιά leads, σημειώσεις, αγαπημένα, ορισμοί Ραντάρ) διατηρούνται ακέραια και δεν διαγράφονται κατά την ακύρωση συνδρομής.
- Το Superadmin Control Center παρέχει δυνατότητα παραχώρησης δωρεάν πρόσβασης (complimentary Pro/Business access) με προαιρετική λήξη χωρίς να αλλοιώνει το Stripe status. Η πρόσβαση (entitlement) δίνεται αν `has_active_paid_subscription OR has_valid_complimentary_access`.
- Η λειτουργία User Impersonation επιτρέπει στο Superadmin να εξετάσει την εφαρμογή ως οποιοσδήποτε απλός χρήστης. Προβάλλεται επίμονο banner στο πάνω μέρος της εφαρμογής και η έξοδος επαναφέρει με ασφάλεια το Superadmin identity. Impersonation άλλου Superadmin ή nested impersonation απαγορεύεται.
- Όλες οι ευαίσθητες διοικητικές ενέργειες καταγράφονται μόνιμα στο `AdminAuditLog`.
- Οι ΚΑΔ από το ΓΕΜΗ έρχονται ως 8 ψηφία, ενώ ο επίσημος κατάλογος χρησιμοποιεί τελείες. Η αντιστοίχιση γίνεται με κανονικοποιημένο κωδικό μόνο ψηφίων.
- Ο πλήρης κατάλογος ΚΑΔ αποθηκεύεται στη βάση και δεν διαβάζεται από CSV κατά τη λειτουργία της εφαρμογής.
- Πολλαπλοί επιλεγμένοι ΚΑΔ λειτουργούν με λογική OR: μια εταιρεία ταιριάζει αν έχει τουλάχιστον έναν από τους επιλεγμένους ΚΑΔ.
- Στα Radars οι επιλογές της ίδιας κατηγορίας λειτουργούν με OR και οι διαφορετικές κατηγορίες με AND.
- Η ίδια εταιρεία δημιουργεί ένα `UserCompanyLead` ανά χρήστη, ακόμη κι αν ταιριάξει σε πολλά Radars. Κάθε Radar διατηρεί ξεχωριστό `RadarMatch` και snapshot του λόγου αντιστοίχισης.
- Για email έχει επιλεγεί το Brevo Free για το MVP. Το `gemileads.gr` θα χρησιμοποιηθεί για εταιρικό mailbox και πιστοποιημένο sender domain.

## Τι απομένει

- **Email templates — ανοιχτά:** (α) τα allauth emails (π.χ. reset από το `/accounts/password/reset/`, που
  είναι ακόμη προσβάσιμο) μένουν στα default plain-text του allauth, εκτός του νέου σχεδίου· (β) το
  `client_outreach.*` μένει στο παλιό στυλ όσο το outreach είναι παγωμένο· (γ) χρειάζεται μία πραγματική
  αποστολή σε Gmail/Outlook/Apple Mail μετά το deploy (τα logos φορτώνουν από `gemileads.gr/static`).
- **Branding — ανοιχτές αποφάσεις (όχι bugs):** (α) το `<meta name="theme-color" content="#081a2c">` του
  `base.html` είναι ακόμη το παλιό navy (το superadmin έχει `#12201e`)· αφέθηκε γιατί είναι χρώμα, όχι asset.
  (β) Το public footer δεν είχε ποτέ logo, μόνο το νομικό «© 2026 GEMI LEADS»· δεν προστέθηκε logo για να μην
  αλλάξει το layout. (γ) Τα email δείχνουν τα logos με απόλυτο URL `https://gemileads.gr/static/images/…`:
  λειτουργούν μόνο μετά το deploy αυτών των assets.

- **G4 blocker A — late publications (ανοιχτό, μετρούμενο):** εταιρείες που ο legacy importer δεν αποθηκεύει
  ποτέ τοπικά (φέρνει μόνο όσες έχουν ημερομηνία σύστασης = ημερομηνία στόχου) μένουν `pending_no_company` και
  **δεν** παίρνουν σήμα NEW_COMPANY μέχρι το cutover του Discovery v2. Δεν λύθηκε σκόπιμα: μετριέται στο G4 ως
  `unmaterialised (no Company yet)` και ως `v2_only → late_publication`. Η λύση σημαίνει δημιουργία `Company`
  εκτός του legacy importer, δηλαδή την πύλη του cutover. **Η διαδρομή υπάρχει πλέον** (pending-company
  hydration, 2026-09-26) αλλά είναι **κλειστή**. Πριν ανοίξει χρειάζεται απόφαση για την έκθεση στο legacy
  προϊόν: (α) αν οι hydrated εταιρείες πρέπει να φαίνονται στο legacy dashboard/CSV· (β) τι γίνεται με τα
  `invalid_date`, που το `company_defaults` αποθηκεύει ως «σήμερα» και άρα μπαίνουν στο σημερινό legacy
  digest/matching· (γ) μία δοκιμαστική εκτέλεση `--dry-run` σε production για τα πραγματικά `stored_as_today`.
- **G4 blocker B — χρονοπρογραμματισμός: ΛΥΘΗΚΕ (2026-09-20).** Η κατάταξη του Discovery v2 κρίνεται πλέον από το
  σύνορο στην αρχή του run, όχι από το αν ο legacy importer είχε προλάβει να γράψει τη γραμμή· η σειρά των runs δεν
  επηρεάζει πια ποια σήματα NEW_COMPANY παράγονται. Η σταθερή ακολουθία discovery → materialization →
  pipeline υπάρχει πλέον ως **μία** εντολή operator, `manage.py run_g4_shadow_cycle` (2026-09-20). Ανοιχτό μόνο:
  πότε την τρέχει ο operator — **καμία** εγγραφή στο `apps.SCHEDULES`, ούτε για το Discovery v2 ούτε για τον κύκλο.
- **Ιδιοκτησία billing — ανοιχτό:** το billing μένει user-owned με entitlement οργανισμού παράγωγο του owner·
  μεταφορά συνδρομής/Stripe customer σε Organization (και θέσεις/seats) είναι ξεχωριστό, ελεγμένο μελλοντικό πακέτο.
- **Customer workspace — ανοιχτά:** δεν υπάρχει ακόμη customer UI για δημιουργία οργανισμού, πρόσκληση/διαχείριση
  μελών, ρυθμίσεις/προφίλ/ICP οργανισμού, audit log (D36) και σήμανση
  «viewed». Απόφαση προϊόντος: αν το `/dashboard/` (Signals) θα ανακατευθύνει τα μέλη στον πίνακα του οργανισμού.
- **Gemi Leads 2.0 — επόμενη δουλειά: RELEASE READINESS** (όχι feature πακέτο). Η Phase D ολοκληρώθηκε· πρώτα οι
  πύλες G0/G1 που μπλοκάρουν τις production migrations 0032–0054 και την ενεργοποίηση του D37 schedule σε
  production, έπειτα το G4. **Επόμενο βήμα G0:** απομονωμένο staging (δες `docs/RELEASE_READINESS.md`). Ανοιχτές αποφάσεις προϊόντος: emitters/παραλήπτες για NEW_OPPORTUNITY, PRIORITY_SIGNAL,
  RADAR_MATCH· reopen τελικών καταστάσεων· επεξεργασία/διαγραφή σημειώσεων και εργασιών· αναίρεση suppression·
  email/phone suppression (Phase G)· UI για το audit log.
- **Release gate για το D37:** η `0054_organization_notification` και το D37 schedule **δεν** ενεργοποιούνται σε
  production πριν τα G0/G1.
- **Release gate για το D36:** η `0053_organization_audit_event` **δεν** εφαρμόζεται σε production πριν τα G0/G1.
- **Release gate για το D35:** η `0052_organization_contact_suppression` **δεν** εφαρμόζεται σε production πριν τα G0/G1.
- **Release gate για το D34:** η `0051_opportunity_task` **δεν** εφαρμόζεται σε production πριν τα G0/G1.
- **Release gate για το D33:** η `0050_opportunity_note` **δεν** εφαρμόζεται σε production πριν τα G0/G1.
- **Release gate για το D31:** η `0049_opportunity_assignment` **δεν** εφαρμόζεται σε production πριν τα G0/G1.
- **Release gate για το D29:** καμία migration· το route είναι πίσω από login και G5, αλλά δεν υπάρχει ακόμη κανένας
  σύνδεσμος πλοήγησης προς αυτό και καμία παραγωγή ευκαιριών/LIVE signals, οπότε στην πράξη δεν εμφανίζει τίποτα.
- **Release gate G5 — `G5_STATUS = PASSED_FOR_CURRENT_ORGANIZATION_SURFACE`:** ισχύει για τις υπάρχουσες
  organization-owned ρίζες (profile, ICP, Radars, opportunities, feed). Νέες επιφάνειες δεν «κληρονομούν» το πέρασμα
  αυτόματα: πρέπει να χρησιμοποιούν το ίδιο layer και να αποδεικνύουν την απομόνωσή τους με tests.
- **Release gate για το C9:** καμία migration· κανένα product path δεν καλεί το feed· δεν υπάρχει ακόμη καμία
  αυτόματη παραγωγή ευκαιριών, οπότε το feed μένει κενό μέχρι ένα εγκεκριμένο πακέτο επεξεργασίας.
- **Release gate για το C8 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0048` δημιουργεί **τέσσερις
  άδειους** πίνακες· κανένα backfill από `UserCompanyLead`/`RadarMatch`. Καμία αυτόματη επεξεργασία: το
  `materialize_opportunities_for_signal` καλείται μόνο εσωτερικά, χωρίς task/schedule/signal hook.
- **Release gate για το C7:** καμία migration· η ανάλυση **παράγεται κατ' απαίτηση** και δεν καταγράφεται ιστορικά —
  εξαρτάται από το `as_of` και από την τρέχουσα διαμόρφωση του Radar, οπότε το C8 πρέπει να αποφασίσει τι «κλειδώνει».
- **Release gate για το C6:** καμία migration· κανένα product path δεν καλεί το scoring. Το scoring εξαρτάται από
  `as_of`, οπότε το πότε «κλειδώνει» ένα score το αποφασίζει το μελλοντικό Opportunity.
- **Release gate για το C5:** καμία migration· ο matcher δεν καλείται από κανένα product path. Χρειάζεται A5 reference data
  και B3/B5 snapshots στην παραγωγή πριν δώσει ουσιαστικά αποτελέσματα· NEW_COMPANY χωρίς σύγχρονο snapshot δίνει
  INSUFFICIENT_STATE για Radars με κριτήρια κατάστασης.
- **Release gate για το C4 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0047` δημιουργεί δύο **άδειους**
  πίνακες· κανένα seed. Τα templates χρειάζονται A5 reference data και ελεγμένες αντιστοιχίσεις ΚΑΔ.
- **Release gate για το C3 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0046` δημιουργεί έξι **άδειους**
  πίνακες Radar· καμία data migration από `CustomerRadar`.
- **Εκκρεμής απαίτηση — layer επαφών εταιρείας:** το Dossier δείχνει ήδη το τηλέφωνο ΓΕΜΗ μέσω του hotfix
  (read-through από `raw_data["phone"]`, βλ. «Τρέχουσα κατάσταση»), που είναι **προσωρινή γέφυρα**. Πρέπει να
  αντικατασταθεί από το εξής, με ρητή πηγή, χρόνο επαλήθευσης, διατήρηση και σημασιολογία ιδιωτικότητας. Κανόνας αρχιτεκτονικής:
  τα στοιχεία επικοινωνίας ζουν σε **ξεχωριστό, ελαχιστοποιημένο layer επαφών εταιρείας** (contact points) με
  δική του πηγή/διατήρηση/ιδιωτικότητα — **όχι** σε `CompanySnapshot`, `CompanySignal`, timeline,
  `OrganizationProfile` ή ICP, και ποτέ έκθεση του raw GEMI payload· το Dossier διαβάζει μόνο το εγκεκριμένο
  layer. Λόγος: τηλέφωνο/email μπορεί να είναι προσωπικά δεδομένα (ατομικές επιχειρήσεις).
- **Release gate για το C2 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0045` δημιουργεί έξι
  **άδειους** πίνακες ICP· καμία data migration. Το ICP προϋποθέτει συγχρονισμένα reference data (A5) για
  να έχει κριτήρια.
- **Release gate για το G6 — μία μόνο additive migration:** η `0055` δημιουργεί **μόνο** τον άδειο πίνακα
  `GemiRequestAttempt`. Το deploy δεν κάνει καμία κλήση ΓΕΜΗ και δεν προγραμματίζει τίποτα: ο πίνακας
  γεμίζει μόνος του από τις **υπάρχουσες** εργασίες (daily/intraday import) καθώς τρέχουν. **Η 0055 εφαρμόζεται
  από το `preDeployCommand` του Render (`python manage.py migrate`)** — **όχι** χειροκίνητα μετά το deploy· η
  επαλήθευση γίνεται από το deploy log του Render (`Applying gemiapp.0055_gemi_request_attempt... OK`). Μετά το
  deploy:
  `python manage.py report_gemi_request_budget --hours 24` (μόνο ανάγνωση) για τουλάχιστον έναν πλήρη κύκλο
  24 ωρών πριν συζητηθεί οποιαδήποτε απόφαση G4.
- **Release gate για το C1 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0044` δημιουργεί τρεις
  **άδειους** πίνακες· καμία data migration. Multi-member ενεργοποίηση μόνο μετά το **G5**.
- **Release gate για το B5 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0043` δημιουργεί μόνο
  τον πίνακα evidence, που μένει **άδειος**. Στο release, μετά από πραγματικά snapshots του B4:
  `materialize_snapshot_change_signals --dry-run`, έλεγχος ακρίβειας των shadow σημάτων (ιδίως γύρω από
  τη μετάβαση ΚΑΔ 2026-03-01), και μόνο μετά ξεχωριστή απόφαση για σύνδεση B4→B5 ή LIVE σήματα.
- **Release gate για το B4 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0042` δημιουργεί μόνο
  τον πίνακα `GemiRefreshRun`, που μένει **άδειος**: το deploy του κώδικα δεν κάνει καμία κλήση ΓΕΜΗ.
  Πριν από οποιαδήποτε εκτέλεση χρειάζεται **G6** (επαλήθευση request-budget) πέρα από τα G0/G1, και στο
  release: `sync_gemi_reference_data` (χωρίς A5 ids κανένα Radar δεν μεταφράζεται), μετά
  `run_gemi_company_refresh --plan-only`, και μόνο τότε μικρή ρητή εκτέλεση με `--max-requests 2`.
  Το task μπαίνει στο `apps.SCHEDULES` μόνο μετά από ξεχωριστή απόφαση.
- **Release gate για το B3 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0041` δημιουργεί μόνο
  τον πίνακα snapshots, που μένει **άδειος**: κανένα baseline δεν φτιάχνεται από παλιά `raw_data`. Τα πρώτα
  baselines θα προκύψουν από τις φρέσκες παρατηρήσεις του B4 — και (από 2026-09-19) από το detection-time
  baseline των NEW_COMPANY από αξιόπιστη εγγραφή του importer. Αλλαγή της κανονικής κατάστασης απαιτεί ρητή
  αύξηση του `COMPANY_SNAPSHOT_SCHEMA_VERSION`.
- **Release gate για το B2 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** τα σήματα NEW_COMPANY
  παράγονται **μόνο** από ευρήματα Discovery v2, που παραμένει shadow. Άρα: πρώτα το 14ήμερο shadow gate του
  A10 και η αξιολόγηση των διαφορών legacy/v2· μετά ξεχωριστή, ρητή απόφαση για LIVE σήματα. Καμία σύνδεση
  με Radars/digests/monitoring πριν από αυτό.
- **Release gate για το B1 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0039` δημιουργεί μόνο τον
  πίνακα `CompanySignal`, ο οποίος μένει **άδειος**: κανένας detector, κανένα task, καμία σύνδεση με πελάτες.
  Ιστορικά σήματα από το B2 πρέπει να δημιουργούνται σε `shadow` ώστε να μη γίνουν ποτέ ειδοποιήσιμα.
- **Release gate για το A10 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0038` δημιουργεί μόνο
  τους πίνακες discovery. Στο release: migration, `bootstrap_gemi_discovery_v2` (επαληθευμένη σάρωση),
  μετά καθημερινές shadow εκτελέσεις και `run_gemi_discovery_v2 --compare <ημερομηνία>`. **Το Discovery v2
  δεν αντικαθιστά τον legacy importer** πριν από ≥14 ημέρες shadow, έλεγχο των διαφορών legacy/v2, καθαρά
  ordering guardrails και αποδεκτό budget· το `GEMI_DISCOVERY_V2_ENABLED` μένει 0 μέχρι τότε.
- **Release gate για το A9 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0037` δημιουργεί μόνο τους
  πίνακες παρακολούθησης. Στο release: `backfill_gemi_company_metadata`, μετά
  `recompute_gemi_company_monitoring --dry-run`, κανονικό, δεύτερο (0 αλλαγές). Το nightly task μπαίνει στο
  `apps.SCHEDULES` μόνο μαζί με τον refresh collector· event-driven recompute (π.χ. αλλαγή Radar) μόνο πίσω
  από flag.
- **Release gate για το A8 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0036` δημιουργεί μόνο τον
  πίνακα `ActivityCodeKadLink`. Στο release (μετά το A5 sync σε staging): `reconcile_gemi_kad_catalogue
  --dry-run`, κανονικό, δεύτερο (0 αλλαγές), `--list-radar-criteria`. Η αναφορά κατάταξης ΚΑΔ και κριτηρίων
  Radars με πραγματικά reference data είναι προϋπόθεση για οποιοδήποτε cutover του επιλογέα
  (`GEMI_KAD_PICKER_CURRENT_TAXONOMY_ONLY`) ή migration κριτηρίων.
- **Release gate για το A7 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0035` αφαιρεί τον
  πλήρη unique και δημιουργεί δύο partial unique indexes στο `CompanyActivity` (σύντομο κλείδωμα εγγραφών
  σε PostgreSQL· μέγεθος πίνακα production άγνωστο). Στο release: migration, `backfill_gemi_company_activities
  --dry-run`, κανονικό, δεύτερο (0 αλλαγές), `report_gemi_activity_matching_parity`. Η ενεργοποίηση του
  `GEMI_MATCH_CURRENT_ACTIVITIES_ONLY` είναι ξεχωριστή, εγκεκριμένη απόφαση προϊόντος (αλλάζει ποιες
  εταιρείες λαμβάνουν οι πελάτες). Rollback: `migrate gemiapp 0034` (αυτόνομο, αφαιρεί μόνο τις παράγωγες
  γραμμές εκτός legacy list).
- **Release gate για το A6 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η `0034` είναι μόνο
  nullable `AddField` (άμεση σε PostgreSQL). Στο release: migration, `backfill_gemi_company_metadata
  --dry-run`, έλεγχος πληθών, κανονικό backfill, δεύτερο backfill που πρέπει να δώσει 0 αλλαγές. Τα
  πεδία δεν διαβάζονται από τίποτα· indexes μόνο όταν προστεθούν queries (σε PostgreSQL με
  `CREATE INDEX CONCURRENTLY`).
- **Release gate για το A5 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η migration
  `0033_gemi_reference_data` δεν εφαρμόζεται σε production πριν από το G0/G1. Κατά το release: πρώτος
  συγχρονισμός με `--dry-run`, έπειτα κανονικός, και εβδομαδιαίο schedule μόνο όταν υπάρχει ξεχωριστό
  GEMI key ή μετρημένο περιθώριο στο κοινό όριο των 8 αιτημάτων/λεπτό.
- **Release gate για το A4 — `PRODUCTION_MIGRATION_STATUS = BLOCKED_BY_G0_G1`:** η migration
  `0032_gemi_source_records` **δεν** εφαρμόζεται σε production πριν υπάρξει staging βάση (G0) και
  δοκιμαστεί εκεί με forward/rollback (G1). Όταν ανοίξει: `GEMI_SOURCE_RECORDS_ENABLED` παραμένει `0`
  μέχρι το cutover του 2.0, το purge task προστίθεται στο `apps.SCHEDULES`, και οι περίοδοι retention
  επιβεβαιώνονται από τη νομική/DPO πολιτική. Σημείωση: το `Company.raw_data` εξακολουθεί να κρατά
  ολόκληρη την εγγραφή ΓΕΜΗ με persons· το A4 δεν το άγγιξε, η τύχη του είναι ξεχωριστή εργασία
  συμμόρφωσης.
- **Γνωστό σφάλμα δεδομένων, για A6/A7 (δεν διορθώθηκε στο A3):** οι εγγραφές ΓΕΜΗ δεν περιέχουν
  `isActive` (ούτε στο `status`), οπότε το `company_defaults()` αποθηκεύει `is_active=True` σε
  **κάθε** εταιρεία — και σε διαγραμμένες. Επαληθεύτηκε στη dev βάση: 17.799/17.799 ενεργές, μαζί με
  8 σε κατάσταση «Διαγραφή». Το φίλτρο «Μόνο ενεργές επιχειρήσεις» των Radars δεν έχει επομένως
  πραγματικό αποτέλεσμα. Η διόρθωση χρειάζεται τα companyStatuses reference data (A5) και parity
  report, γιατί αλλάζει ποια leads βλέπουν οι πελάτες.
- **Release gate για το A2 — ΚΛΕΙΣΤΟ (2026-09-20):** η απαίτηση είναι «**ERROR σε production από τα
  `gemiapp.ingestion.client` / `gemiapp.services` πρέπει να ειδοποιεί ενεργά έναν operator**», **όχι** «τρέξε
  Sentry». Υλοποιήθηκε με `AdminEmailHandler` + `ADMINS` από το `SUPERADMIN_EMAILS`, πάνω στο υπάρχον SMTP
  (βλ. «ενεργή ειδοποίηση operator»). Το Sentry παραμένει **προαιρετικό** και **δεν** υπάρχει σε αυτό το
  deployment. Απομένει μία επιβεβαίωση σε production: `manage.py sendtestemail --admins`. Το `ImportRun`
  (Superadmin → GEMI Pipeline), τα logs και οι γραμμές django-q μένουν **παθητικά** τεκμήρια. Πριν από
  οποιαδήποτε 2.0 migration σε production απαιτούνταν επίσης: staging βάση (**G0 κλειστό**), forward/rollback
  (**G1 κλειστό**) και γνωστός όγκος δεδομένων production (**4.903 / 33.391, καταγεγραμμένος**)· το ξεχωριστό
  GEMI API key για staging μένει ανοιχτό ως A5/G6, όχι ως dark-deploy blocker.
- **Διόρθωση τεκμηρίωσης billing:** το README, το AI_SUMMARY και αυτό το αρχείο γράφουν ότι οι
  πληρωμές είναι κλειστές (beta), ενώ η production σελίδα pricing δείχνει ενεργό checkout
  (`render.yaml`, commit `e158012`). Μέχρι να επιβεβαιωθεί, το billing θεωρείται LIVE.
- **Εκκρεμής αρχιτεκτονική απαίτηση — layer επαφών εταιρείας:** το hotfix τηλεφώνου είναι προσωρινή γέφυρα
  (read-through από `raw_data`). Πρέπει να αντικατασταθεί από **ξεχωριστό, ελαχιστοποιημένο layer επαφών
  εταιρείας** (contact points) με ρητή πηγή, χρόνο επαλήθευσης, διατήρηση και σημασιολογία ιδιωτικότητας.
  Τα στοιχεία επικοινωνίας **δεν** μπαίνουν σε snapshots, σήματα, timeline, organization profile, ICP ή Radar
  (Gemi Leads 2.0), και το raw GEMI payload δεν εκτίθεται ποτέ. Το τηλέφωνο ατομικής επιχείρησης μπορεί να
  είναι προσωπικό δεδομένο.

*(Όλα τα βήματα παραγωγής, Stripe integration, Email & Domain, Landing Page, Paid Subscription Logic και Superadmin Control Center έχουν ολοκληρωθεί. Η εφαρμογή είναι σε beta: το billing παραμένει σκόπιμα κλειστό. Το cold outreach είναι επίσης σκόπιμα παγωμένο και δεν αποτελεί εκκρεμότητα επανενεργοποίησης.)*

- **Visual review του approved Signal Ledger refinement:** να αξιολογηθούν τα πέντε `refined-*` captures και το interactive prototype στο `docs/gemi-leads-ui-study/`. Καμία production υλοποίηση δεν πρέπει να ξεκινήσει πριν εγκριθούν το state grammar, η refined sidebar/source metadata, η selected-signal ιεραρχία και οι mobile Signals/Lead ροές.
- **Visual review του Gemi Leads UI study:** να αξιολογηθούν οι τρεις κατευθύνσεις στο `docs/gemi-leads-ui-study/`, με προτεραιότητα στο interactive prototype **Signal Ledger**. Καμία production υλοποίηση δεν πρέπει να ξεκινήσει πριν εγκριθούν art direction, ορατή ονομασία Dashboard/Signals και επιθυμητή desktop density.
- **Τέταρτος λογαριασμός χωρίς πρόσβαση.** Στις 2026-08-23 μόνο 3 λογαριασμοί έχουν entitlement (`naikos98@gmail.com`, `info@gemileads.gr`, `iotellis@taxville.gr`), ενώ η πρόθεση ήταν 4. Ο `nikoskaravn@gmail.com` είναι ενεργός αλλά χωρίς entitlement. Χρειάζεται complimentary grant από το `/superadmin/users/<id>/` αν προορίζεται για πρόσβαση.
- **Δύο λογαριασμοί παραμένουν κολλημένοι ανεπιβεβαίωτοι:** `filtellis@gmail.com` και `naikos98@hotmail.com`. Είναι ακριβώς οι δύο που εντοπίστηκαν στις 2026-08-22· ο μηχανισμός `resend_verification` υλοποιήθηκε αλλά **δεν εφαρμόστηκε ποτέ σε αυτούς**. Τρέξε `manage.py resend_verification_emails --email filtellis@gmail.com --email naikos98@hotmail.com --dry-run` και μετά χωρίς `--dry-run`.
- **Ο λογαριασμός `naikos98@hotmail.co`** μοιάζει με τυπογραφικό λάθος του `naikos98@hotmail.com` (λείπει το `m`). Να επιβεβαιωθεί πριν διαγραφεί ή συγχωνευτεί.
- **Ανακαλύφθηκαν ~20 εγγραφές `Company` με παραληρηματικό `incorporation_date` στο μέλλον** (π.χ. έτη 2029, 2055, 2100, 3006, 9011). Το `company_defaults()` (`gemiapp/services.py`) έχει clamp `if inc_date > today: inc_date = today`, άρα αυτές οι τιμές δεν θα έπρεπε να είναι δυνατές πλέον· πιθανώς προέρχονται από πριν μπει το clamp ή από παρακαμπτόμενο μονοπάτι. Δεν επηρεάζει το matching/digest (φιλτράρουν με `incorporation_date=target_date`/`<=today`), αλλά χρειάζεται ξεχωριστή διερεύνηση.
- Πριν ανοίξει το billing: `STRIPE_PRICE_ENTERPRISE` στο Render (το System Health το δείχνει ως Warning), επιβεβαίωση ότι το `DEFAULT_FROM_EMAIL` δείχνει στο πιστοποιημένο `notifications@send.gemileads.gr`. Τα στοιχεία του παρόχου/υπεύθυνου επεξεργασίας (NORVA Ι.Κ.Ε.) είναι πλέον σταθερά στο `config/settings.py`· τυχόν παλιές μεταβλητές `LEGAL_CONTROLLER_NAME`/`LEGAL_VAT`/`LEGAL_GEMI`/`LEGAL_ADDRESS`/`LEGAL_CONTACT_EMAIL` στο περιβάλλον του server αγνοούνται και μπορούν να διαγραφούν.
- Όταν ενεργοποιηθούν οι πληρωμές: `LEGAL_BILLING_ACTIVE=1` και, όταν φύγει και η ένδειξη beta, `BETA_MODE=0`.

## Ιστορικό εργασιών

- **2026-09-26 — Free: ημερήσιο email digest.** Αλλαγές: `gemiapp/services.py` (`FREE_DIGEST_FREQUENCY`,
  `digest_skip_reason`, `radar_features` σε `send_digests` και `send_user_yesterday_digest`),
  `templates/emails/daily_digest.html/.txt` (ενότητα Ραντάρ και CSV μόνο με entitlement), `templates/pricing.html`
  (digest στα «Περιλαμβάνει»), `gemiapp/tests.py`. Καμία migration, κανένα νέο schedule.

- **2026-09-26 — Pricing: γραμμή Free.** Αλλαγές: `templates/pricing.html` (γραμμή Free, δύο φράσεις
  «όλα τα πλάνα»), `static/css/product-ui.css` (λίστες περιλαμβάνει/δεν περιλαμβάνει), `gemiapp/tests.py`
  (`PricingFreePlanTests`). Επαλήθευση: render σε desktop (επισκέπτης και συνδεδεμένος Free) και mobile, `check`,
  `makemigrations --check`, 2.132 tests OK.

- **2026-09-26 — Pending-company hydration (κλειστό).** Νέα: `gemiapp/pending_company_hydration.py`,
  `gemiapp/management/commands/hydrate_pending_discovery_companies.py`,
  `gemiapp/test_pending_company_hydration.py`. Αλλαγές: `config/settings.py` και `.env.example` (flag = 0),
  `AGENTS.md`. Επαλήθευση: 26 focused tests + mutation check, discovery/B2/G4/client/metrics/pipeline suites (310
  OK), `check`, `makemigrations --check`, 2.124 tests OK, άρνηση της εντολής με flag 0 σε αντίγραφο της dev βάσης.
  Καμία migration, κανένα schedule, κανένα άγγιγμα στο G4.

- **2026-09-26 — Email templates στο Signal Ledger.** Νέα: `templates/emails/_base.html`, `_button.html`,
  `registration/password_reset_email.txt`, `static/images/gemi-leads-logo-email.png` (αντικατέστησε το
  `gemi-leads-logo-stacked.png`, που δεν έγινε ποτέ deploy). Αλλαγές: `daily_digest.html/.txt`,
  `verification.html/.txt`, `registration/password_reset_email.html`, `config/urls.py` (`email_template_name`),
  `gemiapp/tests.py`. Επαλήθευση: render με δείγματα σε desktop και 375px, `check`, `makemigrations --check`,
  2.101 tests OK.

- **2026-09-26 — Νέο branding (icon + full logo).** Νέα: 6 assets στο `static/images/` (βλ. «Τρέχουσα
  κατάσταση»), `BrandAssetTests`. Αλλαγές: `templates/base.html` (favicon set, og:image, JSON-LD logo, δύο
  topbars), `templates/superadmin/base.html` (favicon set, topbar), `static/css/product-ui.css` (`.brand-logo`,
  αφαίρεση `.product-mark`), 3 HTML emails, `gemiapp/tests.py` (budget/og tests στα νέα ονόματα). Διαγραφή
  `static/images/logo.png`. Επαλήθευση: `check`, `makemigrations --check`, 2.100 tests OK, έλεγχος σε browser
  (desktop/tablet/mobile, public + product, email header)· το superadmin header ελέγχθηκε μέσω test client.

- **2026-09-21 — G6 request-budget observability.** Νέα: `gemiapp/ingestion/request_metrics.py`,
  `gemiapp/management/commands/report_gemi_request_budget.py`, `gemiapp/test_gemi_request_metrics.py`,
  migration `0055_gemi_request_attempt`. Αλλαγές: `gemiapp/models.py` (`GemiRequestAttempt`),
  `gemiapp/ingestion/client.py` (μία παρατήρηση ανά απόπειρα, μέτρηση αναμονής budget),
  `config/settings.py` (`GEMI_REQUEST_METRICS_ENABLED`), `config/fast_test_runner.py` (off στο suite),
  και τα deliberate migration-head pins σε έξι test modules (0054 → 0055). Επαλήθευση: `check`,
  `makemigrations --check`, 2.098 tests OK. **Καμία** αλλαγή σε matching, scoring, billing, organization
  logic, schedules ή στο ceiling· **G4 NOT STARTED**, LIVE απαγορευμένη.

- **2026-09-21 — Radar picker chevron toggle (bugfix).** Αλλαγές: `static/js/app.js` (stopPropagation στο
  chevron, ο field opener αγνοεί κλικ του chevron, abort on close), νέο `gemiapp/jstests/picker_dropdown.mjs`,
  `gemiapp/test_organization_radar_ui.py` (`PickerDropdownTests` + static έλεγχος του toggle). Επαλήθευση:
  `check`, `makemigrations --check`, 2.052 tests OK, έλεγχος σε browser· **καμία migration**.

- **2026-09-20 — Organization Radar criteria pickers: dropdown (UX).** Αλλαγές:
  `gemiapp/reference_search.py` (`browse_reference`, `reference_options`), `gemiapp/views.py` (το endpoint
  απαντά και σε κενό `q`), `templates/includes/reference_picker.html` (chevron, combobox),
  `templates/organizations/radar_form.html` (placeholders), `static/js/app.js` (άνοιγμα σε κλικ/focus,
  capture-phase outside click, «ήδη επιλεγμένο» εκτός λίστας), `static/css/product-ui.css`,
  `gemiapp/test_reference_search.py`, `gemiapp/test_organization_radar_ui.py`. Επαλήθευση: `check`,
  `makemigrations --check`, 2.050 tests OK, browser έλεγχος· **καμία migration**, καμία αλλαγή στην
  αναπαράσταση που διαβάζει ο matcher.

- **2026-09-20 — Organization Radar criteria pickers (UX).** Νέα: `gemiapp/reference_search.py`,
  `templates/includes/reference_picker.html`, `gemiapp/test_reference_search.py`. Αλλαγές: `gemiapp/views.py`
  (+ endpoint `reference_search`), `gemiapp/urls.py`, `gemiapp/organization_radar_form.py`
  (`RadarForm.selections`, μόνο παρουσίαση), `templates/organizations/radar_form.html`, `static/js/app.js`,
  `static/css/product-ui.css`, `gemiapp/test_organization_radar_ui.py`. Επαλήθευση: `check`,
  `makemigrations --check`, 2.028 tests OK· **καμία migration**, καμία αλλαγή στην αναπαράσταση που
  διαβάζει ο matcher.

- **2026-09-20 — Επανάληψη επιβεβαίωσης της πρόβας G1 (staging).** Ολόκληρος ο κύκλος ξανά από την αρχή
  (rollback → baseline 0031 → forward → rollback → reapply) στην ίδια βάση, σε **μεταγενέστερο commit**, με τη
  λίστα στηλών του 0031 **ξανα-παραγόμενη** από το `information_schema`: ίδια σύνολα στηλών, **και τα 13 digests
  αναπαρήχθησαν byte-για-byte**, PASS σε κάθε στάδιο. Χρόνοι: rollback 19,04 s · forward **31,56 s** (`0034`
  3,66 s, `0035` 4,11 s) · rollback 18,15 s · reapply **35,90 s** (`0034` 4,42 s, `0035` 4,63 s). D37
  `1→0→1→0→1`, schedules 4→3→4→3→4 χωρίς διπλότυπα, ids legacy (1, 3, 22) αμετάβλητα, `check` /
  `migrate --check` / `makemigrations --check` καθαρά. Μόνο τεκμηρίωση: `docs/RELEASE_READINESS.md`, `AGENTS.md`.
  Καμία επαφή με production, κανένα αίτημα ΓΕΜΗ.

- **2026-09-20 — Ενεργή ειδοποίηση operator για αποτυχίες ingestion (χωρίς Sentry).** Νέα:
  `config/operator_alerts.py` (`OperatorEmailHandler`), `gemiapp/test_operator_alerts.py` (21 tests). Αλλαγές:
  `config/settings.py` (`operator_admins`, `ADMINS`, `OPERATOR_ALERT_LOGGERS`, `LOGGING`),
  `config/fast_test_runner.py` (`silence_operator_alerts`), `AGENTS.md`, `docs/RELEASE_READINESS.md` (η πύλη
  ξαναγράφηκε ως απαίτηση, όχι ως προϊόν). Επαλήθευση: `check`, `check --deploy`, `makemigrations --check`,
  1.994 tests OK· καμία migration, καμία αλλαγή σε ingestion/retries/billing.

- **2026-09-20 — Τελική πρόβα G1 parity στο staging.** Rollback → baseline 0031 → forward → rollback → reapply,
  με digest πάνω σε **όλες** τις στήλες του 0031 σε κάθε στάδιο· 13/13 datasets ταυτίστηκαν παντού· D37
  `1→0→1→0→1`· `check` και `migrate --check` καθαρά. Αλλαγές τεκμηρίωσης: `docs/RELEASE_READINESS.md`
  (`G1_STATUS = PASSED`, χρόνοι, πίνακας parity), `AGENTS.md`. Καμία αλλαγή κώδικα, καμία production επαφή,
  κανένα αίτημα ΓΕΜΗ, καμία merge, κανένα deploy.

- **2026-09-20 — Pre-dark-deployment closure (τεκμηρίωση + data safety).** Αλλαγές: `docs/RELEASE_READINESS.md`
  (ταξινόμηση τεκμηρίων A/B/C, αφαίρεση του κριτηρίου web+worker service, production volume 4.903/33.391, staging
  parity snapshot με digests, τοπική πρόβα εφαρμογής, build rehearsal, Sentry ως ο ένας χειροκίνητος blocker),
  `.gitignore` (`*.dump`, `*.sql`, `*.sql.gz`). Καμία αλλαγή κώδικα, καμία migration, κανένα αίτημα ΓΕΜΗ, καμία
  εγγραφή στο staging, κανένα deploy, καμία merge.

- **2026-09-20 — Deterministic G4 shadow cycle.** Νέα: `gemiapp/g4_shadow_cycle.py`,
  `manage.py run_g4_shadow_cycle`, `gemiapp/test_g4_shadow_cycle.py` (23 tests). Αλλαγή:
  `gemiapp/opportunity_pipeline.py` (`collect_pipeline_runs()` — παρατήρηση των on-commit runs, καμία αλλαγή
  συμπεριφοράς). Επαλήθευση: `check`, `makemigrations --check`, 1.973 tests OK· καμία migration, κανένα
  επιπλέον αίτημα ΓΕΜΗ πέρα από το ίδιο το discovery run, κανένα schedule.

- **2026-09-20 — Discovery v2 frontier classification fix.** Αλλαγές: `gemiapp/ingestion/discovery.py` (κατάταξη
  ανά εγγραφή με το σύνορο της αρχής του run, `rediscovered_local_records`, ingest που δεν ξαναγράφει τοπική
  γραμμή), `gemiapp/new_company_signals.py` (μόνο docstrings/σχόλια). Νέο: `gemiapp/test_discovery_frontier.py`
  (13 tests). Επαλήθευση: `check`, `makemigrations --check`, 1.950 tests OK· καμία migration, κανένα αίτημα ΓΕΜΗ.

- **2026-09-19 — NEW_COMPANY state at detection (Option A).** Αλλαγές: `gemiapp/new_company_signals.py`
  (detection-time baseline + `detected_at = max(discovery, baseline)`, counters), `gemiapp/ingestion/company_metadata.py`
  (`company_is_admin_touched`), `gemiapp/company_snapshots.py` (docstring). Νέο: `gemiapp/test_new_company_state.py`.
  Επαλήθευση: `check`, `makemigrations --check`, 1.937 tests OK· καμία migration, κανένα αίτημα ΓΕΜΗ.

- **2026-09-19 — G2 SHADOW opportunity pipeline.** Νέα: `gemiapp/opportunity_pipeline.py`,
  `gemiapp/management/commands/process_shadow_signals.py`, `gemiapp/test_shadow_pipeline.py`. Αλλαγές:
  `company_signals.py` (on_commit hook στη δημιουργία signal), `opportunity_score_breakdown.py` (καθαρή εξαγωγή
  `explain_opportunity_scores_for_report`). Επαλήθευση: `check`, `makemigrations --check`, 1.922 tests OK, dry-run
  της εντολής στο αντίγραφο της dev βάσης.
- **2026-09-19 — Organization Radar create/edit (customer UI).** Νέα: `gemiapp/organization_radar_form.py`,
  `templates/organizations/radar_form.html`, `gemiapp/test_organization_radar_ui.py`. Αλλαγές: `organization_access.py`
  (editor + writes + αναγνώσιμη περίληψη), `organization_views.py`, `urls.py`, `radars.html`, `product-ui.css`,
  `organization_radars.py` (μόνο docstring), guards. Επαλήθευση: `check`, `makemigrations --check`, 1.899 tests OK,
  έλεγχος φόρμας στο browser με προσωρινό fixture `WSFIX` που αφαιρέθηκε πλήρως.
- **2026-09-19 — Compatibility layer UserSubscription → Organization.** Νέα: `gemiapp/organization_entitlement.py`,
  `gemiapp/management/commands/provision_organization_for_user.py`, `gemiapp/test_organization_entitlement.py`.
  Αλλαγές: `organization_access.py` (entitlement στο context, `OrganizationNotEntitled`, πλοήγηση), `organization_views.py`
  (legacy paywall για μέλη μη entitled οργανισμών), test fixtures/guards. Επαλήθευση: `check`, `makemigrations --check`,
  1.883 tests OK, dry-run της εντολής στο αντίγραφο της dev βάσης (τίποτα δεν γράφτηκε).
- **2026-09-19 — Gemi Leads 2.0 customer workspace (dashboard & πλοήγηση).** Νέα: `templates/organizations/`
  (`dashboard.html`, `opportunities.html`, `tasks.html`, `radars.html`, `_workspace_bar.html`, `_workspace_rail.html`,
  `_opportunity_row.html`, `_task_row.html`), `gemiapp/test_customer_workspace.py`. Αλλαγές: `organization_access.py`
  (workspace read models), `organization_views.py` (4 GET views + context processor), `urls.py`, `settings.py`
  (context processor), `opportunity_feed.py` (`latest_signal_mode`), `base.html`, `company_opportunity.html`,
  `notifications.html`, `product-ui.css`, guard tests. Επαλήθευση: `check`, `makemigrations --check`, 1.855 tests OK,
  οπτικός έλεγχος desktop/mobile.
- **2026-09-16 — Gemi Leads 2.0 B3 (ελαχιστοποιημένα snapshots εταιρειών).** Νέα: `CompanySnapshot`
  (`gemiapp/models.py`), migration `0041_company_snapshot.py`, `gemiapp/company_snapshots.py` (κανονική
  κατάσταση, state hash, writer), `gemiapp/test_company_snapshots.py`. Αλλαγές: `admin.py` (read-only).
  Επαλήθευση: 1.080 tests OK, `check` / `makemigrations --check` / build:css καθαρά, forward → 0 snapshots →
  προσομοίωση A→A→B→B→A (3 γραμμές) → parity → reverse → reapply (ξανά 0) στο αντίγραφο της dev βάσης.
  Importer, discovery, monitoring, signals, services, views, tasks, schedules και billing αμετάβλητα.
- **2026-09-16 — Gemi Leads 2.0 B2 (παραγωγός σημάτων NEW_COMPANY).** Commit `6960414`. Νέα: `CompanySignalDiscoveryEvidence`
  (`gemiapp/models.py`), migration `0040_company_signal_discovery_evidence.py`,
  `gemiapp/new_company_signals.py`, `manage.py materialize_new_company_signals`,
  `gemiapp/test_new_company_signals.py`. Αλλαγές: `company_signals.py` (ο κανόνας `new_company:v1` έγινε
  implemented), `admin.py` (read-only), `test_company_signals.py` (δύο προσδοκίες του B1 που άλλαξε σκόπιμα
  το B2). Επαλήθευση: 1.048 tests OK, `check` / `makemigrations --check` / build:css καθαρά, forward →
  dry-run → υλοποίηση → δεύτερη εκτέλεση → parity → reverse → reapply στο αντίγραφο της dev βάσης (0
  ευρήματα, άρα 0 σήματα). Importer, discovery, monitoring, services, views, tasks, schedules και billing
  αμετάβλητα.
- **2026-09-16 — Gemi Leads 2.0 B1 (θεμέλιο Signals· αρχή Stage B).** Commit `2b926b2`. Νέα: `CompanySignal`
  (`gemiapp/models.py`), migration `0039_company_signal.py`, `gemiapp/company_signals.py` (ταξινομία,
  ταυτότητα γεγονότος, registry κανόνων, υπηρεσία εγγραφής), `gemiapp/test_company_signals.py`. Αλλαγές:
  `admin.py` (read-only). Επαλήθευση: 1.025 tests OK, `check` / `makemigrations --check` / build:css καθαρά,
  forward → άδειος πίνακας → parity → reverse → reapply στο αντίγραφο της dev βάσης. Importer, discovery,
  services, views, tasks, schedules και billing αμετάβλητα.
- **2026-09-16 — Gemi Leads 2.0 A10 (Discovery v2 σε shadow· τέλος Stage A).** Commit `a4294b7`. Νέα: `GemiDiscoveryCursor`,
  `GemiDiscoveryRun`, `GemiDiscoveryObservation` (`gemiapp/models.py`), migration `0038_gemi_discovery.py`,
  `gemiapp/ingestion/discovery.py`, `manage.py run_gemi_discovery_v2`, `manage.py bootstrap_gemi_discovery_v2`,
  `gemiapp/test_gemi_discovery.py`. Αλλαγές: `tasks.py` (shadow task χωρίς schedule), `admin.py` (read-only),
  `config/settings.py` και `.env.example` (flags = 0/1 shadow). Επαλήθευση: 998 tests OK, `check` /
  `makemigrations --check` / build:css καθαρά, forward → άρνηση χωρίς cursor → αναφορά σύγκρισης → parity →
  reverse → reapply στο αντίγραφο της dev βάσης. Καμία κλήση στο ΓΕΜΗ. Deploy, schedule, services, views,
  superadmin και billing αμετάβλητα.
- **2026-09-16 — Gemi Leads 2.0 A9 (σύνολο παρακολούθησης εταιρειών και πολιτική ανανέωσης).** Commit `1377452`. Νέα:
  `CompanyMonitoring` και `CompanyMonitoringReason` (`gemiapp/models.py`), migration `0037_company_monitoring.py`,
  `gemiapp/ingestion/monitoring.py`, `manage.py recompute_gemi_company_monitoring`,
  `gemiapp/test_gemi_company_monitoring.py`. Αλλαγές: `tasks.py` (task χωρίς schedule), `admin.py` (read-only).
  Επαλήθευση: 968 tests OK, `check` / `makemigrations --check` / build:css καθαρά, forward → dry-run →
  recompute → δεύτερο (0 αλλαγές) → reverse → reapply + recompute (ίδια κατάσταση) στο αντίγραφο της dev
  βάσης με parity matching/επιλογέα. Deploy, schedule, settings, services, views και billing αμετάβλητα.
- **2026-09-15 — Gemi Leads 2.0 A8 (συμφιλίωση καταλόγου ΚΑΔ).** Commit `11af564`. Νέα: `ActivityCodeKadLink`
  (`gemiapp/models.py`), migration `0036_activitycode_kad_links.py`, `gemiapp/ingestion/kad_catalogue.py`,
  `manage.py reconcile_gemi_kad_catalogue`, `gemiapp/test_gemi_kad_catalogue.py`. Αλλαγές: `views.py`
  (`kad_search` μέσω `kad_picker_queryset`, ίδιο αποτέλεσμα με το flag 0), `admin.py` (read-only),
  `config/settings.py` και `.env.example` (flag = 0). Επαλήθευση: 940 tests OK, `check` /
  `makemigrations --check` / build:css καθαρά, forward → dry-run → reconcile → δεύτερο → reverse → reapply
  στο αντίγραφο της dev βάσης με parity επιλογέα/καταλόγου/κριτηρίων/matching. Deploy, schedule, tasks,
  billing και services αμετάβλητα.
- **2026-09-15 — Gemi Leads 2.0 A7 (κανονικά μεταδεδομένα `CompanyActivity`, ασφαλές upsert).** Commit `c9e5dfa`. Νέα:
  migration `0035_companyactivity_canonical_metadata.py`, `gemiapp/ingestion/activities.py`,
  `manage.py backfill_gemi_company_activities`, `manage.py report_gemi_activity_matching_parity`,
  `gemiapp/test_gemi_company_activities.py`. Αλλαγές: `models.py` (`CompanyActivity`), `services.py`
  (upsert αντί delete/recreate, flag στο matching και στο radar preview), `views.py` (dashboard φίλτρο
  ΚΑΔ και καρτέλα εταιρείας σε `legacy_listed`), `superadmin/views.py`, `superadmin/services.py`,
  `ingestion/normalizer.py` (δημόσια `normalize_activity_entry` / `normalized_date_key`, ίδια συμπεριφορά),
  `ingestion/__init__.py`, `admin.py` (read-only), `config/settings.py` και `.env.example` (flag = 0).
  Επαλήθευση: 917 tests OK, `check` / `makemigrations --check` / build:css καθαρά, forward → dry-run →
  backfill → δεύτερο backfill → replay importer → parity report → αυτόνομο reverse με τις παράγωγες
  γραμμές παρούσες (ίδιο με το baseline) → reapply + backfill (ίδια κανονική κατάσταση) στο αντίγραφο της
  dev βάσης. Deploy, schedule, tasks και billing αμετάβλητα.
- **2026-09-15 — Gemi Leads 2.0 A6 (κωδικοί αναφοράς και lifecycle στο `Company`).** Commit `2771a09`. Νέα: migration
  `0034_company_gemi_metadata.py`, `gemiapp/ingestion/company_metadata.py`,
  `manage.py backfill_gemi_company_metadata`, `gemiapp/test_gemi_company_metadata.py`. Αλλαγές:
  `models.py` (9 nullable πεδία στο `Company`), `ingestion/normalizer.py` (δημόσια
  `normalize_event_date`), `ingestion/__init__.py`, `admin.py` (τα νέα πεδία read-only στο
  `CompanyAdmin`). Επαλήθευση: 876 tests OK, `check` / `makemigrations --check` / build:css καθαρά,
  forward → dry-run → backfill → δεύτερο backfill → reverse → reapply στο αντίγραφο της dev βάσης με
  checksums και parity report. Deploy, schedule, settings, services και billing αμετάβλητα.
- **2026-09-15 — Gemi Leads 2.0 A5 (GEMI reference tables και sync).** Commit `a473c5b`. Νέα: επτά πίνακες αναφοράς και
  `GemiReferenceSyncRun` (`gemiapp/models.py`), migration `0033_gemi_reference_data.py`,
  `gemiapp/ingestion/reference_data.py`, `manage.py sync_gemi_reference_data`,
  `gemiapp/test_gemi_reference_data.py`. Αλλαγές: `ingestion/normalizer.py` (δημόσια `normalize_text` /
  `normalize_identifier`), `ingestion/__init__.py`, `admin.py` (read-only), `tasks.py` (task χωρίς
  schedule), `test_gemi_source_records.py` (το migration test φορτώνει την 0032 με όνομα). Επαλήθευση:
  851 tests OK, `check` / `makemigrations --check` / build:css / collectstatic καθαρά, migration
  forward/reverse/forward σε αντίγραφο της dev βάσης. Deploy, schedule και settings αμετάβλητα.
- **2026-09-15 — Gemi Leads 2.0 A4 (minimised GEMI source records).** Commit `7574142`. Νέα: `GemiSourceRecord`
  (`gemiapp/models.py`), migration `0032_gemi_source_records.py`, `gemiapp/ingestion/source_records.py`,
  `manage.py purge_gemi_source_records`, `gemiapp/test_gemi_source_records.py`. Αλλαγές:
  `ingestion/client.py` (source recorder μόνο με ενεργό flag), `ingestion/errors.py`,
  `ingestion/__init__.py`, `admin.py`, `tasks.py` (purge task, χωρίς schedule), `config/settings.py`,
  `.env.example`. Επαλήθευση: 825 tests OK, `check` / `makemigrations --check` / build:css /
  collectstatic καθαρά, migration forward/reverse/forward σε αντίγραφο της dev βάσης. Το `render.yaml`
  και το `apps.SCHEDULES` δεν άλλαξαν. Καμία εφαρμογή σε production.
- **2026-09-15 — Gemi Leads 2.0 A3 (Normaliser v1).** Commit `4eeefe0`. Νέα: `gemiapp/ingestion/normalizer.py`,
  `gemiapp/test_gemi_normalizer.py`. Αλλαγές: `gemiapp/ingestion/__init__.py` (exports). Κανένα
  production code path δεν άλλαξε. Επαλήθευση: 798 tests OK, `check` / `makemigrations --check` /
  build:css / collectstatic καθαρά, καμία migration.
- **2026-09-15 — Gemi Leads 2.0 A2 (επικύρωση σχήματος απαντήσεων ΓΕΜΗ).** Commit `74def43`. Νέα:
  `gemiapp/ingestion/schemas.py`, `gemiapp/test_gemi_validation.py`. Αλλαγές: `ingestion/errors.py`
  (`GemiResponseValidationError`), `ingestion/client.py` (`family` στο `get`, validation στο
  `search_companies`), `ingestion/__init__.py`, `services.py` (ERROR log με το id του ImportRun).
  Επαλήθευση: 762 tests OK, `check` / `makemigrations --check` / build:css / collectstatic καθαρά.
- **2026-09-15 — Gemi Leads 2.0 A1 (κοινός GemiClient).** Commit `2026b93`. Βάση υλοποίησης: `main` (`d1a323c`, ίδιο
  με production) + `chore/norva-legal-identity` (`f372ef4` NORVA, `94a50b0` blueprint). Νέα:
  `gemiapp/ingestion/` (client, rate_budget, errors), `gemiapp/test_gemi_client.py`. Αλλαγές:
  `gemiapp/services.py` (`_get` μέσω GemiClient, backfill χωρίς ατέρμονο retry), `config/settings.py`
  (3 GEMI settings). Επαλήθευση: 692 tests OK πριν, 733 OK μετά· προσομοίωση 3 διεργασιών σε κοινό
  DatabaseCache: το πολύ 7 αιτήματα ανά κυλιόμενο λεπτό, 5,98/λεπτό, σωστή σειρά lanes.
- 2026-09-13: **Νομική ταυτότητα: το Gemi Leads είναι προϊόν της NORVA Ι.Κ.Ε.** Ο πάροχος/υπεύθυνος επεξεργασίας ορίζεται πλέον σταθερά στο `config/settings.py` (NORVA Ι.Κ.Ε. / NORVA P.C., ΑΦΜ 803388810, Δ.Ο.Υ. ΚΕΦΟΔΕ ΑΤΤΙΚΗΣ, ΓΕΜΗ 195879401000, EUID ELGEMI.195879401000, Μαυρομματαίων 6, Αθήνα 10682, info@norva.gr) αντί για env vars, ώστε ένα παλιό περιβάλλον server να μην μπορεί να δημοσιεύσει τα στοιχεία του προηγούμενου φορέα. Ενημερώθηκαν `legal/privacy.html`, `legal/terms.html`, footer στο `base.html` (το JSON-LD δεν άλλαξε: το `StructuredDataTests` απαγορεύει ρητά `address`/`vatID`/`legalName`) και το σχετικό FAQ του `home.html`, με ρητή δήλωση ότι δεν πρόκειται για επίσημη υπηρεσία του Γ.Ε.ΜΗ. Το `info@gemileads.gr` παραμένει ως επαφή προϊόντος/υποστήριξης. Κανένα functional/UI change.

- 2026-09-09: **Παραγωγή commercial & superadmin review captures & copy audit.** Δημιουργήθηκαν τα νέα review captures στο `docs/gemi-leads-ui-study/screenshots/` και στα artifacts: `marketing_superadmin_desktop.png` & `marketing_superadmin_users_desktop.png` (INTERNAL REVIEW ONLY), `marketing_pricing_desktop.png` (PUBLIC MARKETING SAFE), `marketing_landing_desktop.png` (PUBLIC MARKETING SAFE), `marketing_landing_full_desktop.png`, `marketing_landing_mobile.png` & `marketing_landing_full_mobile.png`. Πραγματοποιήθηκε πλήρης έλεγχος copy στη δημόσια landing page (`templates/home.html`), επιβεβαιώνοντας ότι δεν υπάρχουν αναφορές ή υποσχέσεις σε αυτόματη αποστολή cold outreach email. Το Signal Ledger UI και το backend διατηρήθηκαν 100% ανεπηρέαστα.
- 2026-09-09: **Δημιουργία privacy-safe marketing/demo dataset & review captures για το NORVA.** Υλοποιήθηκε η reversible εντολή διαχείρισης `seed_demo_marketing_data` (`gemiapp/management/commands/seed_demo_marketing_data.py`) που δημιουργεί 4 100% εικονικά ελληνικά εταιρικά προφίλ (`ΑΙΓΑΙΟ LOGISTICS ΜΟΝ. Ι.Κ.Ε.`, `HELLAS CLOUD & DATA LABS Α.Ε.`, `GREEN GRID SOLAR SOLUTIONS Ι.Κ.Ε.`, `KALYPSO HOSPITALITY & TRADING Ε.Ε.`) με εικονικά GEMI, ΑΦΜ, διευθύνσεις, emails, ΚΑΔ, διαχειριστές και αντιπροσωπευτικές καταστάσεις product lifecycle (Signals, Radar matches, Leads, Business Dossier). Υποστηρίζεται και παράμετρος `--clean` για άμεση αφαίρεση. Δημιουργήθηκε το documentation file `docs/marketing-demo-data.md` και παράχθηκαν τα νέα review captures στα artifacts (`marketing_signals_desktop`, `marketing_radars_desktop`, `marketing_dossier_desktop`, `marketing_leads_desktop`, `marketing_signals_mobile`, `marketing_dossier_mobile`). Το production Signal Ledger UI διατηρήθηκε ακέραιο.
- 2026-09-09: **Ολοκλήρωση compliance audit & operational shutdown του cold outreach subsystem.** Προστέθηκε η κατάσταση `cancelled` («Ακυρώθηκε») στο `CompanyOutreach.STATUSES` και εκτελέστηκε η migration `0031_cancel_pending_outreach.py` που μετέτρεψε όλες τις εκκρεμείς `pending`/`sending` εγγραφές σε `cancelled` με αιτιολογία compliance shutdown. Θωρακίστηκαν οι εντολές `requeue_dropped_outreach` και `prune_bot_suppressions` με `outreach_enabled()` check. Ενημερώθηκαν τα Superadmin templates (`client_finder/list.html`, `outreach_history/list.html` & `views.py`) για την προβολή των ακυρωμένων εγγραφών. Δημιουργήθηκε το εσωτερικό έγγραφο συμμόρφωσης `docs/compliance/cold-outreach-shutdown.md` που καλύπτει χάρτη 9 εισόδων, safeguards, checklists παραγωγής/provider, data retention και copy audit. Προστέθηκαν 4 νέα regression tests στο `gemiapp/tests.py`. `manage.py check`, `makemigrations --check` και όλο το suite (**646 tests, `OK`**) πέρασαν καθαρά.
- 2026-09-09: **Πλήρης παραγωγική υλοποίηση του εγκεκριμένου Signal Ledger UI direction.** Εφαρμόστηκε το νέο visual system σε όλες τις authenticated επιφάνειες: `templates/base.html` (header, rail navigation, mobile bottom nav), `templates/dashboard.html` & `includes/dashboard_rows.html` (Signal Register + Selected Signal Context Panel & mobile inline reveal), `templates/radars/list.html`, `detail.html`, `form.html` (Matching Definitions register), `templates/companies/detail.html` (Business Dossier με EVENT → INFO → CRITERIA → RELEVANCE → LEAD chain και mobile relevance prioritization), `templates/leads/list.html` (Qualified Outcomes register) και `templates/settings.html` (System Configuration). Δημιουργήθηκε το scoped `static/css/product-ui.css` με `Inter` & monospace typography, dark ink / neutral canvas παλέτα και amber relevance accents, χωρίς να επηρεάζει τις μη-authenticated σελίδες. Όλα τα υφιστάμενα routes, backend logic, filters, CSV exports, infinite scroll, CSRF forms, δικαιώματα συνδρομών και billing state machines διατηρήθηκαν ακέραια. Παράχθηκαν 7 νέα review captures στο `docs/gemi-leads-ui-study/screenshots/production-*.png` για desktop (1440x900) και mobile (390x844). `manage.py check` & `manage.py test`: **642 tests, `OK`**, 0 σφάλματα.
- 2026-09-09: **Focused refinement του εγκεκριμένου Signal Ledger direction.** Διατηρήθηκαν το left navigation, chronological register, selected-signal panel, Signals/Radars/Leads language, flat surfaces, timestamp scanning, EVENT → CRITERIA → LEAD chain, amber accent και off-white/dark system. Τα lifecycle states απέκτησαν ενιαία grammar με σταθερή θέση, rule length/weight και typography αντί για pills: signal, matched signal, new, viewed, contacted, interested και lead. Το selected panel αναδιατάχθηκε σε identity → public metadata → chain → match evidence → primary action. Η navigation χρησιμοποιεί Market/Criteria/Outcome/System descriptors και source → match → lead logic. Το top metadata έγινε GEMI intake / last source sync. Στο mobile Signals ο επιλεγμένος business ανοίγει inline αμέσως μετά το signal row, όχι ως stacked desktop panel· στο mobile Lead η relevance/lead action προηγείται του public record. Δημιουργήθηκαν 5 νέα `refined-*` review captures. Καμία production template/view/model/route/backend αλλαγή.
- 2026-09-09: **Άμεση και καθολική παύση cold outreach προς νέες εταιρείες ΓΕΜΗ.** Προστέθηκε fail-closed `OUTREACH_ENABLED` (default `0`) και εφαρμόστηκε σε όλα τα σημεία: `client_finder_send`, test outreach, `queue_company_outreach`, `process_pending_outreach`, `send_company_outreach_task` και `drain_pending_outreach_task`. Το Superadmin UI εμφανίζει εμφανή προειδοποίηση και disabled κουμπιά. Το cap μηδενίστηκε σε settings, `.env.example`, local `.env` και `render.yaml`. Οι pending εγγραφές μένουν ανέπαφες, χωρίς αποστολή, και τα digest/verification/reset emails παραμένουν ενεργά. Προστέθηκαν 4 regression tests για το emergency stop· 24 focused tests και όλο το suite (**642 tests, `OK`**) πέρασαν, μαζί με καθαρά `manage.py check` και `makemigrations --check --dry-run`. Καμία migration.
- 2026-09-09: **Focused Gemi Leads product UI art-direction study (prototype only).** Έγινε audit των πραγματικών authenticated επιφανειών (`base.html`, Dashboard/company archive, Lead Inbox, Radars list/detail/form, company detail), των σχετικών Django views/models/routes και του shared Tailwind/JS layer. Τεκμηριώθηκαν τα generic SaaS patterns (4-KPI strip, decorative chart, repeated rounded cards, pill overuse, gradient/glass navigation, fragmented dossier) και τα λειτουργικά patterns που πρέπει να διατηρηθούν. Δημιουργήθηκαν τρεις πραγματικά διακριτές κατευθύνσεις: **Signal Ledger** (προτεινόμενη), **Operational Index**, **Business Briefing**, με desktop/mobile mockups. Δημιουργήθηκε standalone interactive low-fidelity prototype για Dashboard/Signals, Radar view και Lead detail, με anonymized schema-faithful data και in-memory-only interactions για favorite, Radar pause/resume, lead status και notes. Προστέθηκαν anti-AI audit, reuse/replacement map, risks και screenshots στο `docs/gemi-leads-ui-study/`. Δεν άλλαξε κανένα production template, view, model, route, migration, backend behavior ή πραγματικό user data. `manage.py check`: καθαρό.
- 2026-09-06 *(αναιρέθηκε με `37e639c` στις 2026-09-11· ξανά σε ισχύ για το Free, μόνο daily και χωρίς Ραντάρ/CSV, από 2026-09-26 — βλ. «Free: ημερήσιο email digest»)*: **Αλλαγή πολιτικής Daily Digest**: Οι χρήστες χωρίς πληρωμένη συνδρομή πλέον λαμβάνουν κανονικά το καθημερινό digest email (αντί να κόβονται με NO_ENTITLEMENT). Το redirect στο `/pricing/` γίνεται κατά το κλικ στο κουμπί "Export CSV", το οποίο ίσχυε ήδη. Το test `test_each_blocking_condition_is_reported_distinctly` προσαρμόστηκε για να περιμένει `NO_ENTITLEMENT` στο `intraday` αντί για το `daily`.
- 2026-08-28: **Brevo engagement tracking (Phase 8): μετακίνηση ιστορικού outreach, διόρθωση bucket bug, νέα φίλτρα.** Ο χρήστης ανέφερε 4 πράγματα μετά την πρώτη έκδοση του Brevo webhook tracking (βλ. προηγούμενη καταχώρηση): (α) η εφαρμογή «κολλούσε» κυρίως στο `/superadmin/` — investigation με πραγματικά Render metrics (CPU peak ~20%, Memory 40-55% του 512MB όριο, καμία υπερφόρτωση) απέδειξε ότι επρόκειτο για τα ίδια τα 4-5 διαδοχικά redeploys της ίδιας συνεδρίας (ορατά ως αλλαγές container hash στο memory graph), όχι bug απόδοσης· επιβεβαιώθηκε ζωντανά ότι το sluggishness εξαφανίστηκε μόλις σταμάτησαν τα deploys. (β) Το «Ιστορικό Αποστολών» outreach μετακινήθηκε από το `/superadmin/client-finder/` σε ξεχωριστή σελίδα `/superadmin/outreach-history/` (νέο `outreach_history` view/template, νέο sidebar link) — το Εύρεση Πελατών έμεινε αμιγώς εργαλείο επιλογής/αποστολής σε νέους. (γ) Bug fix: το bucket mapping μπέρδευε το Brevo `unique_opened` (fires μία φορά ανά παραλήπτη) με το `opened` (fires σε κάθε άνοιγμα, ξανά-ανοίγματα) — αν συγχωνεύονταν στο ίδιο bucket θα διπλομετριόταν το ίδιο άνοιγμα. Λύση: μόνο το `opened` μετράει πλέον, το `unique_opened` πέφτει σκόπιμα σε «other» (μη ορατό) — ασφαλέστερο να μετράει λίγο λιγότερο παρά να διπλομετράει. Επιβεβαιώθηκε επίσης με πραγματικά production δεδομένα ότι οι ~441 από τις 452 outreach αποστολές έγιναν *πριν* ενεργοποιηθεί το webhook σήμερα και δεν θα δείξουν ΠΟΤΕ engagement — όχι bug, το Brevo απλά δεν είχε πού να στείλει τα events τη στιγμή εκείνη· προστέθηκε ρητή ενημερωτική μπάρα στη νέα σελίδα. (δ) Νέα φίλτρα: ΚΑΔ search (`activity_records__code__istartswith`, με `.distinct()` γιατί μία εταιρεία μπορεί να ταιριάζει σε πάνω από μία εγγραφή δραστηριότητας) προστέθηκε και στο Εύρεση Πελατών και στο Ιστορικό· sort by opens/clicks στο Ιστορικό — επειδή η αντιστοίχιση CompanyOutreach↔EmailEngagementEvent γίνεται μέσω string `tag`, όχι πραγματικού DB join, το sort-by-engagement φορτώνει όλο το φιλτραρισμένο σύνολο (cap 5000, πολύ πάνω από τον σημερινό όγκο) στην Python και ταξινομεί εκεί, ενώ το προεπιλεγμένο "πιο πρόσφατα" παραμένει φθηνό DB-level `ORDER BY` + per-page αίτημα engagement. Το `attach_email_engagement_stats` έγινε κοινό `_attach_engagement_stats(objects, tag_of)` ώστε digest και outreach να μοιράζονται το ίδιο query pattern. Νέα tests: `OutreachHistoryTests`-στυλ methods μέσα στο `SuperadminTests` (μετακίνηση σελίδας, sort by clicks, ΚΑΔ φίλτρο και στις δύο σελίδες). Πλήρες suite πράσινο, `manage.py check`/`makemigrations --check` καθαρά, καμία migration.
- 2026-08-28: **Brevo delivery/engagement tracking (πρώτη έκδοση) + διόρθωση 3 real production bugs στο billing webhook.** Νέο μοντέλο `EmailEngagementEvent` (durable, append-only log — καμία idempotency απαίτηση σε αντίθεση με το `StripeWebhookEvent`, ένα διπλό «opened» event δεν έχει συνέπεια real-money) και νέο `POST /api/brevo/webhook/<token>/` (`gemiapp/email_tracking.py`), authenticated με Brevo's δικό του token-based webhook auth (shared secret, `hmac.compare_digest`, όχι HMAC signature). Κάθε digest email πλέον φέρει custom header `X-Mailin-Tag` (`digest:<user_id>:<date>:<frequency>`, μέσω νέου `services.digest_email_tag`/`_send_digest_email`, `send_mail`→`EmailMultiAlternatives`) που το Brevo επιστρέφει σε κάθε webhook event, επιτρέποντας ακριβές matching στο σωστό `DigestDelivery` row χωρίς πραγματικό DB join. Νέο `attach_email_engagement_stats` στο superadmin services δείχνει delivered/opened/clicked/unsubscribed/bounced per-delivery στο `/superadmin/digests/`. Παράλληλα, deploy της ήδη uncommitted δουλειάς Phase 0-6 billing (checkout/upgrade/downgrade/cancel/resume/webhook idempotency) αποκάλυψε live, μέσω πραγματικού Stripe test-mode verification (όχι unit tests, που έκρυβαν το πρόβλημα επειδή mockάρουν με plain dicts): (1) το εγκατεστημένο Stripe SDK v15.5.0 δεν έχει `.get()` στα objects του — κάθε πραγματικό webhook θα έσκαγε 500· (2) `Decimal` non-JSON-serializable στο payload storage· (3) `SubscriptionSchedule` phase items επιστρέφουν `price` ως plain string id, όχι expanded object (σε αντίθεση με `Subscription` items) — κάθε downgrade attempt έσκαγε. Και τα τρία διορθώθηκαν με helpers `_as_plain_dict`/`_price_id_of`. Production billing ενεργοποιήθηκε (`LEGAL_BILLING_ACTIVE=1`, live Stripe keys/prices/webhook, νομικά στοιχεία TAXVILLE), επιβεβαιωμένο με πραγματικό live Stripe Checkout session μέχρι το σημείο πληρωμής (χωρίς πραγματική χρέωση).
- 2026-08-25: **Phase 5g του billing production-readiness audit: Final Billing UI / Subscription Lifecycle Polish.** Καθαρά UI χωρίς αλλαγή σε Stripe billing mechanics, upgrade/downgrade/cancel/resume semantics, webhook architecture, entitlement predicates, idempotency keys ή rank logic — μόνο templates, ένα νέο templatetags module, και tests. Ξεκίνησε με audit των `templates/pricing.html`, `templates/settings.html`, `templates/includes/checkout_button.html` και των Django messages στο `gemiapp/billing.py` (δεν υπήρχε `templates/billing/resume_checkout.html`, ούτε άλλο billing partial). Ευρήματα του audit: (α) το per-tier complimentary-gating στο pricing.html έκρυβε τα checkout buttons για κάθε complimentary-only χρήστη, παραβιάζοντας το requirement «complimentary access δεν πρέπει ποτέ να μπλοκάρει αγορά πραγματικού πλάνου»· (β) το settings.html βασιζόταν αποκλειστικά στο `has_active_paid_subscription` (True μόνο για `status="active"`), οπότε `trialing`/`past_due`/`unpaid`/`incomplete` χρήστες έβλεπαν «Δεν υπάρχει ενεργή συνδρομή» αντί για κατάλληλη κατάσταση διαχείρισης, παρότι το backend (`CANCELABLE_STRIPE_SUBSCRIPTION_STATUSES`) επιτρέπει ήδη cancel για `trialing`· (γ) δεν υπήρχε πουθενά human-readable status label — κανένα raw Stripe status δεν εμφανιζόταν, αλλά και καμία ένδειξη κατάστασης καθόλου· (δ) 3 σημεία στο pricing.html επαναλάμβαναν πανομοιότυπο, μη-πληροφοριακό placeholder «Υπάρχει ήδη προγραμματισμένη αλλαγή» για δύο εννοιολογικά διαφορετικά states (`active_until` vs `scheduled_tier`). Τα Django messages ελέγχθηκαν εξ ολοκλήρου (checkout/change-plan/cancel/resume/scheduled-downgrade/cancel-scheduled-downgrade) και ήταν ήδη σύντομα, σαφή, στα Ελληνικά, χωρίς raw exception text ή internal terms — καμία αλλαγή χρειάστηκε εκεί, μόνο tests που το pin-άρουν ρητά.

  **Νέο `gemiapp/templatetags/billing_tags.py`** (νέο package, `__init__.py` + module — Django auto-discovers templatetags μέσα σε installed apps, καμία επιπλέον registration χρειάστηκε): filter `stripe_status_label(status)` — dict mapping 8 γνωστών Stripe statuses σε Ελληνικά labels (`active`→«Ενεργή», `trialing`→«Δοκιμαστική περίοδος», `past_due`→«Εκκρεμεί πληρωμή», `unpaid`→«Ανεξόφλητη», `incomplete`→«Η πληρωμή δεν ολοκληρώθηκε», `incomplete_expired`→«Έληξε», `canceled`→«Ακυρωμένη», `paused`→«Σε παύση»), με safe generic fallback «Άγνωστη κατάσταση» για οτιδήποτε άλλο — ποτέ raw status string στον χρήστη. Filter `billing_state(sub)` — μία κανονική, mutually-exclusive lifecycle κατάσταση, priority `payment_problem` > `scheduled_cancellation` > `scheduled_downgrade` > `normal_active` > `terminal` > `complimentary_only` > `none`· ξαναχρησιμοποιεί τα ήδη υπάρχοντα `PROBLEMATIC_PAYMENT_STRIPE_SUBSCRIPTION_STATUSES`/`CANCELABLE_STRIPE_SUBSCRIPTION_STATUSES`/`TERMINAL_STRIPE_SUBSCRIPTION_STATUSES` από το `gemiapp/billing.py` (import μέσα στο templatetags module — καμία κυκλική εξάρτηση, το billing.py δεν εισάγει ποτέ templatetags) αντί να τα ξαναγράψει, άρα καμία απόκλιση από ό,τι ήδη επιβάλλουν τα mutation endpoints. Ρητά τεκμηριωμένο στο module docstring ότι τίποτα εδώ δεν επιτρέπεται να χρησιμοποιηθεί για entitlement/authorization — `has_active_paid_subscription`/`has_valid_complimentary_access`/`effective_tier`/`has_entitlement` παραμένουν ανέγγιχτα.

  **`templates/settings.html`** ξαναγράφτηκε γύρω από `{% with state=user.subscription|billing_state %}` με ένα `{% if/elif %}` block, ένα branch ανά state, ώστε ποτέ να μη ρενταριστούν δύο lifecycle actions μαζί: `payment_problem` → μόνο «Διαχείριση Χρέωσης» (Portal), ζεστό/όχι τρομακτικό amber card με «⚠️ Χρειάζεται ενέργεια για την πληρωμή σας» + status label, καμία plan-change ένδειξη· `scheduled_cancellation` → «Η συνδρομή σου θα λήξει στις Χ» + «Συνέχιση συνδρομής» + Portal (Resume, όχι Cancel)· `scheduled_downgrade` → «Τρέχον πλάνο: Χ» / «Προγραμματισμένη αλλαγή: Υ στις Ζ» + «Ακύρωση προγραμματισμένης υποβάθμισης» + Portal· `normal_active` (περιλαμβάνει πλέον και `trialing`, όχι μόνο `status="active"`) → status label + radar limit + Portal + «Ακύρωση στο τέλος περιόδου»· `terminal` (`canceled`/`incomplete_expired`) → «Χωρίς Ενεργή Συνδρομή» + «Επιλογή νέου πλάνου» → pricing, καμία mutation button για την παλιά συνδρομή· `complimentary_only` → το ήδη υπάρχον complimentary card + νέο CTA «Αγορά συνδρομής» → pricing, καμία billing-portal/cancel/resume/downgrade ένδειξη (αφού δεν υπάρχει καν Stripe subscription να διαχειριστεί)· `none` → «Δεν υπάρχει ενεργή συνδρομή» + «Δείτε τα πλάνα». Καμία Stripe κλήση μέσα στο render — το `billing_state` διαβάζει αποκλειστικά ήδη-φορτωμένα πεδία στο `user.subscription`. Regression-critical: τα προϋπάρχοντα Phase 5c/5f tests (`SettingsSubscriptionUITests`, `CancelScheduledDowngradeUITests`) συνεχίζουν να περνάνε αναλλοίωτα, γιατί το `normal_active`/`scheduled_cancellation`/`scheduled_downgrade` branching παράγει το ίδιο `cancel_subscription`/`resume_subscription`/`cancel_scheduled_downgrade`/`customer_portal` markup με πριν για τα ήδη-testαρισμένα states.

  **`templates/pricing.html`**: αφαιρέθηκε το complimentary-gating branch από τις 3 paid-tier κάρτες (Pro/Business/Enterprise) — πριν, κάθε complimentary-only χρήστης έβλεπε είτε «✨ Ενεργό (Δωρεάν Access)» είτε «Περιλαμβάνεται στο Admin Access» σε ΚΑΘΕ κάρτα, χωρίς κανένα checkout button, ό,τι Stripe subscription και να είχε (ή δεν είχε)· τώρα οι 3 κάρτες κρίνονται αποκλειστικά από `has_active_paid_subscription`/`tier`, όπως θα έκριναν για οποιονδήποτε άλλο χρήστη, οπότε ένας complimentary-only χρήστης (καμία Stripe subscription) πέφτει κανονικά στο `{% include "includes/checkout_button.html" %}` branch — το πάνω-πάνω purple banner (γραμμές 83-110, αναλλοίωτο) συνεχίζει να δείχνει καθαρά το complimentary access. Νέο `templates/includes/pending_change_notice.html`: ένα include που αντικαθιστά 4 πανομοιότυπα, μη-πληροφοριακά «Υπάρχει ήδη προγραμματισμένη αλλαγή» placeholders (Pro-card downgrade-target, Business-card upgrade-from-pro, Business-card downgrade-target, Enterprise-card upgrade-target) — τώρα διαφοροποιεί ρητά `active_until` («Η συνδρομή θα λήξει στις Χ · Διαχείριση από τις ρυθμίσεις») από `scheduled_tier` («Προγραμματισμένη αλλαγή → Υ στις Ζ · Διαχείριση από τις ρυθμίσεις»), και τα δύο ως link προς `/settings/` (canonical management surface, καμία δεύτερη mutation button στο pricing). Το ίδιο include εφαρμόστηκε και στα 3 «current tier» branches (πλέον ελέγχουν `active_until` πριν δείξουν το Portal-button «Διαχείριση», ώστε μια scheduled cancellation στο ίδιο το τρέχον tier να δείχνει επίσης το σωστό μήνυμα αντί για γενικό Portal button). Regression-critical: `ChangePlanPricingUITests`/`CancelScheduledDowngradeUITests` tests 30-35 συνεχίζουν να περνάνε — ελέγχουν απουσία/παρουσία του `reverse("change_plan")`, όχι το ακριβές παλιό copy.

  **Tests**: `PricingLifecycleUITests` (12, items 3-8+23: no-subscription checkout, complimentary-only checkout — το ρητό regression test για το bug που διορθώθηκε, current-tier χωρίς self-mutation, Pro→Business/Enterprise upgrade, Business→Pro downgrade, Business→Enterprise upgrade, Enterprise→lower-tier downgrades χωρίς κανένα «Αναβάθμιση», scheduled downgrade χωρίς κανένα mutation, scheduled cancellation χωρίς κανένα mutation, terminal επιτρέπει νέο checkout, payment-problem δεν ρίχνει exception στο pricing render), `SettingsLifecycleUITests` (9, items 9-17+24: normal-active Portal+Cancel, trialing με δικό του label, cancellation-pending Resume+Portal, downgrade-pending CancelDowngrade+Portal, payment-problem Portal-only, canceled→pricing CTA, complimentary-only→info+pricing CTA χωρίς billing controls, no-subscription→pricing CTA, και ένα συνολικό «ποτέ δύο lifecycle mutations μαζί» test πάνω σε 4 states), `BillingStatusLabelTests` (3, items 18+25: όλα τα γνωστά statuses παίρνουν το σωστό label, άγνωστο status παίρνει safe fallback χωρίς να διαρρεύσει το ίδιο το raw string, κανένα raw status δεν εμφανίζεται στο settings render), `BillingMessageContentTests` (2, item 19+25: cancel-failure message χωρίς raw Stripe exception text, change-plan success message σωστό και χωρίς internal terms) — 26 νέα tests. 2 προϋπάρχοντα tests (Phase 5e `DowngradeUITests.test_scheduled_downgrade_shows_target_and_date_in_settings`, Phase 5f `CancelScheduledDowngradeUITests.test_pending_downgrade_still_shows_target_and_date`) ενημερώθηκαν να ελέγχουν το νέο, ισοδύναμο copy «Προγραμματισμένη αλλαγή» αντί για τη λέξη «μετά» που υπήρχε στο παλιό, αντικατεστημένο settings.html copy — η συμπεριφορά που testάρουν (εμφανίζεται το scheduled target tier + ημερομηνία) παραμένει ίδια, άλλαξε μόνο η ίδια η διατύπωση, σκόπιμα ως μέρος αυτού ακριβώς του UI polish. Καμία migration. Full suite: **525 tests, καθαρό `OK`, μηδέν expected failures.** `manage.py check`: καθαρό. `makemigrations --check --dry-run`: `No changes detected`.

  **Backend inconsistency που αποκάλυψε το UI audit (τεκμηριωμένο, ΔΕΝ διορθώθηκε — ρητά εκτός scope, θα άλλαζε entitlement semantics):** `UserSubscription.ALLOWED_PAID_STATUSES = ("active",)` σημαίνει `has_active_paid_subscription` False για `trialing`/`past_due`/`unpaid`/`incomplete`. Το settings.html το διορθώνει πλέον σωστά μέσω του νέου `billing_state` (που διαβάζει `status`/`stripe_subscription_id` απευθείας, όχι μόνο `has_active_paid_subscription`), αλλά το **pricing.html** εξακολουθεί να βασίζεται αποκλειστικά στο `has_active_paid_subscription` για τα per-tier CTAs — ένας `trialing`/`past_due` χρήστης βλέπει ακόμη κανονικά checkout buttons εκεί αντί για ένδειξη διαχείρισης. Αυτό ΔΕΝ είναι real-money risk: το ήδη υπάρχον `_blocks_new_checkout` (Phase 4) κάνει fresh Stripe read και μπλοκάρει σωστά με μήνυμα «Έχεις ήδη ενεργή συνδρομή» αν προσπαθήσουν, οπότε είναι καθαρά UX rough edge, όχι entitlement/billing bug — ρητά αφέθηκε εκτός Phase 5g scope (θα απαιτούσε το ίδιο state-machine restructuring στο pricing.html με το settings.html, μεγαλύτερη αλλαγή από το «minimal necessary» που ζητήθηκε).

  **Residual UX risks:** (1) το pricing/trialing-payment-problem gap παραπάνω· (2) καμία πραγματική οπτική επιβεβαίωση σε πραγματικό browser/mobile viewport έγινε αυτή τη φάση (μόνο Django test client HTML assertions) — η spacing/wrapping πολιτική (item 22) ακολουθεί το ήδη υπάρχον Tailwind-based responsive σύστημα του pricing/settings χωρίς αλλαγή στο grid/breakpoints, αλλά δεν επαληθεύτηκε οπτικά σε πραγματική συσκευή· (3) καμία αλλαγή στο accessibility attributes πέρα από ό,τι ήδη υπήρχε (POST forms + CSRF + real `<button>` elements, καμία clickable div — ήδη σωστό πριν το Phase 5g, επιβεβαιώθηκε στο audit, όχι νέο).


- 2026-08-25: **Phase 5f του billing production-readiness audit: ακύρωση/release μιας ήδη προγραμματισμένης downgrade (κλείνει τον upgrade/downgrade lifecycle κύκλο που ξεκίνησε στο Phase 5).** Νέο endpoint `POST /api/stripe/cancel-scheduled-downgrade/` (`gemiapp/billing.py:cancel_scheduled_downgrade`, `@login_required @require_POST`), route `api/stripe/cancel-scheduled-downgrade/`. Preconditions πριν από οποιοδήποτε Stripe write, όλα fail-closed: `sub.scheduled_tier`/`sub.stripe_schedule_id` πρέπει να υπάρχουν τοπικά (αλλιώς τίποτα να ακυρωθεί, info message)· `sub.active_until` δεν επιτρέπεται να συνυπάρχει (η ίδια αμοιβαία αποκλειστικότητα scheduled_tier/active_until που το Phase 5e ήδη επιβάλλει — αν και τα δύο εμφανιστούν ποτέ μαζί, θεωρείται ασαφές τοπικό state και μπλοκάρει αντί να μαντέψει ποιο είναι σωστό). **Φρέσκο, unlocked `SubscriptionSchedule.retrieve(sub.stripe_schedule_id)`** πριν από οποιοδήποτε write (ίδιο tradeoff-πάντα-fresh με Phase 5b/5d): επιβεβαιώνεται ότι το schedule's `subscription` id ταιριάζει με το `sub.stripe_subscription_id` (schedule ανήκει σε άλλο subscription → block+log, ποτέ implicit correction)· ότι έχει ακριβώς τις αναμενόμενες δύο phases με το future phase's price να ταιριάζει με το τοπικό `scheduled_tier` (mismatch → block+log, ίδια φιλοσοφία με το Phase 5e modify-side check)· και το `status`. **Boundary-race handling (η κύρια νέα πολυπλοκότητα της φάσης):** αν ο χρήστης πατήσει «Ακύρωση» ακριβώς καθώς περνάει το period boundary, το schedule μπορεί να έχει ήδη μεταβεί σε `status="completed"` πριν προλάβει το retrieve — αυτό αντιμετωπίζεται με ρητό, ξεχωριστό μήνυμα «η αλλαγή έχει ήδη εφαρμοστεί» (όχι γενικό error, όχι ψευδής επιτυχία) και **καμία** automatic αντίστροφη αλλαγή (δεν ξαναγυρίζει σε παλιό tier) — ο χρήστης καθοδηγείται να κάνει νέο upgrade αν το θέλει, ρητά εκτός scope το να το κάνει το ίδιο το endpoint σιωπηλά. `status` ∈ `{canceled, released}` (ήδη ακυρωμένο/ελευθερωμένο αλλού, π.χ. δεύτερο tab ή ήδη επεξεργασμένο webhook) → idempotent «ήδη ακυρώθηκε» info message, καμία δεύτερη Stripe κλήση. `status` οτιδήποτε άλλο/άγνωστο → fail closed. Η κλήση καθαυτή: `stripe.SubscriptionSchedule.release(sub.stripe_schedule_id, idempotency_key=idempotency_key)` — ρητά **`.release()` και ΟΧΙ `.cancel()`**: το `.release()` αποσυνδέει το schedule και αφήνει το υποκείμενο subscription να συνεχίσει κανονικά στο τρέχον (πριν τη downgrade) price/phase· το `.cancel()` θα τερμάτιζε ολόκληρη τη συνδρομή, εντελώς λάθος ενέργεια εδώ. Idempotency: **τέταρτο, εντελώς ξεχωριστό namespace** από checkout/cancel-resume/change-plan/downgrade-create: `_cancel_downgrade_attempt_nonce`/`_conclude_cancel_downgrade_attempt`/`_cancel_downgrade_idempotency_key`, session key prefix `_CANCEL_DOWNGRADE_ATTEMPT_SESSION_KEY`, derived key prefix `cdr_` — pinned με test ότι δεν συγκρούεται με το `dg_` του Phase 5e. Nonce καθαρίζεται μόνο μετά από επιβεβαιωμένη επιτυχία, ίδιο σχήμα με όλες τις προηγούμενες φάσεις. **Local synchronous behavior: καμία γραμμή δεν γράφει `scheduled_tier`/`scheduled_change_at`/`stripe_schedule_id`/tier/status/entitlement** σε success ΟΥΤΕ σε failure — pinned με tests που συγκρίνουν state πριν/μετά. Νέο webhook handler `_handle_subscription_schedule_released` χειρίζεται **και** `subscription_schedule.released` **και** `subscription_schedule.canceled` με την ίδια λογική (το Stripe στέλνει `.released` για κανονικό `.release()` αλλά ένα schedule μπορεί επίσης να φτάσει σε `canceled` status από άλλο μονοπάτι π.χ. Dashboard — και τα δύο σημαίνουν το ίδιο για το τοπικό bookkeeping: «αυτό το schedule δεν είναι πια ενεργό, καθάρισε τα scheduled πεδία», τεκμηριωμένο ρητά στον κώδικα): καθαρίζει **αποκλειστικά** τα τρία `scheduled_*` πεδία σε ένα atomic write, ποτέ tier/status/active_until/entitlement — ταιριάζει με τη γενική αρχή «webhooks μόνο, synchronous endpoints ποτέ δεν γράφουν business state» που ισχύει σε όλο το Phase 5. **Out-of-order guard** (ίδια κλάση residual risk με Phase 5c/5e, εδώ ελαφρώς μετριασμένη χωρίς νέο schema): πριν καθαρίσει, συγκρίνει το event's schedule id με το τοπικά αποθηκευμένο `sub.stripe_schedule_id` — αν διαφέρουν (ένα νεότερο schedule έχει ήδη αντικαταστήσει το παλιό, π.χ. νέα downgrade μπήκε στο μεταξύ), αγνοεί το stale event (`processed`, όχι error) αντί να σβήσει state που ανήκει σε νεότερο schedule. UI (`templates/settings.html`): το προηγούμενο δυαδικό Cancel/Resume έγινε **τριμερές, αμοιβαία αποκλειστικό** block — `active_until` → «Συνέχιση συνδρομής» (Resume, Phase 5c)· αλλιώς `scheduled_tier` → νέο «Ακύρωση προγραμματισμένης υποβάθμισης» (POST στο `cancel_scheduled_downgrade`)· αλλιώς → «Ακύρωση στο τέλος περιόδου» (Cancel, Phase 5b) — ποτέ πάνω από ένα ταυτόχρονα, με ρητό template comment που εξηγεί το invariant. Tests: `CancelScheduledDowngradeTests` (18, καλύπτει κάθε precondition/status/boundary-race/idempotency σενάριο — βρέθηκε και διορθώθηκε bug στα ίδια τα test fixtures όπου αρκετά tests καλούσαν `_schedule_payload()` με λάθος default `subscription_id="sub_1"` αντί για το πραγματικό subscription id του test user, κρύβοντας το σκόπιμο precondition κάθε test πίσω από ένα άσχετο «schedule ανήκει σε άλλο subscription» block· διορθώθηκε περνώντας ρητό `subscription_id=` σε κάθε κλήση), `SubscriptionScheduleReleasedWebhookTests` (7, καλύπτει `.released`/`.canceled`/out-of-order/unknown-subscription), `CancelScheduledDowngradeUITests` (7, καλύπτει το τριμερές mutually-exclusive button state). Καμία migration (τα Phase 5a πεδία ήδη επαρκούσαν). Billing/webhook/checkout/entitlement regression subset (28 κλάσεις, Phase 0-5f): 233 tests, `OK`. Full suite: **500 tests, καθαρό `OK`, μηδέν expected failures.** `manage.py check`: καθαρό. `makemigrations --check --dry-run`: `No changes detected`. **⚠️ Production-ready μόνο μετά από πραγματικό Stripe TEST MODE verification (δεν έγινε σε αυτή τη φάση, καμία πραγματικά credentials χρησιμοποιήθηκαν, όλα mocked)**: χρειάζεται χειροκίνητο checklist πριν ενεργοποιηθεί σε πραγματικούς χρήστες — δημιουργία πραγματικού scheduled downgrade → cancel του → επιβεβαίωση ότι η συνδρομή συνεχίζει στο ΑΡΧΙΚΟ (πριν τη downgrade) price χωρίς καμία χρέωση/credit· δοκιμή του boundary-race πατώντας cancel πολύ κοντά στο πραγματικό period end· επιβεβαίωση ότι φτάνει πραγματικό `subscription_schedule.released` webhook και καθαρίζει σωστά τα τρία πεδία· δοκιμή διπλού POST (idempotency) με πραγματικό Stripe. Κανένα νέο real-money billing risk εντοπίστηκε — το `.release()` δεν προκαλεί καμία χρέωση/refund, μόνο επαναφέρει το subscription στο ήδη-ενεργό του recurring price.
- 2026-08-25: **Phase 5e του billing production-readiness audit: scheduled downgrade στο τέλος περιόδου μέσω Stripe Subscription Schedule.** Επεκτάθηκε το `POST /api/stripe/change-plan/` (`gemiapp/billing.py:change_plan`) — μετά την ύπαρξη preconditions κοινών με το Phase 5d (status, cancel_at_period_end, item/tier extraction, local/Stripe mismatch), αν `_is_strict_upgrade()` είναι False και δεν είναι same-tier, δρομολογείται σε νέο `_perform_scheduled_downgrade()`. **Recovery mechanism για το κρίσιμο create-succeeds/modify-fails σενάριο:** πριν από κάθε `SubscriptionSchedule.create()`, ελέγχεται το `schedule` πεδίο του φρέσκου `Subscription.retrieve()` — αν υπάρχει ήδη (προηγούμενο create πέτυχε παρότι ο client είδε timeout), γίνεται `SubscriptionSchedule.retrieve()` πάνω σε ΑΥΤΟ αντί για νέο create (το Stripe θα το απέρριπτε άλλωστε). Καμία νέα local persistence χρειάστηκε — το Stripe subscription's δικό του `schedule` field είναι το recovery bookkeeping. Πριν το modify, επιβεβαιώνεται `len(phases)==1` και ότι η current phase's price ταιριάζει με το πραγματικό τρέχον Stripe price (fail closed σε mismatch). Schedule modify στέλνει ΚΑΙ τις δύο phases μαζί (current + future) χτισμένες από το πραγματικό retrieved schedule object, με explicit narrow allowlist (`collection_method`, `metadata`, `discounts` — μόνο αν ήδη υπάρχουν στο schedule, τίποτα άλλο δεν εικάζεται): current phase `end_date` = πραγματικό `current_period_end` (όχι top-level Subscription πια, ίδιο SDK-εύρημα με Phase 5c, διαβάζεται από το μοναδικό item), `proration_behavior` διατηρημένο από το υπάρχον· future phase `start_date`=ίδιο boundary, target price, `quantity` διατηρημένη, `proration_behavior="none"` (καμία immediate χρέωση/credit/refund), χωρίς `end_date` (open-ended, συνεχίζει recurring). `end_behavior="release"` ρητά (Stripe default, τεκμηριωμένο) ώστε μετά τη μετάβαση η συνδρομή να συνεχίσει κανονικά αντί να ακυρωθεί. Idempotency: τρίτο ξεχωριστό namespace (`downgrade_attempt_nonce:`, prefix `dg_`) με **δύο διαφορετικά derived keys** από το ίδιο attempt nonce (`schedule_create` / `schedule_update` operation strings) — τα δύο Stripe writes δεν μοιράζονται ποτέ key. Νέο webhook `subscription_schedule.updated` (`_handle_subscription_schedule_updated`) κάνει την projection: fail-closed (event failed, 5xx, retryable) αν δεν είναι ακριβώς 2 phases, άγνωστο future price, ή invalid `start_date` — ποτέ fabricated `scheduled_tier`. Άγνωστο τοπικό subscription στο schedule webhook αντιμετωπίζεται ως graceful no-op (`processed`, όχι `failed`) — ίδια σύμβαση με το ήδη υπάρχον `customer.subscription.updated` DoesNotExist handling, τεκμηριωμένη ρητά στον κώδικα/tests. **Cleanup:** το ήδη υπάρχον `_handle_subscription_updated_or_deleted` επεκτάθηκε με μία γραμμή λογικής — αν το νέο tier από ένα `customer.subscription.updated` ταιριάζει με το `sub.scheduled_tier`, τα τρία scheduled πεδία καθαρίζονται στο ΙΔΙΟ atomic write (χωρίς να χρειαστεί το ασαφές `subscription_schedule.completed`, που ενδέχεται να μην πυροδοτηθεί καν για μια open-ended τελική phase). UI: `pricing.html` κάθε κάρτα δείχνει «Υποβάθμιση στο τέλος περιόδου» (POST στο ίδιο `change_plan`) όταν πρόκειται για πραγματικό strict downgrade target ΚΑΙ δεν υπάρχει ήδη `active_until`/`scheduled_tier` (ίδιο gating με τα upgrade buttons)· `settings.html` δείχνει «{tier} έως {ημερομηνία} → μετά {scheduled_tier}» όταν υπάρχει pending downgrade. Δύο tests από προηγούμενες φάσεις χρειάστηκαν διόρθωση κατά το regression run: ένα Phase 5d UI test είχε ξεπερασμένη παραδοχή («enterprise δεν βλέπει καθόλου change_plan» — τώρα σωστά βλέπει downgrade actions, ενημερώθηκε το assertion σε «καμία Αναβάθμιση»)· ένα Phase 5c test είχε πραγματικό timezone bug (σύγκριση raw UTC datetime έναντι του already-localized template output, flaky κοντά σε τοπικό μεσονύχτιο — διορθώθηκε με `localtime()` στο assertion). Καμία migration (τα Phase 5a πεδία ήδη επαρκούσαν). Full suite: 468 tests, καθαρό `OK`, **μηδέν expected failures**.
- 2026-08-25: **Phase 5d του billing production-readiness audit: immediate strict-upgrade με proration.** Νέο endpoint `POST /api/stripe/change-plan/` (`gemiapp/billing.py:change_plan`, `@login_required @require_POST`), δέχεται **μόνο** `target_tier` από το request — subscription id, price id και τρέχον tier προκύπτουν αποκλειστικά από φρέσκο `stripe.Subscription.retrieve()` (πάντα, χωρίς DB lock, ίδιο tradeoff με Phase 5b). Νέο `TIER_RANK = {"pro":1,"business":2,"enterprise":3}` για canonical σύγκριση (όχι alphabetical) — επιτρέπει αυστηρά μόνο strict upgrade (target_rank > current_rank), same-tier και οποιοδήποτε downgrade απορρίπτονται με info message, καμία Stripe κλήση. Preconditions πριν από mutation, όλα fail-closed: `stripe_subscription_id` υπάρχει· `scheduled_tier`/`stripe_schedule_id` (Phase 5a) απαγορεύουν αναβάθμιση χωρίς καν retrieve (schedule-managed subscription, εκτός scope εδώ, όχι implicit `.release()`)· status ∈ `{active, trialing}` (ίδιο conservative σύνολο με το Phase 5b cancel, `past_due`/`unpaid`/`incomplete` → redirect στο Billing Portal, όχι mutation)· `cancel_at_period_end=True` → block με μήνυμα «κάνε πρώτα Resume» (ρητά ΟΧΙ implicit resume+upgrade)· νέο `_current_subscription_item()` απαιτεί **ακριβώς ένα** usable item με `id` και αναγνωρίσιμο `price.id` (malformed/πολλαπλά items → block)· mismatch ανάμεσα στο τοπικό `sub.tier` και το tier που προκύπτει από το φρέσκο Stripe price → block+log (ίδια αρχή με το Phase 4 ambiguous-state handling, ποτέ "διόρθωση" από το request handler). Stripe mutation ακριβώς όπως στο εγκεκριμένο design: `Subscription.modify(sub_id, items=[{"id": item_id, "price": target_price_id}], proration_behavior="always_invoice", payment_behavior="error_if_incomplete", idempotency_key=...)`. Idempotency σε τρίτο, εντελώς ξεχωριστό namespace από checkout (Phase 3) και cancel/resume (Phase 5b): session prefix `plan_change_attempt_nonce:`, key prefix `pc_`, scope user+subscription+target_tier+nonce· nonce καθαρίζεται μόνο μετά από επιβεβαιωμένη επιτυχία. Καμία γραμμή δεν γράφει τοπικά tier/status/active_until/scheduled_*/entitlement σε success ΟΥΤΕ σε failure — το `customer.subscription.updated` webhook (αναλλοίωτο, ΔΕΝ χρειάστηκε καμία αλλαγή· η ήδη υπάρχουσα λογική από Phases 1/2/5c εφαρμόζεται γενικά σε ΚΑΘΕ price change, upgrade ή όχι) είναι η μοναδική πηγή της τοπικής προβολής — pinned με end-to-end test (sync call → tier ίδιο· webhook → tier/entitlement/radar_limit αλλάζουν). UI στο `pricing.html`: κάθε κάρτα δείχνει «Αναβάθμιση» (POST στο `change_plan`) μόνο όταν είναι πραγματικά strict upgrade ΚΑΙ δεν υπάρχει `active_until`/`scheduled_tier` (αλλιώς fallback στο ήδη υπάρχον «Διαχείριση» → Portal)· η κάρτα Pro δείχνει πάντα «Υποβάθμιση — σύντομα» σε ήδη-συνδρομητή άλλου tier (Pro είναι το χαμηλότερο, ποτέ upgrade target)· το settings.html δεν άλλαξε. Ρητά **δεν** υλοποιήθηκε: downgrade, Subscription Schedule, αλλαγή cancellation semantics, νέο schema field (κανένα item id/pending-state δεν αποθηκεύεται — retrieve φρέσκο κάθε φορά). Full suite: 428 tests, καθαρό `OK`, **μηδέν expected failures**, καμία migration. **Production-ready μόνο μετά από πραγματικό Stripe TEST MODE verification (δεν έγινε σε αυτή τη φάση — δεν υπήρχαν test credentials διαθέσιμα)**: χρειάζεται χειροκίνητο checklist πριν ενεργοποιηθεί σε πραγματικούς χρήστες — successful Pro→Business upgrade, failed-payment upgrade, επιβεβαίωση καμίας δεύτερης subscription, immediate prorated invoice, ότι failed payment αφήνει το αρχικό πλάνο ανέπαφο, ότι το webhook ενημερώνει το τοπικό tier μόνο μετά την πραγματική αλλαγή στο Stripe.
- 2026-08-24: **Phase 5c του billing production-readiness audit: `active_until` webhook projection + Cancel/Resume UI state, καμία αλλαγή entitlement architecture.** Νέα helpers στο `gemiapp/billing.py`: `_stripe_timestamp_to_datetime` (Unix seconds → timezone-aware UTC datetime, `None` σε οτιδήποτε μη έγκυρο, ποτέ naive), `_subscription_period_end` (διαβάζει `items.data[0].current_period_end` — **όχι** top-level, ίδιο SDK-version εύρημα με το Phase 5 design doc), `_subscription_active_until` (η authoritative λογική: `None` αν `cancel_at_period_end` δεν είναι True· αλλιώς `cancel_at` αν υπάρχει ρητά, αλλιώς fallback στο item's `current_period_end`· αν είναι True αλλά ΚΑΝΕΝΑ από τα δύο δεν εξάγεται, κάνει **`raise RuntimeError`** αντί να επιστρέψει `None` — ώστε το webhook να μη λέει ψέματα στο UI ότι δεν υπάρχει scheduled cancellation ενώ το Stripe λέει το αντίθετο). Το `_handle_subscription_updated_or_deleted` υπολογίζει tier/status/active_until πριν ανοίξει το `transaction.atomic()` block και τα γράφει και τα τρία μαζί, μία φορά· σε `customer.subscription.deleted` το `active_until` πάει ρητά σε `None` (η αφαίρεση entitlement γίνεται αποκλειστικά μέσω `status="canceled"`, το πεδίο δεν είναι ποτέ historical marker). Καμία νέα Stripe API call χρειάστηκε — όλα τα δεδομένα ήδη υπάρχουν στο verified webhook payload. Ένα ασαφές `cancel_at_period_end=True` χωρίς εξαγόμενη ημερομηνία αξιοποιεί το ήδη υπάρχον Phase 1/2 μηχανισμό (event→failed, 5xx, Stripe retry) χωρίς καμία νέα πλάκα κώδικα. Entitlement (`has_entitlement`/`effective_tier`/`radar_limit`/DB predicates) παραμένει 100% ανεπηρέαστο — pinned με tests. Τα Phase 5a `scheduled_tier`/`scheduled_change_at`/`stripe_schedule_id` παραμένουν πλήρως απείραχτα (ρητό isolation test: scheduled downgrade metadata σε Business subscription αφήνει `active_until=None`). Το `/settings/` δείχνει πλέον conditionally: χωρίς scheduled cancellation → "Ακύρωση στο τέλος περιόδου"· με scheduled cancellation (`active_until` set) → "Η συνδρομή σου θα λήξει στις Χ" + "Συνέχιση συνδρομής" — ποτέ και τα δύο. Portal button αναλλοίωτο. Residual risk, ρητά τεκμηριωμένο και ΟΧΙ διορθωμένο σε αυτή τη φάση: ένα stale (out-of-order) `customer.subscription.updated` event που ξαναφτάνει μετά από πιο πρόσφατο θα μπορούσε να ξαναγράψει παλιό `active_until`/`status` — δεν προστέθηκε προστασία γιατί θα απαιτούσε νέο πεδίο (π.χ. αποθηκευμένο event timestamp στο `UserSubscription`) και αυτή η φάση έπρεπε να είναι schema-free. Καμία migration. Full suite: 396 tests, καθαρό `OK`, **μηδέν expected failures**.
- 2026-08-24: **Phase 5b του billing production-readiness audit: cancel-at-period-end / resume, χωρίς καμία synchronous τοπική αλλαγή entitlement.** Νέα views `cancel_subscription`/`resume_subscription` στο `gemiapp/billing.py` (`@login_required @require_POST`, routes `api/stripe/cancel-subscription/` και `api/stripe/resume-subscription/` — ακολούθησαν το ήδη υπάρχον `api/stripe/<action>/` naming convention αντί για το literal `/billing/...` του αιτήματος, ώστε να μην συγκρουστεί το όνομα με το ήδη υπάρχον `resume_checkout` — ρητά διαφορετικό concept, GET-safe landing μετά από login). Και τα δύο κάνουν **πάντα** φρέσκο, unlocked `stripe.Subscription.retrieve()` πριν από οποιοδήποτε write (σε αντίθεση με το Phase 4 guard, που έκανε live read μόνο σε ασαφή local status — εδώ, αφού πρόκειται για write σε συγκεκριμένο ήδη-υπάρχον subscription, το tradeoff έγερνε προς πάντα-fresh). Cancel επιτρέπεται μόνο για `active`/`trialing` (νέο `CANCELABLE_STRIPE_SUBSCRIPTION_STATUSES`, strict subset του Phase 4 `LIVE_STRIPE_SUBSCRIPTION_STATUSES`)· για `past_due`/`unpaid`/`incomplete` (νέο `PROBLEMATIC_PAYMENT_STRIPE_SUBSCRIPTION_STATUSES`) γίνεται redirect στο settings με μήνυμα προς το Billing Portal, καμία mutation· terminal ή άγνωστο status (π.χ. `paused`, μη αναγνωρισμένο) → fail closed. Resume επιτρέπεται όταν το φρέσκο status είναι «live» (Phase 4 taxonomy) ΚΑΙ `cancel_at_period_end=True`· αν ήδη `False`, idempotent no-op με info message, όχι error· terminal → blocked. Idempotency: νέο `_subscription_action_attempt_nonce`/`_conclude_subscription_action_attempt`/`_subscription_action_idempotency_key`, ίδιο pattern με το Phase 3 checkout nonce αλλά **ξεχωριστό session key namespace** (`subscription_action_attempt_nonce:<cancel|resume>` vs `checkout_attempt_nonce:<tier>`) και ξεχωριστό key prefix (`sa_` vs `ck_`) — pinned με ρητό test ότι δεν μοιράζονται namespace. Nonce καθαρίζεται **μόνο** μετά από επιβεβαιωμένη επιτυχία (ίδια λογική με Phase 3: αποτυχία = ασάφεια = ίδιο key στο retry). Καμία γραμμή κώδικα δεν γράφει `tier`/`status`/`active_until`/`scheduled_*`/entitlement — επιβεβαιωμένο με tests που συγκρίνουν state πριν/μετά το endpoint call. Νέο κουμπί «Ακύρωση στο τέλος περιόδου» στο `/settings/` (μόνο όταν `has_active_paid_subscription`, plain POST form όχι link). Resume button **δεν** προστέθηκε ακόμη σκόπιμα (Επιλογή A του brief): καμία τοπική προβολή του `cancel_at_period_end` υπάρχει πριν τη Phase 5c, και ζωντανό Stripe call σε κάθε page render θα ήταν αργό/εύθραυστο — το endpoint υπάρχει πλήρως testable, απλά χωρίς UI hook. Regression: pinned ρητά ότι το ήδη υπάρχον webhook handler (αναλλοίωτο σε αυτή τη φάση) δεν αφαιρεί entitlement όταν φτάνει `customer.subscription.updated` με `status="active"` — ακριβώς έτσι μοιάζει ένα πραγματικό cancel-at-period-end delivery μέχρι να λήξει πραγματικά η περίοδος. Customer Portal παρέμεινε άθικτο· σημειώθηκε ως operational risk ότι αν το Portal configuration επιτρέπει ήδη cancellation, θα υπάρχουν προσωρινά δύο cancellation entry points μέχρι απόφαση αν θα απενεργοποιηθεί το ένα. Καμία migration. Full suite: 375 tests, καθαρό `OK`, **μηδέν expected failures**.
- 2026-08-24: **Phase 5a του billing production-readiness audit: schema foundation για το upgrade/downgrade lifecycle (Phase 5b+), καμία λειτουργική αλλαγή.** Το `UserSubscription` (`gemiapp/models.py`) απέκτησε τρία νέα πεδία (migration `0021_subscription_schedule_fields`, μόνο `AddField`, καμία data migration): `scheduled_tier` (CharField, `choices` περιορισμένο σε `pro`/`business`/`enterprise` — όχι `free`/`custom`, `null=True, blank=True, default=None`· το `null=True` είναι σκόπιμη απόκλιση από τα αδελφά Stripe id πεδία, ακριβώς επειδή το `None` έπρεπε να είναι ξεχωριστό sentinel από οποιαδήποτε πραγματική τιμή tier), `scheduled_change_at` (DateTimeField, nullable), `stripe_schedule_id` (CharField, `blank=True`, ΟΧΙ `null=True` — συνέπεια με τα ήδη υπάρχοντα `stripe_customer_id`/`stripe_subscription_id`, ίδιο "" ως «κανένα ακόμη» sentinel). Νέο `UserSubscription.clean()` invariant (Python-level, ΟΧΙ DB constraint, για να μην περιπλέξει portability): αν `scheduled_tier is None`, το `scheduled_change_at`/`stripe_schedule_id` πρέπει να είναι επίσης κενά — ελέγχεται μόνο μέσω ρητού `full_clean()`, όπως ήδη γίνεται (άτυπα, μέσω `choices=`) για το `tier`/`complimentary_tier`· κανένα `save()` οπουδήποτε στο codebase δεν καλεί αυτόματα `full_clean()`, άρα καμία υπάρχουσα ροή δεν επηρεάζεται. Το `active_until` (dead field, καμία αλλαγή schema) απέκτησε ρητή, τεκμηριωμένη — αλλά ακόμη unwired — σημασιολογία στο ίδιο το πεδίο ως docstring: σημαίνει αποκλειστικά «πότε λήγει το entitlement επειδή υπάρχει ήδη προγραμματισμένος τερματισμός (`cancel_at_period_end=True`)», ποτέ renewal boundary, και ΔΕΝ χρησιμοποιείται για scheduled downgrade (αυτό εκφράζεται αποκλειστικά από τα δύο νέα `scheduled_*` πεδία). Επιβεβαιώθηκε ρητά με tests (`ScheduledTierProjectionTests`, 8 tests) ότι κανένα από τα νέα πεδία δεν επηρεάζει `effective_tier`/`has_entitlement`/`has_active_paid_subscription`/`radar_limit` — συγκρίθηκε μια subscription με και χωρίς scheduled metadata, πανομοιότυπο αποτέλεσμα. Complimentary entitlement επιβεβαιωμένα ανεξάρτητο. Καμία αλλαγή σε `create_checkout_session`, στο Phase 4 guard, στο webhook processing, ή σε οποιοδήποτε UI — τα νέα πεδία είναι πλήρως αδρανή. Full suite: 354 tests, καθαρό `OK`, **μηδέν expected failures**. `makemigrations --check --dry-run`: `No changes detected` μετά το migration, όπως ζητήθηκε.
- 2026-08-24: **Phase 4 του billing production-readiness audit: αποτράπηκε δεύτερη ενεργή Stripe subscription για τον ίδιο χρήστη — το τελευταίο intentional xfail έγινε green.** Νέο server-side guard στο `create_checkout_session` (`gemiapp/billing.py`), πριν από οποιοδήποτε Phase 3 nonce/idempotency βήμα: `_blocks_new_checkout(sub)` επιστρέφει True/False απευθείας από τοπικό state όταν το `sub.status` είναι ένα από τα ήδη αναγνωρισμένα `LIVE_STRIPE_SUBSCRIPTION_STATUSES = {active, trialing, past_due, unpaid, incomplete, paused}` (μπλοκάρει) ή `TERMINAL_STRIPE_SUBSCRIPTION_STATUSES = {canceled, incomplete_expired}` (επιτρέπει) — σκόπιμα ευρύτερο σύνολο από το `UserSubscription.ALLOWED_PAID_STATUSES = ("active",)`, που αφορά μόνο entitlement, όχι το «θα διπλοχρεωθεί ο πελάτης» ερώτημα. Για οποιαδήποτε άλλη/άγνωστη τιμή status (π.χ. ιστορικό `"inactive"` από webhook που δεν είχε status field) γίνεται φρέσκο `stripe.Subscription.retrieve()` — χωρίς DB lock κατά το network call, καθαρά read-only, δεν γράφει πίσω το αποτέλεσμα στο `UserSubscription` (εκτός scope εδώ) — και αποτυχία retrieve ή ακόμη ασαφές αποτέλεσμα κάνει **fail closed** (block), ποτέ fail open. `cancel_at_period_end` δεν χρειάστηκε ξεχωριστό πεδίο/χειρισμό: το Stripe κρατά `status="active"` μέχρι να λήξει πραγματικά η περίοδος, οπότε καλύπτεται ήδη από τον απλό «active» έλεγχο — pinned με ρητό test. `stripe_customer_id` χωρίς ζωντανό `stripe_subscription_id` ΔΕΝ μπλοκάρει (ο Customer επαναχρησιμοποιείται όπως πριν). Complimentary-only access παραμένει πλήρως ανεξάρτητο (δεν αγγίζει καθόλου το guard, μόνο Stripe state). Σε block: `stripe.checkout.Session.create` ποτέ δεν καλείται, κανένα Phase 3 nonce δεν δημιουργείται, `UserSubscription` παραμένει 100% αμετάβλητο, ο χρήστης βλέπει `messages.info` και redirect στο `/settings/` (υπάρχον «Διαχείριση Συνδρομής» κουμπί προς `customer_portal` — καμία νέα UI). Ρητά **δεν** υλοποιήθηκε: upgrade/downgrade, `stripe.Subscription.modify()`, auto-cancel του υπάρχοντος. Documented residual risk (δεν διορθώθηκε, εκτός scope): δύο πραγματικά ταυτόχρονα πρώτα-checkout requests από διαφορετική session δεν μπλοκάρονται μεταξύ τους πριν ολοκληρωθεί το πρώτο webhook — θα χρειαζόταν lightweight in-progress σήμανση, ρητά αναφερόμενο αντί να υλοποιηθεί αυθαίρετα. Το `DuplicateSubscriptionCheckoutTests.test_an_already_subscribed_user_should_not_get_a_new_checkout_session` (το μοναδικό εναπομείναν xfail από την αρχή του audit) έγινε κανονικό passing test· η κλάση επεκτάθηκε σε 17 tests που καλύπτουν κάθε Stripe status, customer-only, complimentary-only, ambiguous-status resolution και retrieve failure. Καμία migration. Full suite: 346 tests, καθαρό `OK` — **μηδέν expected failures, μηδέν γνωστά ανοιχτά billing bugs.**
- 2026-08-24: **Phase 3 του billing production-readiness audit: Stripe Checkout idempotency + double-submit protection.** Στο `gemiapp/billing.py` (`create_checkout_session`) κάθε `stripe.checkout.Session.create()` παίρνει πλέον `idempotency_key`, παραγόμενο από `_stripe_idempotency_key(user_id, tier, nonce)` (sha256 hash, prefix `ck_`, τίποτα ευαίσθητο μέσα). Το `nonce` είναι ένα random, server-controlled token (`secrets.token_urlsafe(24)`) αποθηκευμένο στο Django session, ένα ανά `(session, tier)` — όχι νέο persistence subsystem. Σημασιολογία: `_checkout_attempt_nonce()` δημιουργεί νέο nonce μόνο αν δεν υπάρχει ήδη ένα για αυτό το tier· `_conclude_checkout_attempt()` το καθαρίζει **μόνο** μετά από επιβεβαιωμένη επιτυχή δημιουργία Stripe Session — ποτέ σε αποτυχία, γιατί μια αποτυχία είναι ασαφής (το Stripe μπορεί να την έχει ήδη επεξεργαστεί) και η σωστή, Stripe-recommended απάντηση σε ασάφεια είναι retry με το **ίδιο** key, όχι νέο. Αποτέλεσμα: διπλό-κλικ ή network-level retry της ίδιας προσπάθειας → ίδιο key (Stripe deduplication)· νέα, συνειδητή προσπάθεια μετά από επιτυχία → νέο key· διαφορετικό tier ή διαφορετικός χρήστης → πάντα διαφορετικό scope. Client-side: νέα `data-checkout-form`/`data-checkout-submit`/`data-loading-label` hooks στο `templates/includes/checkout_button.html` + delegated listener στο `static/js/app.js` (rebuild `npm run build:css` + `collectstatic` για τα νέα `disabled:` Tailwind utilities) — disable μόνο του submitted button, loading label, καμία επίδραση στα άλλα plans· ρητά τεκμηριωμένο ως UX-only layer, όχι source of correctness. Η πραγματική «ήδη συνδρομητής ξεκινά δεύτερο ανεξάρτητο checkout» παραμένει **σκόπιμα μη διορθωμένη** (Phase 4) — το `DuplicateSubscriptionCheckoutTests` xfail δεν αγγίχτηκε καθόλου. Κανένα migration (schema-free phase, όπως ζητήθηκε). Τα tests αυξήθηκαν από 323 σε **331**, `OK (expected failures=1)`.
- 2026-08-24: **Phase 2 του billing production-readiness audit: διορθώθηκε το P0 «χρεώθηκε αλλά δεν έχει entitlement» bug.** Στο `_handle_checkout_session_completed` (`gemiapp/billing.py`) αφαιρέθηκε το εσωτερικό `try/except` που κατάπινε αποτυχία του `stripe.Subscription.retrieve()` και έγραφε `status="inactive"` με HTTP 200 (καμία Stripe retry). Τώρα: το Stripe API read γίνεται **πριν** από οποιοδήποτε DB write και **χωρίς** ανοιχτό transaction/lock· αν αποτύχει, το exception προπαγάρεται ασχολίαστο — δεν εκτελείται κανένα `sub.save()`, το υπάρχον `UserSubscription` μένει 100% ανέπαφο (ούτε καν το `stripe_customer_id`/`stripe_subscription_id` linking γράφεται) — και η εξωτερική διαχείριση του `stripe_webhook` (Phase 1) το πιάνει, γράφει `StripeWebhookEvent.status="failed"` με `error_message`, log με event id/type/subject (ποτέ secrets/payload), και ξανακάνει `raise` ώστε η Django να απαντήσει με το κανονικό της 500 — άρα το Stripe κάνει retry. Σε επόμενο επιτυχές delivery του **ίδιου** `event.id`, ο μηχανισμός retry της Phase 1 (`status="failed"` → ξαναγίνεται `"received"`) τρέχει το processing πλήρως: γράφεται το πραγματικό Stripe state (customer/subscription id, status, tier) μέσα σε ένα μικρό `transaction.atomic()` block, το event γίνεται `processed`, και το entitlement ενεργοποιείται σωστά — πλήρες self-healing χωρίς fabricated state στο ενδιάμεσο. Η ίδια φιλοσοφία («αβεβαιότητα = failure, ποτέ μαντεμένο tier/status») εφαρμόστηκε και στο `_handle_subscription_updated_or_deleted`: νέο helper `_extract_tier_from_items()` κάνει σαφές πού και γιατί ένα αλλοιωμένο `items[0]["price"]["id"]` shape πρέπει να σκάει (όχι να σιωπά σε κενό tier) — η σειρά εκτέλεσης (πρώτα `UserSubscription.objects.get`, μετά τα items) διατηρήθηκε ρητά ίδια ώστε ένα άγνωστο `stripe_subscription_id` να συνεχίσει να αγνοείται με χάρη αντί να γίνεται failed λόγω άσχετου malformed items. Το exception handling στο dispatcher παραμένει σκόπιμα ένα ενιαίο broad `except Exception` (όχι στενεμένο σε `stripe.StripeError`), επειδή τόσο τα πραγματικά Stripe errors (`APIConnectionError`/`APIError`/`RateLimitError`, όλα subclasses του `StripeError` σε αυτή την έκδοση SDK) όσο και ένα απλό `KeyError` από αλλοιωμένο shape πρέπει να καταλήγουν στην ίδια «failed, retryable» έκβαση. Το Phase 0/1 `@unittest.expectedFailure` (`test_a_charged_customer_should_not_silently_lose_entitlement`) έγινε κανονικό passing test. Το duplicate-subscription `@unittest.expectedFailure` **παραμένει σκόπιμα κόκκινο** (Phase 3+, δεν αγγίχτηκε). Full suite: 323 tests, `OK (expected failures=1)`. Κανένα νέο migration.
- 2026-08-23: **Phase 1 του billing production-readiness audit: webhook event persistence + idempotency.** Νέο model `StripeWebhookEvent` (migration `0020_add_stripe_webhook_event`) καταγράφει κάθε επαληθευμένο Stripe webhook delivery πριν από οποιοδήποτε business processing, keyed by το μοναδικό `stripe_event_id` (unique constraint). Το `stripe_webhook` στο `gemiapp/billing.py` σπάστηκε σε `_claim_webhook_event` (persist + απόφαση αν πρέπει να τρέξει processing: νέο event → ναι· `processed`/`ignored` → όχι, 200 χωρίς κανένα Stripe call· `failed` → ναι, retry, με καθαρισμό του `error_message`· `received` από άλλο in-flight delivery → όχι, δεν ξανατρέχει concurrent), `_handle_checkout_session_completed`/`_handle_subscription_updated_or_deleted` (η ΙΔΙΑ business λογική, εξαγόμενη byte-for-byte χωρίς αλλαγή συμπεριφοράς) και `_finalize_webhook_event` (καθορίζει `status`/`error_message`/`processed_at`). Idempotency εξασφαλίζεται σε δύο επίπεδα: `get_or_create` πάνω στο unique `stripe_event_id` (ασφαλές σε race μεταξύ δύο ταυτόχρονων πρώτων deliveries — Django πιάνει το `IntegrityError` εσωτερικά) και `select_for_update()` όταν η γραμμή ήδη υπάρχει (πραγματικό row lock σε Postgres/production· no-op σε SQLite/tests, τεκμηριωμένο ρητά — η πραγματική συνθήκη ανταγωνισμού δεν είναι δοκιμάσιμη σε SQLite, μόνο το DB-level uniqueness invariant που εξαρτάται από αυτήν). Σκόπιμα ΔΕΝ άλλαξε: το γνωστό P0 bug (`stripe.Subscription.retrieve()` αποτυγχάνει μετά από επιτυχές checkout → σιωπηλό `status="inactive"`, HTTP 200) παραμένει ακριβώς ως έχει — το εσωτερικό `try/except` το καταπίνει πριν φτάσει στο νέο outer error handling, οπότε αυτό το event καταγράφεται ως `processed`, όχι `failed`· διόρθωση αυτού είναι Phase 2. Ένα ΠΡΑΓΜΑΤΙΚΑ απρόβλεπτο exception (π.χ. `KeyError` από αλλοιωμένο `items[0]` shape σε `customer.subscription.updated`, ήδη δυνατό πριν από αυτή τη φάση) καταγράφεται τώρα ως `failed` με `error_message`, αλλά συνεχίζει να κάνει re-raise ώστε η Django να απαντήσει 500 ακριβώς όπως πριν — η HTTP σημασιολογία για Stripe retries δεν άλλαξε καθόλου, μόνο προστέθηκε το durable audit trail γύρω της. Το Phase 0 duplicate-delivery `@unittest.expectedFailure` test έγινε κανονικό passing test (αντικατέστησε και το παλιό, πλέον ψευδές, characterisation test που περίμενε `retrieve.call_count == 2`)· τα άλλα δύο Phase 0 xfails (retrieve-failure entitlement loss, duplicate-subscription checkout) παραμένουν σκόπιμα κόκκινα, ΔΕΝ διορθώθηκαν. Read-only-ish registration στο Django admin (`has_add_permission`/`has_change_permission` = False). Τα tests αυξήθηκαν από 299 σε 320 (Phase 0, προηγούμενη εργασία) σε **323**.
- 2026-08-23: **Διορθώθηκαν το επικίνδυνο SMTP default τοπικά και η ασυνέπεια `Company.activities` μεταξύ των import paths (issue #6, δύο ανεξάρτητα υποπροβλήματα).** (Α) Το `EMAIL_BACKEND` ήταν hardcoded σε SMTP ανεξαρτήτως περιβάλλοντος: ένα τοπικό `run_daily_pipeline` με έγκυρα Brevo credentials στο `.env` έστελνε πραγματικά email. Προστέθηκε `resolve_email_backend(debug, override)` στο `config/settings.py`: production (`DJANGO_DEBUG=0`) συνεχίζει να χρησιμοποιεί το πραγματικό SMTP relay αμετάβλητα· non-production (προεπιλογή) πέφτει αυτόματα σε `console.EmailBackend` — τίποτα δεν φεύγει ποτέ στο δίκτυο κατά λάθος, ακόμη κι αν υπάρχουν έγκυρα credentials. Ρητό `EMAIL_BACKEND=...` env var παρακάμπτει το default όποτε χρειάζεται σκόπιμο τοπικό SMTP test. Αφαιρέθηκε το παραπλανητικό `EMAIL_BACKEND=smtp...` από το `.env.example` (θα παρέκαμπτε σιωπηλά το νέο safe default αν αντιγραφόταν σε `.env`). Τα tests δεν εξαρτώνται ποτέ από αυτό — ο Django test runner επιβάλλει ούτως ή άλλως `locmem` global — αλλά προστέθηκε regression test που καλεί απευθείας το `resolve_email_backend()` και κλειδώνει και τα δύο defaults και το explicit opt-in. (Β) Το `Company.activities` (JSONField, γράφεται αλλά δεν διαβάζεται πουθενά στον ζωντανό κώδικα — μοναδικός reader είναι η ιστορική, ήδη εφαρμοσμένη data migration `0002`) διέφερε μεταξύ των δύο import paths: το `import_for_date` το έγραφε πάντα με το σχήμα του `company_defaults()`, ενώ το `import_companies_since_date` το έκανε `pop` πριν το `update_or_create`, αφήνοντάς το είτε στο model default `[]` (νέα εγγραφή) είτε ανεπηρέαστο/μπαγιάτικο (ενημέρωση). Το πεδίο **δεν διαγράφηκε** — παραμένει, απλώς και τα δύο paths γράφουν πλέον ακριβώς το ίδιο σχήμα (`[{"code","description","type"}, ...]`), αφαιρώντας το `pop`. Προστέθηκε regression test που τρέχει το ίδιο δείγμα και από τα δύο paths και επιβεβαιώνει πανομοιότυπο αποτέλεσμα. Τα tests αυξήθηκαν από 271 σε 299 (`manage.py test`, `manage.py check`, `makemigrations --check --dry-run` καθαρά).
- 2026-08-23: **Διερευνήθηκε η ασυμφωνία `last_sent_company_id=841` vs «17.789 Company» — λύθηκε, όχι bug, όχι απώλεια δεδομένων.** Σύνδεση read-only στην πραγματική production Supabase βάση (επιβεβαιώθηκε ταυτοποιώντας τους 4 γνωστούς λογαριασμούς) έδειξε `Company.objects.count()=840`, `max(id)=841` — δηλαδή το `last_sent_company_id` είναι **σωστό και ενήμερο**, όχι σύμπτωμα προβλήματος: είναι per-user pointer πάνω σε *σημερινές μόνο* εγγραφές (`incorporation_date=target_date, id__gt=pointer`, βλ. `send_digests`/`diagnose_intraday`), ποτέ σχεδιασμένο να ισούται με το συνολικό count. Το πραγματικό εύρημα: το `imported_at` (`auto_now_add`, αμετάβλητο) των 840 σειρών δεν πάει ποτέ πριν τις **2026-08-17 18:05 UTC** — ακριβώς τη στιγμή που το commit `34ea38b` («Optimize Render architecture with honcho and Supabase») αφαίρεσε το managed Postgres του Render από το `render.yaml` και έκανε το `DATABASE_URL` χειροκίνητο. Η εφαρμογή δηλαδή **άλλαξε βάση δεδομένων** εκείνη τη μέρα και η τρέχουσα Supabase βάση ουδέποτε είχε άλλα δεδομένα πριν από αυτήν — δεν «χάθηκαν» 16.949 σειρές από αυτήν, απλώς δεν υπήρξαν ποτέ σε αυτήν. Ο αριθμός «17.789» στο ιστορικό της 2026-08-21 (migration `0015` backfill) είναι πλέον τεκμηριωμένα **αναξιόπιστος**: θα σήμαινε ρυθμό ~4.400 νέων εγγραφών/ημέρα για 4 ημέρες, ενώ τα πραγματικά `ImportRun` δείχνουν 1-109/ημέρα — ταιριάζει με το ήδη γνωστό πρόβλημα του project ότι στατικοί αριθμοί εγγραφών περνούν λάθος στα docs (βλ. `AI_SUMMARY.md` §8). Καμία αλλαγή δεδομένων, κανένα import δεν χρειάστηκε. Παράλληλα εντοπίστηκαν ~20 σειρές `Company` με μη ρεαλιστικό μελλοντικό `incorporation_date` (έτη έως 9011) — καταγράφηκε ξεχωριστά στο «Τι απομένει», δεν διερευνήθηκε περαιτέρω (εκτός scope της εργασίας).
- 2026-08-23: **Διερευνήθηκαν τα «χαμένα» 3ωρα email των 11:00 και 14:00 — δεν υπήρχε σφάλμα.** Τα δεδομένα του `diagnose_intraday` σε production: `08:00 new=2`, `11:00 new=0`, `14:00 new=0`, `17:00 new=2`, `20:00 new=2`, σύνολο 6 εταιρείες με σημερινή ημερομηνία σύστασης. Και τα πέντε slots έτρεξαν με `status=success`, καμία αποτυχία task. Δηλαδή στις 11:00 και 14:00 το ΓΕΜΗ δεν είχε δημοσιεύσει **καμία** νέα εγγραφή, και το `send_digests` σιώπησε εσκεμμένα αντί να στείλει άδειο alert. Η αριθμητική κλείνει ακριβώς (2+0+0+2+2 = 6). **Συμπέρασμα: σωστή συμπεριφορά, όχι bug.** Το πρόβλημα ήταν αποκλειστικά ορατότητας — τίποτα στο σύστημα δεν έλεγε «έτρεξα και δεν είχα τι να στείλω».
- 2026-08-23: **Διορθώθηκε παραπλανητική ένδειξη στο `diagnose_intraday`.** Η ενότητα αποστολών εμφάνιζε το `DigestDelivery.sent_at`, που είναι `auto_now_add` και άρα δεν μετακινείται ποτέ από το `update_or_create`. Μια ημέρα με πέντε επιτυχείς αποστολές εμφανιζόταν ως «στάλθηκε μόνο στις 08:00», δηλαδή το ίδιο το διαγνωστικό υπέδειξε πρόβλημα εκεί που δεν υπήρχε. Πλέον η γραμμή γράφει ρητά «1η αποστολή» και προηγείται προειδοποίηση ότι το intraday κρατά **μία** γραμμή ανά ημέρα. Προστέθηκε test που κλειδώνει τη σημασιολογία του `sent_at`.
- 2026-08-23: **Beta mode και κλείσιμο του billing, ρητά.** Η εφαρμογή δηλώνεται πλέον παντού ως beta χωρίς ενεργές πληρωμές. (α) Δύο ανεξάρτητες σημαίες: `BETA_MODE` (ετικέτα) και `LEGAL_BILLING_ACTIVE` (πραγματικός διακόπτης) — χωριστές επειδή οι πληρωμές μπορεί να ανοίξουν πριν φύγει η ένδειξη beta ή το αντίστροφο. (β) Ο έλεγχος είναι **server-side** στο `create_checkout_session` και στο `resume_checkout`, όχι μόνο στο template: ένα απευθείας POST δεν φτάνει ποτέ στο Stripe, με test που επιβεβαιώνει ότι το `stripe.checkout.Session.create` δεν κλήθηκε. (γ) Τα κουμπιά checkout **δεν αποδίδονται καθόλου** αντί να αποδίδονται απενεργοποιημένα — ένα disabled button αφήνει λειτουργικό POST target για ένα devtools click. Μπήκαν στο `includes/checkout_button.html`, ένα σημείο αντί για τρία. (δ) Το JSON-LD δηλώνει `PreOrder` αντί για `InStock`: μια προσφορά «σε απόθεμα» σε προϊόν που δεν πωλείται είναι ψευδής δήλωση προς τις μηχανές αναζήτησης. (ε) Beta badge στο nav, γραμμή στο footer, ενημερωτικό panel στο pricing, ενημέρωση σε `README`, `AI_SUMMARY`, `.env.example`, `render.yaml`. Ο κώδικας του Stripe δεν αφαιρέθηκε: το άνοιγμα των πληρωμών είναι αλλαγή μεταβλητής. Τα tests αυξήθηκαν σε 286.
- 2026-08-23: **Νέα εντολή `manage.py diagnose_intraday`.** Κάθε προηγούμενη διερεύνηση χαμένου email απαιτούσε διάσπαρτα shell queries και η απάντηση εξαρτιόταν από το ποιο θα έτρεχε κανείς. Η εντολή τα συγκεντρώνει με τη σειρά που τα επισκέπτεται το ίδιο το pipeline (scheduler → import runs → δικαιούχοι → `DigestDelivery`), οπότε η πρώτη ενότητα που δείχνει πρόβλημα είναι η αιτία. Είναι read-only. Απαντά σε δύο διαφορετικά ερωτήματα: (α) «δεν λαμβάνω ΠΟΤΕ 3ωρο» — λύνεται από την ενότητα παραληπτών, όπου φαίνεται αν ο λογαριασμός έχει `enterprise`/`custom`· (β) «έλαβα στα άλλα slots αλλά όχι στις 11:00» — λύνεται από την ενότητα `ImportRun`, όπου κάθε slot έχει δική του γραμμή με status και πλήθος νέων εγγραφών. Δείχνει επίσης, ανά δικαιούχο, πόσες εγγραφές εκκρεμούν πέρα από το `last_sent_company_id`, που ξεχωρίζει το «δεν ήρθε τίποτα» από το «ήρθε και δεν στάλθηκε».
- 2026-08-23: **Διόρθωση παλιωμένης τεκμηρίωσης.** Το AGENTS.md δήλωνε 55 tests στην «Τρέχουσα κατάσταση» και 113 στο ιστορικό ενώ τα πραγματικά είναι 271· δήλωνε επίσης σταθερούς αριθμούς εγγραφών (1.226 εταιρείες, 9.744 ΚΑΔ) που είχαν παλιώσει και είχαν ήδη διαρρεύσει λάθος σε δημόσιο κείμενο. Οι αριθμοί που αλλάζουν αφαιρέθηκαν αντί να ενημερωθούν. Καταγράφηκε επίσης η προϋπόθεση εκτέλεσης των tests (`npm run build:css` + `collectstatic`, αλλιώς ~126 ψευδή errors από το manifest storage).
- 2026-08-22: **Performance, SEO και mobile.** (α) Το `base.html` φόρτωνε το `cdn.tailwindcss.com` — 120 KB **JavaScript JIT compiler** που τρέχει στη συσκευή κάθε επισκέπτη και μπλοκάρει το πρώτο render· είναι ρητά development-only. Αντικαταστάθηκε με πραγματικό build (`npm run build:css`, Tailwind CLI 3.4.17): **7.7 KB gzipped CSS**. Το build τρέχει στο `scripts/build_render.sh` και στο `Dockerfile` (που δεν είχε καθόλου Node και θα έστελνε εικόνα χωρίς στυλ). (β) Βελτιστοποιήθηκαν οι εικόνες: `logo.png` 382→105 KB, `favicon.png` 129→22 KB (ήταν 353×353 ενώ προβάλλεται σε 48×48). Συνολικά **−498 KB, −67%** στην πρώτη φόρτωση. (γ) SEO από το μηδέν: `robots.txt`, `sitemap.xml` (`django.contrib.sitemaps`, μόνο δημόσιες σελίδες), `meta description`, canonical, Open Graph/Twitter cards, και `noindex` σε 14 ιδιωτικά templates. (δ) Mobile: το `h-18` του fixed nav **δεν είναι έγκυρη κλάση Tailwind** και δεν παραγόταν ποτέ — το nav δεν είχε ορισμένο ύψος ενώ το `main` αντιστάθμιζε με hardcoded `pt-[72px]`. Ορίστηκε `spacing: {18: '4.5rem'}` και τα δύο δείχνουν πλέον στην ίδια τιμή. Προστέθηκαν `overflow-x-hidden` στο `<html>`, ελάχιστο touch target 44px σε coarse pointers, και containment στους οριζόντιους scrollers. Τα tests αυξήθηκαν σε 113.
- 2026-08-22: **Διορθώθηκε το αδιέξοδο των ανεπιβεβαίωτων λογαριασμών.** Ένας χρήστης που δεν έλαβε (ή έχασε) το email επιβεβαίωσης δεν είχε **καμία** διέξοδο: νέα εγγραφή απορριπτόταν με «Υπάρχει ήδη λογαριασμός», το password reset του Django **αγνοεί σιωπηλά** τους `is_active=False` (0 email), και το μήνυμα login δεν ανέφερε τίποτα για επιβεβαίωση. Αυτό εξηγεί τους κολλημένους λογαριασμούς `filtellis@gmail.com` και `naikos98@hotmail.com`. Προστέθηκε self-service `resend_verification` (rate-limited 5/ώρα, δεν αποκαλύπτει αν υπάρχει λογαριασμός), με συνδέσμους από login και verify_pending, και σαφέστερο μήνυμα στη φόρμα εγγραφής. Η λογική αποστολής βγήκε σε κοινή `send_verification_email()` ώστε signup και resend να μην αποκλίνουν. Η command `resend_verification_emails` απέκτησε `--email` και `--dry-run` (πριν έστελνε αδιακρίτως σε **όλους** τους ανενεργούς) και χρησιμοποιεί `BASE_URL` αντί για hardcoded domain. Σημείωση: το token περιέχει το `last_login`, άρα ο σύνδεσμος είναι μίας χρήσης και λήγει σε 3 ημέρες (`PASSWORD_RESET_TIMEOUT`). Τα tests αυξήθηκαν σε 104.
- 2026-08-22: **Νέος γύρος bug hunting σε περιοχές που δεν είχαν ελεγχθεί.** (α) Το `unsubscribe` έσκαγε με HTTP 500 όταν ο χρήστης δεν είχε `DigestPreference`: το `except` έπιανε `BadSignature`/`User.DoesNotExist` αλλά όχι `RelatedObjectDoesNotExist`. Κάθε digest email περιέχει αυτόν τον σύνδεσμο. Πλέον χρησιμοποιεί `get_or_create` και πιάνει και `SignatureExpired`. (β) Το `company_detail` δημιουργούσε `UserCompanyLead` για **κάθε** συνδεδεμένο χρήστη, ακόμη και χωρίς entitlement, απλώς με την περιήγηση — γεμίζοντας το Lead Inbox και τα metrics του Superadmin με leads που δεν έχουν κανένα `RadarMatch` πίσω τους. Τώρα δημιουργείται lead μόνο με ενεργό entitlement· τα ήδη υπάρχοντα leads παραμένουν ορατά μετά από ακύρωση συνδρομής. (γ) Το `export_csv` δεν είχε κανένα όριο: ένα export χωρίς φίλτρα έγραφε και τις 17.789 εγγραφές σε ένα in-memory `HttpResponse`. Προστέθηκε `MAX_EXPORT_ROWS = 5000` με `.only()` και `.iterator()`. Τα tests αυξήθηκαν σε 94.
- 2026-08-22: **Τα radar matches στο intraday έγιναν incremental.** Το `send_digests` επέλεγε τα `RadarMatch` μόνο με βάση το `matched_on`, οπότε οι ίδιες εταιρείες ραντάρ επαναλαμβάνονταν και στα 6 email της ημέρας, ενώ οι γενικές εγγραφές ήταν ήδη incremental μέσω `last_sent_company_id`. Πλέον **και τα δύο τμήματα** χρησιμοποιούν τον ίδιο δείκτη (`company__id__gt=last_sent_id`), ο οποίος διαβάζεται μία φορά πριν χτιστεί το email και προχωράει **μόνο μετά** από επιτυχή αποστολή — άρα μια αποτυχία SMTP δεν χάνει εγγραφές, ξαναμπαίνουν στο επόμενο 3ωρο. Το ημερήσιο digest παραμένει πλήρες στιγμιότυπο της ημέρας και δεν επηρεάζεται.
- 2026-08-22: **Διορθώθηκε το plain-text digest που έχανε εγγραφές.** Το `templates/emails/daily_digest.txt` δεν απέδιδε ποτέ το `general_companies` — μόνο η HTML έκδοση το έκανε. Όποιος διάβαζε το email σε text client έβλεπε αποκλειστικά τα radar matches, ενώ ο τίτλος μιλούσε για περισσότερες εγγραφές. Εντοπίστηκε από test που συνέκρινε το σώμα του email με τα αναμενόμενα περιεχόμενα. Τα tests αυξήθηκαν σε 86.
- 2026-08-22: **Καταργήθηκε πλήρως το εβδομαδιαίο digest** κατόπιν αιτήματος. Αφαιρέθηκε το `weekly` από τα `FREQUENCIES` των `DigestPreference` και `CustomerRadar`, από το `send_digests` (που πλέον πετάει `ValueError` αν του ζητηθεί), από το `run_daily_pipeline_task` και την ομώνυμη management command (η κυριακάτικη αποστολή), από την `digest_recipients`, από το `send_test_emails.py` και από τα docs. Διαγράφηκαν τα `templates/emails/weekly_digest.{html,txt}`. Το migration `0017` μετατρέπει σε `daily` όσα `DigestPreference`/`CustomerRadar` ήταν σε `weekly`. Στο `DigestDelivery` η τιμή διατηρείται με ετικέτα «Εβδομαδιαία (καταργήθηκε)» επειδή ο πίνακας είναι ιστορικό αρχείο αποστολών· η επανεγγραφή του θα παραποιούσε το ιστορικό και θα συγκρουόταν με το unique constraint `(user, digest_date, frequency)`.
- 2026-08-22: **Το intraday κλειδώθηκε σε ώρα Ελλάδας.** Τα `run_daily_pipeline_task` και `run_intraday_pipeline_task` χρησιμοποιούν πλέον `timezone.localdate()` αντί για `date.today()`, που ακολουθούσε το ρολόι του container (UTC στο Render). Το παράθυρο εκτέλεσης ευθυγραμμίστηκε με τα πραγματικά cron slots (08:00–23:00) και αφαιρέθηκε ο νεκρός κλάδος `hour == 0`.
- 2026-08-22: **Real-Time ορατότητα στο dashboard για Enterprise/Custom.** Οι σημερινές εγγραφές ήταν ήδη ορατές (`incorporation_date__lte=today`), αλλά δεν ξεχώριζαν. Προστέθηκε σήμανση «Σήμερα» σε κάθε σημερινή γραμμή (και στο AJAX pagination) και, μόνο για Enterprise/Custom, live panel με την ώρα της τελευταίας επιτυχημένης 3ωρης κλήσης ΓΕΜΗ και shortcut στις σημερινές. Τα tests αυξήθηκαν σε 82.
- 2026-08-21: **Κάθε χρήστης αποκτά πλέον αυτόματα `DigestPreference`.** Το `send_digests` επαναλαμβάνει πάνω στα `DigestPreference`, οπότε ένας λογαριασμός χωρίς τέτοια εγγραφή ήταν **σιωπηλά απροσπέλαστος όσο υψηλό tier κι αν είχε** — ακριβώς η περίπτωση λογαριασμών Enterprise που δημιουργήθηκαν από το Django admin και δεν πέρασαν ποτέ από signup/dashboard. Το post_save signal του `User` δημιουργεί τώρα και τα δύο (`UserSubscription` + `DigestPreference`) με `get_or_create`, και το migration `0016` κάνει backfill τους υπάρχοντες. Επίσης: η λογική επιλεξιμότητας βγήκε σε κοινή `digest_skip_reason()` ώστε το intraday να μην παρακάμπτει πια τον έλεγχο ύπαρξης email (τον οποίο είχε μόνο το daily), και προστέθηκε η εντολή διάγνωσης `manage.py digest_recipients [--frequency daily|weekly|intraday]` που δείχνει ποιοι θα λάβουν και τον ακριβή λόγο για όσους δεν θα λάβουν, χωρίς να στέλνει τίποτα. Τα tests αυξήθηκαν σε 73.
- 2026-08-21: **Βρέθηκε η πραγματική αιτία που δεν έφευγαν scheduled emails**, με δεδομένα από την production βάση. Υπήρχαν **δύο διπλότυπες γραμμές** `Schedule` για το `run_daily_pipeline_task` (χειροκίνητες, με `name=None`, τύπου DAILY). Το `get_or_create(func=...)` πετούσε `MultipleObjectsReturned`, το σιωπηλό `except` το κατάπινε, και έτσι (α) το intraday CRON schedule **δεν δημιουργήθηκε ποτέ** σε production — εξ ου και μηδέν 3ωρα email — και (β) το ημερήσιο pipeline έτρεχε **δύο φορές ταυτόχρονα** κάθε βράδυ. Οι δύο ταυτόχρονες εκτελέσεις κλείδωναν η μία την άλλη μέσα στο `get_or_create`, οπότε από τις 2026-08-20 κάθε run τερμάτιζε με «Task exceeded maximum timeout value (300 seconds)». Επιπλέον το `retry` (360s) ήταν μόλις 60s πάνω από το `timeout`, οπότε το ίδιο task ξαναμπαινε στην ουρά ενώ ακόμα αποτύγχανε (4 tasks είχαν συσσωρευτεί). Διορθώσεις: το `apps.py` καθαρίζει πλέον τα διπλότυπα πριν το `update_or_create`, το `timeout` ανέβηκε σε 1800s με `retry` 2400s, και προστέθηκε φρουρός `_pipeline_is_already_running()` που αποτρέπει επικαλυπτόμενες εκτελέσεις για την ίδια ημερομηνία (με λήξη ίση με το task timeout, ώστε ένα σκοτωμένο run να μην μπλοκάρει για πάντα). Τα tests αυξήθηκαν σε 65, με νέα που αναπαράγουν ακριβώς την production κατάσταση.
- 2026-08-21: **Ανοιχτό, χρειάζεται απόφαση:** ακόμα και τα επιτυχημένα daily runs (έως 2026-08-19) κατέγραφαν `DigestDelivery status=skipped` με `No active subscription entitlement` για ΟΛΟΥΣ τους χρήστες. Δηλαδή, ακόμη κι όταν το pipeline δούλευε, δεν είχε σε ποιον να στείλει. Πρέπει να επιβεβαιωθεί αν υπάρχει έστω ένας λογαριασμός με ενεργή συνδρομή ή complimentary πρόσβαση.
- 2026-08-21: **Διορθώθηκε ότι δεν έφευγε κανένα scheduled email.** Το intraday schedule δηλωνόταν ως `Schedule.CRON`, αλλά το `croniter` (optional extra του django-q2, `django-q2[croniter]`) έλειπε από το `requirements.txt`. Το `Schedule.calculate_next_run()` πετούσε `ImportError` μέσα στο `transaction.atomic()` του scheduler, το django-q το κατάπινε με `except Exception` και έκανε rollback ΟΛΟΚΛΗΡΟ το pass — άρα ούτε το ημερήσιο digest έφευγε ποτέ, με μόνο ίχνος ένα «Could not create task from schedule» ανά 30 δευτερόλεπτα. Προστέθηκε `croniter==6.2.4`. Επιπλέον: και τα δύο schedules δηλώνονται πλέον ως cron (ημερήσιο στις 09:00 Αθήνας, όπως τεκμηριωνόταν ήδη στο README), το `get_or_create` έγινε `update_or_create` ώστε αλλαγές στον ορισμό να εφαρμόζονται σε υπάρχουσες γραμμές, το `next_run` ορίζεται μόνο κατά τη δημιουργία (ένα deploy δεν πυροδοτεί ξαφνική αποστολή), το `catch_up` απενεργοποιήθηκε ώστε μετά από downtime να μην ξεχυθούν όλα τα χαμένα slots μαζί, και το σιωπηλό `except: pass` στο `apps.py` κάνει πλέον log. Τα tests αυξήθηκαν σε 59, με 4 νέα που τρέχουν τον πραγματικό scheduler του django-q και επαληθεύτηκε ότι αποτυγχάνουν (0 tasks αντί για 2) αν λείψει ξανά το croniter.
- 2026-08-21: **Audit & fixes**. (α) Το `send_digests` έγραφε `DigestDelivery.objects.create()` πάνω σε unique constraint `(user, digest_date, frequency)`: από τη 2η intraday αποστολή κάθε ημέρας πετούσε `IntegrityError`, και το `except` έσκαγε με δεύτερο `IntegrityError` που τερμάτιζε όλο το intraday pipeline — έγινε `update_or_create` (και στο `send_user_yesterday_digest`). (β) Το `import_companies_since_date` τελείωνε καλώντας ανύπαρκτη `run_radar_matching()` (`NameError`)· το matching ξαναγράφτηκε σε `eligible_radars` / `_match_date` / `match_companies_in_range`, με τα ιστορικά `RadarMatch` να κρατούν την ημερομηνία σύστασης ώστε ένα backfill να μη γεμίζει το επόμενο digest. (γ) Το κουμπί «Επιλογή Enterprise» έστελνε `tier=enterprise` που δεν αναγνωριζόταν από το `create_checkout_session` — προστέθηκε `STRIPE_PRICE_ENTERPRISE` και αμφίδρομο mapping price↔tier. (δ) Τα `redirect(url, code=303)` ήταν στην πραγματικότητα 302 (το `redirect()` αγνοεί το `code`) — προστέθηκε `HttpResponseSeeOther`. (ε) Production hardening στο `settings.py` + `render.yaml` (`DJANGO_DEBUG=0`, Stripe/Sentry env vars, σωστό sender domain): το `check --deploy` είναι πλέον καθαρό. (στ) Νέο indexed `Company.search_name` — η αναζήτηση επωνυμίας στα Radars δεν φορτώνει πια όλες τις εταιρείες στην Python (migration `0015` με backfill 17.789 εγγραφών). (ζ) Τα φίλτρα/metrics του Superadmin έγιναν database queries αντί για Python λίστες. (η) Το `RADAR_LIMITS` έγινε single source of truth. (θ) Ξεκόλλησαν 703 αρχεία `node_modules/` από το Git. Τα tests αυξήθηκαν από 44 σε 55.
- 2026-08-07: Clone, δημιουργία `.venv`, migrations, demo seed, tests και τοπικός server.
- 2026-08-07: Προστέθηκε φόρτωση `.env` και πραγματικό GEMI API key.
- 2026-08-07: Εισαγωγή πραγματικών δεδομένων 01/08–07/08 (1.183 εγγραφές εκείνη τη στιγμή) και αφαίρεση demo εταιρειών.
- 2026-08-07: Πίνακας dashboard με όλα τα αποτελέσματα, 20 ορατές γραμμές και scroll.
- 2026-08-07: Αντικατάσταση hardcoded marketing metrics με πραγματικούς αριθμούς βάσης.
- 2026-08-15: Δημιουργήθηκε το παρόν μόνιμο handoff αρχείο και ξεκίνησε η λειτουργία πλήρους καταλόγου ΚΑΔ.
- 2026-08-15: Ολοκληρώθηκε ο μόνιμος κατάλογος 9.651 ΚΑΔ 2025, αυτόματη κάλυψη GEMI-only κωδικών, normalization εταιρικών δραστηριοτήτων, autocomplete πολλαπλής επιλογής και φίλτρα dashboard/CSV/digest. Τα tests αυξήθηκαν από 4 σε 7.
- 2026-08-15: Προστέθηκε φίλτρο χρονικού διαστήματος «Από–Έως» στο dashboard και στο CSV export, με συμπεριληπτικά όρια, καθαρισμό φίλτρων και ασφαλή χειρισμό μη έγκυρων ημερομηνιών. Τα tests αυξήθηκαν από 7 σε 9.
- 2026-08-15: Το εμπορικό όνομα άλλαξε από «GEMI Signal» σε «Gemi Leads» σε UI, email templates, ρυθμίσεις αποστολέα, CSV export, τεκμηρίωση και demo login. Καταγράφηκε το νέο domain `gemileads.gr`.
- 2026-08-15: Σχεδιάστηκε το πλήρες product/technical blueprint της λειτουργίας «Ραντάρ Πελατών», με matching, lead lifecycle, migrations, UI, digest, ασφάλεια, tests και φάσεις υλοποίησης.
- 2026-08-15: Ολοκληρώθηκε η Φάση 1 — Core Radars: νέα models και migrations, migration των digest preferences, idempotent OR/AND matching στο import pipeline, CRUD/preview/pause/soft-delete, ownership security, admin και responsive UI. Τα tests αυξήθηκαν από 9 σε 14.
- 2026-08-15: Ολοκληρώθηκε η Φάση 2 — Lead Inbox: προσωπική λίστα με φίλτρα, lifecycle statuses, αγαπημένα, ιδιωτικές σημειώσεις, αναλυτική εταιρική καρτέλα, match reasons, Radar-specific CSV, dashboard metrics και πλήρες user isolation/POST-only security. Τα tests αυξήθηκαν από 14 σε 17.
- 2026-08-17: Ολοκληρώθηκε η Φάση 3 — Digest integration: Μετάβαση σε Radar-based daily και weekly digests, προσθήκη deduplication για εταιρείες που εμφανίζονται σε πολλαπλά ραντάρ, και υποστήριξη empty digests. Τα tests αυξήθηκαν σε 18.
- 2026-08-17: Ολοκληρώθηκε η Φάση 4 — Plans/production hardening: Μοντέλο UserSubscription, δυναμικά όρια Ραντάρ (Free: 1, Pro: 5, Business: 25) με προστασία δημιουργίας, ενσωμάτωση `django-ratelimit`, PostgreSQL, Sentry.
- 2026-08-17: Ολοκληρώθηκε πλήρως το setup του Email και Domain (Brevo SMTP): Πιστοποίηση του domain `gemileads.gr`, δημιουργία dedicated sender, αποθήκευση SMTP credentials στο `.env` και επιτυχής επαλήθευση αποστολής email σε πραγματικό παραλήπτη.
- 2026-08-17: Ολοκληρώθηκε η Φάση 5 — Auth Flow: Ενσωμάτωση Email verification κατά την εγγραφή (`is_active=False` default, επιβεβαίωση μέσω token), Password reset και Unsubscribe flow χωρίς login. Τα tests αυξήθηκαν σε 21 και περνούν όλα.
- 2026-08-17: Ολοκληρώθηκε η Φάση 6 — Production Setup: Προετοιμασία υποδομής με προσθήκη `psycopg[binary]`, `gunicorn`, και `whitenoise`. Ενσωμάτωση `django-q2` ως scheduler, Docker/Compose, entrypoint και backup scripts.
- 2026-08-17: Ολοκληρώθηκε η Φάση 7 — Stripe Integration: Ενσωμάτωση Stripe για μηνιαίες συνδρομές. Δημιουργήθηκαν σελίδα Pricing, Stripe Checkout, Stripe Customer Portal redirects και ασφαλές `stripe_webhook` για την αυτόματη ενημέρωση του `UserSubscription` tier.
- 2026-08-18: Ολοκληρώθηκε το Focused Redesign της δημόσιας Landing Page: Νέο brand positioning, αφαίρεση Free Trial copy, εισαγωγή CSS/SVG radar logo, product preview with glassmorphic UI, 3-step workflow, feature cards, hardcoded presentation demo records και trust badge.
- 2026-08-18: Ολοκληρώθηκε το **Paid-Only Subscription Hardening**: Το Gemi Leads έγινε paid-only SaaS. Αυστηροποιήθηκε η `UserSubscription.has_active_paid_subscription` και προστέθηκαν πεδία/properties δωρεάν πρόσβασης. Τα tests αυξήθηκαν σε 31 και περνούν όλα.
- 2026-08-18: Ολοκληρώθηκε το **Custom Superadmin Control Center (`/superadmin/`)**: Δημιουργήθηκε αυτόνομο, production-grade administrative interface με `gemiapp/superadmin/` package, `@superadmin_required` decorator, executive SaaS KPIs (MRR/ARR calculation), διαχείριση χρηστών, επισκόπηση συνδρομών, παγκόσμια Ραντάρ, παγκόσμια Leads, GEMI pipeline monitoring, digest delivery log, system health checks, `AdminAuditLog` και User Impersonation flow. Τα tests αυξήθηκαν από 31 σε 40 και περνούν όλα επιτυχώς.
- 2026-08-19: Κλειδώθηκαν οι ακριβείς εκδόσεις των εξαρτήσεων στο `requirements.txt` (συμπεριλαμβανομένου `stripe==15.5.0`, `psycopg[binary]==3.3.4`, `gunicorn==26.0.0`, `whitenoise==6.12.0`, `django-q2==1.11.0`, `honcho==2.0.0`) επιλύοντας το σφάλμα `Exited with status 127` (missing start command binary) κατά το deployment στο Render.
- 2026-08-19: Ολοκληρώθηκαν 3 νέες λειτουργίες: α) Κουμπί «Αποστολή Χθεσινών Εγγραφών» στην καρτέλα κάθε χρήστη στο Superadmin (`/superadmin/users/<id>/`), β) Ευέλικτη παραχώρηση δωρεάν πρόσβασης (Μόνιμη «Για πάντα» ή 1m, 3m, 6m, 1y & προσαρμοσμένη ημερομηνία) με υποστήριξη custom ορίου Ραντάρ, γ) Νέο Top Tier Enterprise/Real-Time (€99/μήνα, 15 Ενεργά Ραντάρ) & Custom Package card στην τιμολόγηση και 3-ωρο GEMI API pipeline (08:00 - 00:00) με ειδοποιήσεις email ΑΠΟΚΛΕΙΣΤΙΚΑ στους Top Tier συνδρομητές. Δημιουργήθηκαν τα migrations `0011` & `0012`, ενημερώθηκαν τα `pricing.html`, `apps.py`, `tasks.py`, `services.py` και αυξήθηκαν τα tests σε 44 (όλα περνούν επιτυχώς).
- 2026-08-19: Ολοκληρώθηκαν 3 νέες βελτιώσεις: α) Infinite Scroll / Pagination ανά 20 εγγραφές στο Dashboard για ταχύτατο loading χωρίς επιβάρυνση μνήμης/DOM, β) Εμπλουτισμός Email Digests με Radar Matches & Όλες τις εγγραφές ΓΕΜΗ (με αυξητική/incremental αποστολή νέων ημερήσιων εγγραφών για Enterprise/Custom), γ) Διόρθωση σφάλματος `incorporation_date` None κατά τη χειροκίνητη αποστολή χθεσινών εγγραφών από το Superadmin. Δημιουργήθηκε το migration `0013` (`last_sent_company_id`) και όλα τα 44 tests περνούν επιτυχώς.
- 2026-08-20: Διορθώθηκε το σφάλμα μη αποστολής 3-ωρων Intraday Real-Time email στους Enterprise/Custom συνδρομητές (`send_digests` στο `gemiapp/services.py`): α) Αφαιρέθηκε ο εσφαλμένος περιορισμός `radar__frequency="intraday"`, β) Εξαιρέθηκαν τα intraday runs από το ημερήσιο κλείδωμα `DigestDelivery`, επιτρέποντας την επαναλαμβανόμενη αποστολή νέων εγγραφών ανά 3 ώρες (08:00 - 00:00), γ) Προστέθηκε αυστηρός έλεγχος δικαιωμάτων Top Tier (`enterprise` / `custom`). Όλα τα 44 unit tests περνούν επιτυχώς.
