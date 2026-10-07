"""Read-only: unified ingestion vs legacy importer for one day, and the ingestion's current health.

    python manage.py report_gemi_ingestion_parity [--date YYYY-MM-DD]

Answers "did the legacy importer discover a valid company the unified ingestion failed to discover or store?"
from recorded provenance (``gemiapp.ingestion_parity``), never from a Company row merely existing. No GEMI
request, no write. The default date is yesterday (local), the last day both pipelines have finished with.
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from gemiapp.ingestion_alerts import ingestion_health
from gemiapp.ingestion_parity import compare_unified_with_legacy


class Command(BaseCommand):
    help = (
        "Μόνο ανάγνωση: σύγκριση της ενιαίας ingestion με τον legacy importer για μία ημέρα (με βάση την "
        "καταγεγραμμένη προέλευση, όχι την ύπαρξη της εταιρείας) και η τρέχουσα υγεία της ingestion. Κανένα "
        "αίτημα ΓΕΜΗ, καμία εγγραφή."
    )

    def add_arguments(self, parser):
        parser.add_argument("--date", metavar="YYYY-MM-DD", help="Ημέρα σύγκρισης (προεπιλογή: χθες).")

    def handle(self, *args, **options):
        target = timezone.localdate() - timedelta(days=1)
        if options["date"]:
            try:
                target = date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError("Το --date θέλει ημερομηνία YYYY-MM-DD.") from exc
        report = compare_unified_with_legacy(target)
        for line in report.lines():
            self.stdout.write(line)
        self.stdout.write("INGESTION HEALTH (now)")
        for line in ingestion_health():
            self.stdout.write(f"  {line}")
        if report.verdict == "MISS":
            raise CommandError("Η ενιαία ingestion έχασε ή δεν αποθήκευσε εταιρεία που βρήκε ο legacy importer "
                               "(βλ. τους αριθμούς ΓΕΜΗ πιο πάνω).")
