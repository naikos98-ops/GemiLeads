# GEMI Signal

Ένα Django SaaS για καθημερινή εισαγωγή νέων επιχειρήσεων από το επίσημο Open Data API ΓΕΜΗ, ιστορική αναζήτηση, CSV exports και προσωποποιημένα email digests.

## Τοπική εκκίνηση

Το απομονωμένο `.venv` είναι ήδη έτοιμο. Από PowerShell:

```powershell
cd "C:\Users\iotel\Documents\Codex\2026-08-02\ai\outputs\gemi_signal"
.\run_dev.ps1
```

Άνοιξε `http://127.0.0.1:8000/`.

Demo λογαριασμός:

- Email: `demo@gemisignal.gr`
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
