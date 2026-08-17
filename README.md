# Gemi Leads

Ένα Django SaaS για καθημερινή εισαγωγή νέων επιχειρήσεων από το επίσημο Open Data API ΓΕΜΗ, ιστορική αναζήτηση, CSV exports και προσωποποιημένα email digests.

> Για Codex/ChatGPT συνεργασία, διάβασε πρώτα το `AGENTS.md`. Είναι η μόνιμη κοινή μνήμη και το handoff του project.

## Τοπική εκκίνηση

Δημιούργησε το απομονωμένο περιβάλλον και εγκατέστησε τις εξαρτήσεις:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\run_dev.ps1
```

Άνοιξε `http://127.0.0.1:8000/`.

Demo λογαριασμός:

- Email: `demo@gemileads.gr`
- Password: `demo12345`

## Πραγματικά δεδομένα ΓΕΜΗ

Ζήτησε προσωπικό API key στο https://opendata.businessportal.gr/register/ και αποθήκευσέ το ως μεταβλητή χρήστη:

```powershell
[Environment]::SetEnvironmentVariable("GEMI_API_KEY", "YOUR_KEY", "User")
$env:GEMI_API_KEY = [Environment]::GetEnvironmentVariable("GEMI_API_KEY", "User")
```

Εισαγωγή προηγούμενης ημέρας και αποστολή digest:

```powershell
.\run_daily.ps1
```

Συγκεκριμένη ημερομηνία χωρίς email:

```powershell
.\.venv\Scripts\python.exe .\manage.py run_daily_pipeline --date 2026-08-01 --skip-email
```

## Κατάλογος και φίλτρα ΚΑΔ

- Ο κατάλογος ΚΑΔ 2025 περιλαμβάνει 9.651 εγγραφές και βρίσκεται σε κανονικοποιημένη μορφή στο `gemiapp/data/kad_2025.json`.
- Η migration `0002` τον φορτώνει μία φορά στη βάση. Η εφαρμογή δεν ανοίγει το αρχικό CSV κατά τη λειτουργία της.
- Οι πραγματικές δραστηριότητες των εταιρειών αποθηκεύονται στον πίνακα `CompanyActivity` και συνδέονται με τους κωδικούς μόνο-ψηφίων του ΓΕΜΗ.
- Κωδικοί που επιστρέφει το ΓΕΜΗ αλλά λείπουν από τον κατάλογο 2025 προστίθενται αυτόματα ως συμπληρωματικές επιλογές.
- Το dashboard και τα Ραντάρ Πελατών υποστηρίζουν πολλαπλή επιλογή ΚΑΔ με λογική OR.

Αν δοθεί νεότερο CSV, ανανέωσε το tracked catalog με:

```powershell
.\.venv\Scripts\python.exe scripts\build_kad_catalog.py "C:\path\to\kad.csv" "gemiapp\data\kad_2025.json"
```

## Ραντάρ Πελατών

Κάθε χρήστης μπορεί να αποθηκεύει μόνιμα κριτήρια ΚΑΔ, περιοχής, νομικής μορφής, επωνυμίας και ενεργής κατάστασης. Στο ίδιο φίλτρο οι επιλογές λειτουργούν με OR, ενώ οι διαφορετικές κατηγορίες συνδυάζονται με AND.

Το daily import αξιολογεί μόνο ενεργά Radars και δημιουργεί ένα ενιαίο lead ανά χρήστη/εταιρεία, ακόμη κι αν η εταιρεία ταιριάξει σε περισσότερα Radars. Τα παλιά δεδομένα χρησιμοποιούνται για preview και δεν δημιουργούν αναδρομικά leads.

Το προσωπικό Lead Inbox υποστηρίζει φίλτρα κατάστασης/Radar, αγαπημένα και αναζήτηση. Κάθε lead έχει κοινό status σε όλα τα Radars, ιδιωτικές σημειώσεις, αναλυτική αιτιολόγηση match και καρτέλα με τα δημόσια στοιχεία και τους ΚΑΔ της επιχείρησης. Κάθε Radar εξάγει το δικό του, user-scoped CSV.

- Διαχείριση Radars: `http://127.0.0.1:8000/radars/`
- Lead Inbox: `http://127.0.0.1:8000/leads/`

## Email

Σε development τα email τυπώνονται στο terminal. Για πραγματική αποστολή, όρισε τις SMTP μεταβλητές του `.env.example` στο environment του server.

## Καθημερινός προγραμματισμός

Σε production εκτέλεσε μία φορά την ημέρα:

```text
python manage.py run_daily_pipeline
```

Προτεινόμενη ώρα: 09:00 Europe/Athens. Σε Linux χρησιμοποίησε cron/systemd timer· σε Windows Task Scheduler το `run_daily.ps1`.

## Production checklist

- PostgreSQL αντί SQLite
- `DJANGO_DEBUG=0` και ισχυρό `DJANGO_SECRET_KEY`
- HTTPS, production WSGI server και `collectstatic`
- SMTP provider με SPF/DKIM/DMARC
- Daily database backups και error monitoring
- Celery/Redis όταν ο όγκος χρηστών απαιτήσει background email queues
