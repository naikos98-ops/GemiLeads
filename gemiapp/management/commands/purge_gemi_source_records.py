from django.core.management.base import BaseCommand, CommandError

from gemiapp.ingestion.source_records import purge_expired_source_records


class Command(BaseCommand):
    help = (
        "Διαγράφει τις GEMI source records που έληξαν σύμφωνα με την κατηγορία διατήρησής τους "
        "(GEMI_SOURCE_RECORD_RETENTION_DAYS). Δεν αγγίζει εταιρείες, δραστηριότητες ή leads. "
        "Μπορεί να τρέξει επανειλημμένα. Τρέξε πρώτα με --dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Πλήθος εγγραφών ανά διαγραφή (προεπιλογή: 1000).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Δείξε πόσες θα διαγραφούν, χωρίς να διαγραφεί τίποτα.",
        )

    def handle(self, *args, **options):
        if options["batch_size"] < 1:
            raise CommandError("Το --batch-size πρέπει να είναι τουλάχιστον 1.")
        count = purge_expired_source_records(batch_size=options["batch_size"], dry_run=options["dry_run"])
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"[dry-run] θα διαγράφονταν {count} ληγμένες GEMI source records."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Διαγράφηκαν {count} ληγμένες GEMI source records."))
