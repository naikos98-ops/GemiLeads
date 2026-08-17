# Gemi Leads — Blueprint «Ραντάρ Πελατών»

Κατάσταση: **σχεδιασμένο, αναμένει έγκριση — δεν έχει αρχίσει υλοποίηση**.

## 1. Στόχος

Το Radar μετατρέπει φίλτρα σε μόνιμο κανόνα παρακολούθησης. Το Gemi Leads αποθηκεύει τα κριτήρια, αναγνωρίζει τις νέες εταιρείες που ταιριάζουν και τις εμφανίζει στο lead inbox και στο κατάλληλο digest.

`ΓΕΜΗ import → matching ενεργών Radars → ενιαία leads ανά χρήστη → dashboard / digest`

## 2. Απαράβατοι κανόνες

1. Μία εταιρεία εμφανίζεται μόνο μία φορά ανά χρήστη, ακόμη κι αν ταιριάζει σε πολλά Radars.
2. Ο χρήστης βλέπει πάντα γιατί έγινε το match.
3. Επιλογές ίδιας κατηγορίας λειτουργούν με OR· διαφορετικές κατηγορίες με AND.
4. Κενή κατηγορία σημαίνει «χωρίς περιορισμό».
5. Δημιουργία ή αλλαγή Radar δεν στέλνει αναδρομικά email για παλιές εταιρείες.
6. Κατάσταση, σημειώσεις και αγαπημένα είναι ιδιωτικά ανά χρήστη.
7. Επανάληψη pipeline δεν δημιουργεί διπλά matches ή emails.
8. Διαγραφή Radar δεν διαγράφει εταιρείες ΓΕΜΗ ή χρήσιμα δεδομένα του χρήστη.
9. Όλες οι μεταβολές χρησιμοποιούν POST και CSRF.

## 3. Φίλτρα και validation

Κάθε Radar περιλαμβάνει:

- όνομα 3–80 χαρακτήρων, μοναδικό ανά χρήστη χωρίς διάκριση πεζών/κεφαλαίων,
- ενεργό/ανενεργό,
- έως 25 ΚΑΔ,
- περιφερειακές ενότητες,
- νομικές μορφές,
- προαιρετική φράση στην επωνυμία,
- «μόνο ενεργές επιχειρήσεις»,
- συχνότητα `daily`, `weekly` ή `off`.

Η ημερομηνία δεν αποθηκεύεται ως φίλτρο Radar. Τα «Από–Έως» παραμένουν στην ελεύθερη αναζήτηση αρχείου.

Παράδειγμα:

`(ΚΑΔ 56.10 OR 56.21) AND (Αττική OR Πειραιάς) AND (ΙΚΕ OR ΕΠΕ) AND ενεργή`

Broad Radar χωρίς φίλτρα επιτρέπεται με προειδοποίηση. Πανομοιότυπα κριτήρια επιτρέπονται τεχνικά, αλλά το UI προειδοποιεί.

## 4. Χρονική συμπεριφορά

Κάθε Radar έχει `monitor_from`:

- νέο Radar: τρέχουσα ώρα,
- επανενεργοποίηση: νέα ώρα έναρξης,
- αλλαγή κριτηρίων: ισχύει μόνο για επόμενα imports,
- ιστορική προεπισκόπηση: δεν δημιουργεί «Νέα leads»,
- admin backfill: δεν ειδοποιεί χωρίς ρητή μελλοντική επιλογή `--notify-backfill`.

Το match συνδέεται με το `ImportRun`, όχι μόνο με την ημερομηνία σύστασης.

## 5. Μοντέλα

### `CustomerRadar`

- `user` FK, `name`, `is_active`, `name_query`
- `prefectures` και `legal_types` ως JSON lists
- `only_active`, `frequency`, `monitor_from`
- `activity_codes` ManyToMany προς `ActivityCode`
- `created_at`, `updated_at`, προαιρετικό `deleted_at`
- indexes `(user, is_active)` και `(frequency, is_active)`

### `UserCompanyLead`

Μία εγγραφή ανά χρήστη/εταιρεία:

- `user`, `company`
- `status`: `new`, `viewed`, `contacted`, `interested`, `not_interested`, `archived`
- `is_favorite`, `notes`
- `first_seen_at`, `last_seen_at`, `updated_at`
- unique `(user, company)`
- indexes `(user, status, first_seen_at)` και `(user, is_favorite)`

Η κατάσταση είναι κοινή σε όλα τα Radars του ίδιου χρήστη για να μην υπάρχουν διπλά leads.

### `RadarMatch`

- `radar`, `lead`, `company`, `import_run`
- `matched_on`, `created_at`
- `matched_activity_codes` JSON snapshot
- `match_reason` JSON snapshot με ΚΑΔ/περιοχή/νομική μορφή
- unique `(radar, company)`

Το snapshot εξηγεί το αρχικό match ακόμη κι αν αργότερα αλλάξει το Radar.

### Digest models

Το `DigestPreference` κρατά μόνο καθολικές επιλογές, όπως `include_empty_digest` και μελλοντικά ώρα/ζώνη ώρας. Τα φίλτρα μεταφέρονται στα Radars.

Το `DigestDelivery` αποκτά `cadence`, `period_start`, `period_end`, `radar_count` και unique `(user, cadence, period_start, period_end)`.

## 6. Ασφαλής migration

1. Δημιουργούνται νέα tables χωρίς να αφαιρεθούν παλιά πεδία.
2. Κάθε υπάρχον `DigestPreference` μετατρέπεται σε Radar «Το πρώτο μου ραντάρ».
3. ΚΑΔ, περιοχές, νομικές μορφές, `only_active` και frequency μεταφέρονται.
4. Χρήστης χωρίς φίλτρα παίρνει Radar «Όλες οι νέες επιχειρήσεις».
5. Τα παλιά πεδία παραμένουν μία έκδοση ως fallback και αφαιρούνται μόνο μετά από production verification.

## 7. Matching engine

Νέα υπηρεσία:

`match_imported_companies(import_run) -> MatchSummary`

Ροή:

1. Φορτώνει τις εταιρείες του επιτυχημένου import.
2. Φορτώνει ενεργά Radars με prefetch ΚΑΔ.
3. Αξιολογεί pure function `company_matches_radar(company, radar)`.
4. Δημιουργεί/ενημερώνει ένα `UserCompanyLead` ανά χρήστη/εταιρεία.
5. Δημιουργεί `RadarMatch` με `bulk_create(ignore_conflicts=True)`.
6. Δεν επαναφέρει αρχειοθετημένο lead.
7. Επιστρέφει counters για monitoring.

Pipeline:

`import_for_date → match_imported_companies → send_due_digests`

Unique constraints κάνουν ασφαλή κάθε επανάληψη μετά από αποτυχία. Η αρχική πολυπλοκότητα `εισαγόμενες εταιρείες × ενεργά Radars` είναι επαρκής για MVP· σε μεγάλη κλίμακα μεταφέρεται σε optimized PostgreSQL queries.

## 8. Lead lifecycle

`Νέο → Προβλήθηκε → Επικοινώνησα → Ενδιαφέρεται / Δεν ενδιαφέρεται → Αρχείο`

- Άνοιγμα λεπτομέρειας μετατρέπει μόνο `new` σε `viewed`.
- Ρητή επιλογή χρήστη δεν αλλάζει αυτόματα.
- Αγαπημένο/σημειώσεις είναι ανεξάρτητα από status.
- Ίδιο status εμφανίζεται σε όλα τα Radars που εντόπισαν την εταιρεία.
- Μαζικές αλλαγές μπαίνουν σε δεύτερη φάση.

## 9. Οθόνες

### Dashboard

Cards: νέα σήμερα, αδιάβαστα, ενεργά Radars, ενδιαφερόμενα leads. Η σημερινή λίστα χωρίζεται σε:

- **Lead inbox**: προσωπικά matches.
- **Αρχείο ΓΕΜΗ**: ελεύθερη αναζήτηση σε όλες τις εταιρείες.

### `/radars/`

Cards με όνομα, φίλτρα, συχνότητα, matches 7 ημερών, τελευταίο match και ενέργειες προβολής/επεξεργασίας/παύσης/διαγραφής.

### `/radars/new/` και `/radars/<id>/edit/`

Wizard:

1. Όνομα και στόχος.
2. ΚΑΔ, περιοχή, νομική μορφή, μόνο ενεργές.
3. Προεπισκόπηση, συχνότητα, επιβεβαίωση.

Επαναχρησιμοποιείται το υπάρχον προσβάσιμο KAD picker.

### `/radars/<id>/`

Κριτήρια, counters, matches, φίλτρα status, match reason και Radar-specific CSV.

### `/companies/<gemi_number>/`

Δημόσια στοιχεία ΓΕΜΗ, ΚΑΔ, επίσημη πηγή, Radars που την εντόπισαν, status, αγαπημένο και ιδιωτικές σημειώσεις.

### `/settings/`

Μένουν μόνο καθολικές ρυθμίσεις λογαριασμού/digest. Τα φίλτρα μεταφέρονται αποκλειστικά στα Radars.

## 10. Routes

- `GET /radars/`
- `GET|POST /radars/new/`
- `GET /radars/<int:pk>/`
- `GET|POST /radars/<int:pk>/edit/`
- `POST /radars/<int:pk>/toggle/`
- `POST /radars/<int:pk>/delete/`
- `POST /radars/preview/`
- `GET /leads/`
- `GET /companies/<str:gemi_number>/`
- `POST /leads/<int:pk>/status/`
- `POST /leads/<int:pk>/favorite/`
- `POST /leads/<int:pk>/notes/`
- `GET /radars/<int:pk>/export/`

Όλα τα user-owned queries έχουν `user=request.user`. ID άλλου χρήστη επιστρέφει 404.

## 11. Digest

- Ένα email ανά χρήστη, όχι ανά Radar.
- Sections ανά Radar.
- Μία εταιρεία μία φορά, με badges όλων των Radars.
- Έως 50 εταιρείες στο email· οι υπόλοιπες με count και link.
- Match reason και CTA «Άνοιγμα lead».
- Empty digest μόνο αν το επιλέξει ο χρήστης.
- Weekly περίοδος Δευτέρα–Κυριακή, ώρα `Europe/Athens`.
- Inactive/off Radar δεν δημιουργεί νέα matches ή email sections.

## 12. Όρια πακέτων

Κεντρική entitlement service, όχι hardcoded checks σε views/templates:

- Free: 1 ενεργό Radar, έως 5 ΚΑΔ, ιστορικό 7 ημερών.
- Basic: 5 ενεργά Radars, έως 25 ΚΑΔ/Radar, CSV, ιστορικό 90 ημερών.
- Pro: 25 ενεργά Radars, πλήρες ιστορικό, daily/weekly και μελλοντικές integrations.

Πριν το billing, development/demo χρήστες μπορούν να έχουν προσωρινό Pro entitlement.

## 13. Διαγραφή και retention

- Radar: soft-delete 30 ημερών.
- Μετά τη μόνιμη διαγραφή αφαιρούνται τα matches του.
- Lead παραμένει αν έχει notes, favorite ή επεξεργασμένο status.
- Άδειο ορφανό lead μπορεί να καθαριστεί από maintenance job.
- Διαγραφή χρήστη αφαιρεί Radars/matches/leads/notes, όχι τις κοινές εταιρείες ΓΕΜΗ.

## 14. Ασφάλεια και monitoring

- Ownership σε κάθε view/export.
- CSRF και POST-only mutations.
- Notes ως escaped plain text.
- Το `raw_data` δεν εμφανίζεται αυτούσιο.
- Rate limit σε preview/KAD search πριν το production.
- Admin: Radars, matches/day, matching counters, digest errors, read-only match reasons.
- User dashboard: τελευταίο import, τελευταία αξιολόγηση Radars και τελευταίο digest.

## 15. Tests

### Models/validation

- unique Radar name, όριο ΚΑΔ, user isolation, unique lead/match, status choices.

### Matching

- OR εντός κατηγορίας, AND μεταξύ κατηγοριών, wildcard κενών φίλτρων,
- active/name filters, duplicate prevention, ένα lead από δύο Radars,
- inactive/off Radar, no historical notifications, idempotent retries.

### Views/security

- authentication, 404 άλλου χρήστη, POST-only, CRUD/preview και scoped CSV.

### Digest

- μία αποστολή ανά περίοδο, deduplication, Radar sections, όριο 50, empty preference, daily/weekly, failed retry.

### Migration

- preference με/χωρίς φίλτρα, χρήστης χωρίς preference και μηδενική απώλεια δεδομένων.

## 16. Φάσεις υλοποίησης

1. **Core Radars:** models, migrations, data migration, matching, CRUD, preview, tests.
2. **Lead inbox:** statuses, favorites, notes, company detail, match reason, Radar CSV.
3. **Digest:** unified daily, weekly, delivery constraints/templates, Brevo SMTP test.
4. **Plans/production:** entitlements, PostgreSQL optimization, rate limits, monitoring, billing.

## 17. Acceptance criteria

Η λειτουργία ολοκληρώνεται μόνο όταν:

1. Υπάρχει πλήρες CRUD και pause/resume Radar.
2. Preview και πραγματικό matching συμφωνούν.
3. Νέο import δημιουργεί μόνο σωστά matches.
4. Η ίδια εταιρεία δεν διπλασιάζεται στο inbox.
5. Εμφανίζεται match reason.
6. Status/favorite/notes λειτουργούν με user isolation.
7. CSV σέβεται Radar και ownership.
8. Digest είναι συγκεντρωτικό, deduplicated και idempotent.
9. Migration διατηρεί τις σημερινές προτιμήσεις.
10. Django checks, migrations check, tests και JavaScript syntax check περνούν.
11. Γίνεται χειροκίνητος έλεγχος desktop/mobile.

## 18. Εκτός MVP

- AI scoring, enrichment από τρίτες πηγές, CRM integrations,
- SMS/WhatsApp, team assignment, mobile app,
- αυτόματη αποστολή cold emails.

## 19. Αποφάσεις προς έγκριση

1. Οριστική ονομασία «Ραντάρ Πελατών».
2. Παρακολούθηση μόνο νέων imports· ιστορικό μόνο για preview/αναζήτηση.
3. Κοινό status/notes για την ίδια εταιρεία σε όλα τα Radars ενός χρήστη.
4. Ένα συγκεντρωτικό email ανά χρήστη.
5. Broad Radar επιτρέπεται με προειδοποίηση.
6. Όρια πακέτων 1 / 5 / 25 ενεργά Radars.
7. Υλοποίηση στις τέσσερις φάσεις της ενότητας 16.
