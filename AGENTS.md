# Gemi Leads — μόνιμο project handoff

Το αρχείο αυτό είναι η κοινή μνήμη του project για όλα τα Codex/ChatGPT accounts και όλους τους υπολογιστές που επεξεργάζονται το repository.

## Υποχρεωτική διαδικασία για κάθε Codex

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
.\.venv\Scripts\python.exe manage.py test
.\run_dev.ps1
```

Demo login (μόνο development): `demo@gemileads.gr` / `demo12345`.

## Τρέχουσα κατάσταση

- Το πραγματικό `GEMI_API_KEY` φορτώνεται τοπικά από `.env` μέσω `python-dotenv`.
- Το εμπορικό όνομα της εφαρμογής είναι «Gemi Leads» και το production domain που έχει κατοχυρωθεί είναι `gemileads.gr`.
- Έχουν εισαχθεί πραγματικές εταιρείες για 01/08/2026–07/08/2026 και 14/08/2026 (1.226 εταιρείες στην τρέχουσα τοπική βάση).
- Το επταήμερο μετρά ακριβώς 7 ημερολογιακές ημέρες.
- Το dashboard φορτώνει όλα τα αποτελέσματα σε πίνακα με ορατές έως 20 γραμμές και εσωτερικό scroll.
- Τα εμφανιζόμενα metrics στην αρχική και στο dashboard προέρχονται από τη βάση, όχι από demo αριθμούς.
- Οι 6 demo εταιρείες έχουν αφαιρεθεί από την τοπική βάση. Ο demo χρήστης παραμένει.
- Ο κατάλογος περιλαμβάνει 9.651 επίσημους ΚΑΔ 2025 και 93 συμπληρωματικούς κωδικούς που βρέθηκαν στα πραγματικά δεδομένα ΓΕΜΗ (9.744 συνολικά στην τρέχουσα τοπική βάση).
- Και οι 1.226 αποθηκευμένες εταιρείες έχουν κανονικοποιημένες δραστηριότητες (`CompanyActivity`), συνολικά 8.195 σχέσεις και 100% αντιστοίχιση με αναζητήσιμο ΚΑΔ.
- Το dashboard και οι φόρμες Radar έχουν προσβάσιμο autocomplete ΚΑΔ με debounce, αναζήτηση αριθμού/ελληνικών όρων, keyboard navigation, chips και έως 25 επιλογές.
- Τα φίλτρα ΚΑΔ εφαρμόζονται στο dashboard και στο CSV export με λογική OR.
- Το dashboard υποστηρίζει επιλογή χρονικού διαστήματος «Από–Έως» με συμπεριληπτικά όρια· το ίδιο εύρος εφαρμόζεται και στο CSV export.
- Η Φάση 1 — Core Radars έχει ολοκληρωθεί.
- Η Φάση 2 — Lead Inbox έχει ολοκληρωθεί.
- Το Gemi Leads είναι πλέον **paid-only product**. Paid tiers: Pro (€19/μήνα, 5 Ραντάρ), Business (€49/μήνα, 10 Ραντάρ), Enterprise/Real-Time (€99/μήνα, 15 Ραντάρ) και Custom (κατόπιν συμφωνίας, 15 Ραντάρ). Τα όρια ορίζονται **αποκλειστικά** στο `RADAR_LIMITS` (`gemiapp/models.py`) και μπορούν να παρακαμφθούν ανά λογαριασμό με `custom_radar_limit`.
- Ολοκληρώθηκε το **Custom Superadmin Control Center** στο `/superadmin/`: Dedicated responsive layout, Executive SaaS KPI Metrics & Charts (MRR/ARR calculation), Users Management (deactivate/reactivate, complimentary Pro/Business grant), Subscriptions Overview, Global Radars (Effective Matching Status breakdown), Global Leads & Snapshots (με προστασία απομόνωσης ιδιωτικών σημειώσεων), GEMI Pipeline Operations & Manual Run trigger, Digest Deliveries Log & Retry, Non-destructive System Health monitoring, Audit Log (`AdminAuditLog`), και User Impersonation με καθολικό top banner & ασφαλή επαναφορά identity.
- Υπάρχουν 55 tests και περνούν όλα επιτυχώς. Το `manage.py check`, το `manage.py check --deploy` (με `DJANGO_DEBUG=0`), το `makemigrations --check` και το `git diff --check` είναι καθαρά.
- Το `Company.search_name` είναι denormalized, indexed πεδίο (accent-stripped, uppercase) που ενημερώνεται αυτόματα στο `save()`. Όλες οι αναζητήσεις επωνυμίας των Radars γίνονται πάνω σε αυτό, στη βάση.
- Τα δικαιώματα συνδρομής εκφράζονται και ως database predicates (`paid_subscription_q`, `complimentary_q`, `entitlement_q`, `effective_tier_q` στο `gemiapp/models.py`), ώστε τα φίλτρα του Superadmin να μη φορτώνουν όλους τους χρήστες στη μνήμη. Υπάρχει test που επαληθεύει την ισοδυναμία τους με τα Python properties σε 375 συνδυασμούς.
- Σε production (`DJANGO_DEBUG=0`) ενεργοποιούνται αυτόματα HTTPS redirect, HSTS, secure/HttpOnly cookies, `X_FRAME_OPTIONS=DENY` και `SECURE_PROXY_SSL_HEADER`. Τα `CSRF_TRUSTED_ORIGINS` παράγονται από το `DJANGO_ALLOWED_HOSTS`.

## Σημαντικές αποφάσεις

- Χωρίς ενεργή πληρωμένη συνδρομή (`has_active_paid_subscription == True`), ο χρήστης δεν έχει πρόσβαση στην παραγωγή νέων leads. Το όριο ενεργών Ραντάρ είναι 0.
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

*(Όλα τα βήματα παραγωγής, Stripe integration, Email & Domain, Landing Page, Paid Subscription Logic και Superadmin Control Center έχουν ολοκληρωθεί).*

- Να οριστεί το `STRIPE_PRICE_ENTERPRISE` στο Render environment. Ο κώδικας το υποστηρίζει πλέον πλήρως, αλλά χωρίς αυτό το κουμπί «Επιλογή Enterprise» επιστρέφει τον χρήστη στο pricing. Το System Health το επισημαίνει ως Warning.
- Να επιβεβαιωθεί ότι το `DEFAULT_FROM_EMAIL` στο Render δείχνει στο πιστοποιημένο `notifications@send.gemileads.gr`.
- Ο φάκελος `node_modules/` υπάρχει ακόμη τοπικά αλλά δεν παρακολουθείται πλέον από το Git· μπορεί να διαγραφεί με ασφάλεια.

## Ιστορικό εργασιών

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
