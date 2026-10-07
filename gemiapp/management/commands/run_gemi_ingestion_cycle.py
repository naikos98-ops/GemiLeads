"""Operator-run: one lean GEMI ingestion cycle (dormant; see ``gemiapp.ingestion_cycle``).

    GEMI_DISCOVERY_V2_ENABLED=1 python manage.py run_gemi_ingestion_cycle [--dry-run] [--max-pages N] [--verbose]

Discovery v2 in ingest mode (companies are created from the search page that was fetched, create-only and
date-safe) -> scoped NEW_COMPANY materialisation (always SHADOW) -> the after-commit SHADOW opportunity
pipeline. Deliberately **not** in ``apps.SCHEDULES``, and refused unless GEMI_DISCOVERY_V2_ENABLED is on. The
legacy importer stays the canonical production fetch.
"""

from django.core.management.base import BaseCommand, CommandError

from gemiapp.ingestion_cycle import SKIPPED_LOCKED, IngestionCycleRefused, run_ingestion_cycle


class Command(BaseCommand):
    help = (
        "Ένας λιτός κύκλος ingestion ΓΕΜΗ: Discovery v2 σε ingest (οι εταιρείες δημιουργούνται από τη σελίδα "
        "αναζήτησης που μόλις ήρθε — μόνο δημιουργία, ποτέ ενημέρωση, ποτέ clamped ημερομηνία), υλοποίηση "
        "NEW_COMPANY μόνο για τους αριθμούς αυτής της εκτέλεσης και το pipeline ευκαιριών μετά το commit. Μόνο "
        "SHADOW. Απαιτεί GEMI_DISCOVERY_V2_ENABLED=1· δεν είναι προγραμματισμένο."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Καμία εγγραφή στη βάση. ΚΑΝΕΙ τα αιτήματα αναζήτησης στο ΓΕΜΗ.")
        parser.add_argument("--max-pages", type=int,
                            help="Ανώτατο πλήθος σελίδων αυτής της εκτέλεσης (προεπιλογή: GEMI_DISCOVERY_MAX_PAGES).")
        parser.add_argument("--verbose", action="store_true", help="Μία γραμμή ανά σήμα του pipeline.")

    def handle(self, *args, **options):
        if options["max_pages"] is not None and options["max_pages"] < 1:
            raise CommandError("Το --max-pages πρέπει να είναι τουλάχιστον 1.")
        try:
            report = run_ingestion_cycle(dry_run=options["dry_run"], max_pages=options["max_pages"])
        except IngestionCycleRefused as exc:
            raise CommandError(f"Ο κύκλος σταμάτησε πριν από οποιαδήποτε εγγραφή ή αίτημα ΓΕΜΗ — {exc}") from exc

        for line in report.lines():
            self.stdout.write(line)
        if options["verbose"]:
            for run in report.pipeline_runs:
                self.stdout.write(f"  [pipeline] {run.line()}")
        if report.status == SKIPPED_LOCKED:
            self.stdout.write(self.style.WARNING("Τρέχει ήδη άλλος κύκλος· δεν έγινε τίποτα."))
            return
        if report.failed_phases:
            raise CommandError(
                f"Απέτυχαν φάσεις: {', '.join(name for name, _ in report.failed_phases)} "
                f"(η αναφορά πιο πάνω δείχνει τι ολοκληρώθηκε· τίποτα δεν αναιρέθηκε).")
        self.stdout.write(self.style.SUCCESS(
            "[dry-run] δεν γράφτηκε τίποτα." if report.dry_run else "Ο κύκλος ολοκληρώθηκε (μόνο SHADOW)."))
