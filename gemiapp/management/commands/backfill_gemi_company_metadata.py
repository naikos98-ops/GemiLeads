from django.core.management.base import BaseCommand, CommandError

from gemiapp.ingestion.company_metadata import backfill_company_metadata


class Command(BaseCommand):
    help = (
        "Συμπληρώνει τα νέα πεδία Gemi Leads 2.0 των εταιρειών (κωδικοί αναφοράς ΓΕΜΗ, ποιότητα ημερομηνίας "
        "σύστασης, first/last seen) από τα αποθηκευμένα raw_data, σε batches. Δεν καλεί το ΓΕΜΗ και δεν "
        "αλλάζει κανένα υπάρχον πεδίο. Μπορεί να τρέξει ξανά χωρίς αλλαγές. Τρέξε πρώτα με --dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500, help="Εταιρείες ανά batch (προεπιλογή: 500).")
        parser.add_argument("--start-id", type=int, default=0, help="Συνέχεια μετά από αυτό το id (προεπιλογή: 0).")
        parser.add_argument("--dry-run", action="store_true", help="Μόνο αναφορά πλήθους, χωρίς εγγραφές.")

    def handle(self, *args, **options):
        if options["batch_size"] < 1:
            raise CommandError("Το --batch-size πρέπει να είναι τουλάχιστον 1.")
        if options["start_id"] < 0:
            raise CommandError("Το --start-id δεν μπορεί να είναι αρνητικό.")
        report = backfill_company_metadata(
            batch_size=options["batch_size"], dry_run=options["dry_run"], start_id=options["start_id"],
        )
        for line in report.lines():
            self.stdout.write(line)
        if report.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] δεν γράφτηκε τίποτα."))
        else:
            self.stdout.write(self.style.SUCCESS("Η συμπλήρωση ολοκληρώθηκε."))
