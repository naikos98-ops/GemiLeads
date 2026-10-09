"""Operator-run: one run of the dormant same-day discovery lane (``gemiapp.ingestion.sameday_discovery``).

    GEMI_DISCOVERY_SAMEDAY_LANE_ENABLED=1 python manage.py run_gemi_sameday_discovery --dry-run [--max-pages N]

Pages ``/companies`` by ``-incorporationDate`` (an active and an inactive pass) over the local dates
``[today - lookback, today]`` and reports what it finds. With ``--dry-run`` nothing is written -- no Company, no
run row, no observation -- but the search pages **are** requested from GEMI. Without it the lane creates the
missing companies through the shared create-only writer and stores its evidence; their SHADOW signals are
materialised by the next ingestion cycle's catch-up, not here.

Refused unless both ``GEMI_DISCOVERY_SAMEDAY_LANE_ENABLED`` and ``GEMI_DISCOVERY_V2_ENABLED`` are on. Not
scheduled: the ingestion cycle runs the lane only while the first flag is on, and it is off by default.
"""

from django.core.management.base import BaseCommand, CommandError

from gemiapp.ingestion.sameday_discovery import SameDayLaneRefused, run_sameday_discovery


class Command(BaseCommand):
    help = (
        "Μία εκτέλεση της (αδρανούς) same-day λωρίδας discovery: σελίδες /companies κατά -incorporationDate, "
        "ενεργές και ανενεργές, για τις τοπικές ημερομηνίες [χθες, σήμερα]. Μόνο δημιουργία, ποτέ ενημέρωση· "
        "κανένα αίτημα ανά εταιρεία. Απαιτεί GEMI_DISCOVERY_SAMEDAY_LANE_ENABLED=1 και "
        "GEMI_DISCOVERY_V2_ENABLED=1· δεν είναι προγραμματισμένο."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Καμία εγγραφή στη βάση. ΚΑΝΕΙ τα αιτήματα αναζήτησης στο ΓΕΜΗ.")
        parser.add_argument("--max-pages", type=int,
                            help="Ανώτατο πλήθος σελίδων ανά πέρασμα (προεπιλογή: GEMI_DISCOVERY_SAMEDAY_MAX_PAGES).")

    def handle(self, *args, **options):
        if options["max_pages"] is not None and options["max_pages"] < 1:
            raise CommandError("Το --max-pages πρέπει να είναι τουλάχιστον 1.")
        try:
            result = run_sameday_discovery(dry_run=options["dry_run"], max_pages=options["max_pages"])
        except SameDayLaneRefused as exc:
            raise CommandError(f"Η λωρίδα σταμάτησε πριν από οποιαδήποτε εγγραφή ή αίτημα ΓΕΜΗ — {exc}") from exc

        self.stdout.write("SAME-DAY DISCOVERY (by -incorporationDate; active and inactive passes)")
        for line in result.lines():
            self.stdout.write(f"  {line}")
        self.stdout.write("  zero per-company lookups; existing companies are never updated; no signal is created here.")
        if result.status != "success":
            raise CommandError(f"Η εκτέλεση δεν ολοκληρώθηκε: status={result.status} "
                               f"stop_reason={result.stop_reason or 'error'}.")
        self.stdout.write(self.style.SUCCESS(
            "[dry-run] δεν γράφτηκε τίποτα· οι σελίδες αναζήτησης ΖΗΤΗΘΗΚΑΝ από το ΓΕΜΗ." if result.dry_run
            else "Η εκτέλεση ολοκληρώθηκε (μόνο δημιουργία· τα σήματα υλοποιούνται από τον επόμενο κύκλο)."))
