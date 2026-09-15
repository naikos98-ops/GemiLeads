from django.core.management.base import BaseCommand, CommandError

from gemiapp.ingestion.discovery import bootstrap_discovery_cursor


class Command(BaseCommand):
    help = (
        "Αρχικοποιεί τον cursor του Discovery v2 με επαληθευμένη σάρωση: σελιδοποιεί από τον νεότερο αριθμό "
        "ΓΕΜΗ προς τα πίσω και απαιτεί αρκετές ήδη γνωστές εγγραφές πριν καταγράψει σύνορο. Το σύνορο είναι ο "
        "υψηλότερος αριθμός ΓΕΜΗ που υπάρχει ήδη τοπικά, ώστε καμία άγνωστη εγγραφή να μη μείνει κρυφή. "
        "Δεν δημιουργεί εταιρείες."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Μόνο αναφορά, χωρίς εγγραφές.")
        parser.add_argument(
            "--force", action="store_true",
            help="Ρητή επανα-αρχικοποίηση υπάρχοντος cursor (π.χ. μετά από ανωμαλία). Μόνο για διαχειριστή.",
        )

    def handle(self, *args, **options):
        try:
            result = bootstrap_discovery_cursor(dry_run=options["dry_run"], force=options["force"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        for line in result.lines():
            self.stdout.write(line)
        if result.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] δεν γράφτηκε τίποτα."))
        elif result.status == "success":
            self.stdout.write(self.style.SUCCESS(f"Ο cursor αρχικοποιήθηκε στο {result.resulting_high_water_mark}."))
        else:
            self.stdout.write(self.style.WARNING(f"Δεν αρχικοποιήθηκε cursor (status={result.status})."))
