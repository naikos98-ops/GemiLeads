from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from gemiapp.ingestion.monitoring import recompute_company_monitoring


class Command(BaseCommand):
    help = (
        "Υπολογίζει ποιες εταιρείες παρακολουθεί το Gemi Leads 2.0, για ποιους λόγους, με ποια προτεραιότητα "
        "και πότε είναι επόμενη φορά επιλέξιμες για ανανέωση. Δεν καλεί το ΓΕΜΗ και δεν ανανεώνει εταιρείες. "
        "Μπορεί να τρέξει ξανά χωρίς αλλαγές. Τρέξε πρώτα με --dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Μόνο αναφορά πλήθους, χωρίς εγγραφές.")
        parser.add_argument("--batch-size", type=int, default=1000, help="Εταιρείες ανά batch (προεπιλογή: 1000).")
        parser.add_argument("--company-id", type=int, action="append", dest="company_ids", help="Μόνο αυτή η εταιρεία (επαναλαμβανόμενο).")
        parser.add_argument("--start-company-id", type=int, default=0, help="Συνέχεια μετά από αυτό το id εταιρείας.")
        parser.add_argument("--as-of", help="Χρονική στιγμή υπολογισμού, ISO 8601 με ζώνη ώρας (προεπιλογή: τώρα).")

    def handle(self, *args, **options):
        if options["batch_size"] < 1:
            raise CommandError("Το --batch-size πρέπει να είναι τουλάχιστον 1.")
        if options["start_company_id"] < 0:
            raise CommandError("Το --start-company-id δεν μπορεί να είναι αρνητικό.")
        as_of = None
        if options["as_of"]:
            as_of = parse_datetime(options["as_of"])
            if as_of is None or timezone.is_naive(as_of):
                raise CommandError("Το --as-of πρέπει να είναι ημερομηνία-ώρα ISO 8601 με ζώνη ώρας.")
        report = recompute_company_monitoring(
            as_of=as_of, company_ids=options["company_ids"], batch_size=options["batch_size"],
            start_company_id=options["start_company_id"], dry_run=options["dry_run"],
        )
        for line in report.lines():
            self.stdout.write(line)
        if report.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] δεν γράφτηκε τίποτα."))
        else:
            self.stdout.write(self.style.SUCCESS("Ο υπολογισμός ολοκληρώθηκε."))
