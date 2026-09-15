from django.core.management.base import BaseCommand, CommandError

from gemiapp.ingestion.activities import backfill_company_activities


class Command(BaseCommand):
    help = (
        "Συμπληρώνει τα κανονικά μεταδεδομένα ΚΑΔ των εταιρειών (έκδοση ΚΑΔ, τύπος, dtFrom/dtTo, τρέχουσα ή "
        "λήξασα δραστηριότητα) από τα αποθηκευμένα raw_data, σε batches. Δεν καλεί το ΓΕΜΗ, δεν διαγράφει "
        "γραμμές και δεν αλλάζει ό,τι βλέπουν σήμερα τα Radars και οι πελάτες. Μπορεί να τρέξει ξανά χωρίς "
        "αλλαγές. Τρέξε πρώτα με --dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=200, help="Εταιρείες ανά batch (προεπιλογή: 200).")
        parser.add_argument("--start-company-id", type=int, default=0, help="Συνέχεια μετά από αυτό το id εταιρείας (προεπιλογή: 0).")
        parser.add_argument("--dry-run", action="store_true", help="Μόνο αναφορά πλήθους, χωρίς εγγραφές.")

    def handle(self, *args, **options):
        if options["batch_size"] < 1:
            raise CommandError("Το --batch-size πρέπει να είναι τουλάχιστον 1.")
        if options["start_company_id"] < 0:
            raise CommandError("Το --start-company-id δεν μπορεί να είναι αρνητικό.")
        report = backfill_company_activities(
            batch_size=options["batch_size"], dry_run=options["dry_run"], start_company_id=options["start_company_id"],
        )
        for line in report.lines():
            self.stdout.write(line)
        if report.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] δεν γράφτηκε τίποτα."))
        else:
            self.stdout.write(self.style.SUCCESS("Η συμπλήρωση ολοκληρώθηκε."))
