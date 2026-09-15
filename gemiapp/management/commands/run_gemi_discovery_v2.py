from datetime import date

from django.core.management.base import BaseCommand, CommandError

from gemiapp.ingestion.discovery import INGEST, SHADOW, compare_with_legacy, run_discovery


class Command(BaseCommand):
    help = (
        "Discovery v2: εντοπίζει νέες δημοσιευμένες εταιρείες ΓΕΜΗ σελιδοποιώντας κατά φθίνοντα αριθμό ΓΕΜΗ, "
        "μαζί με τις καθυστερημένες δημοσιεύσεις που χάνει ο σημερινός importer. Σε shadow (προεπιλογή) "
        "καταγράφει μόνο τι ΘΑ εντόπιζε: δεν δημιουργεί ούτε αλλάζει εταιρείες, digests ή matching. "
        "Ο σημερινός importer παραμένει η πηγή παραγωγής."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Χωρίς καμία εγγραφή, ούτε run/cursor.")
        parser.add_argument("--mode", choices=[SHADOW, INGEST], default=SHADOW, help="shadow (προεπιλογή) ή ingest (απαιτεί GEMI_DISCOVERY_V2_ENABLED).")
        parser.add_argument("--max-pages", type=int, help="Όριο σελίδων για αυτή την εκτέλεση.")
        parser.add_argument("--compare", metavar="YYYY-MM-DD", help="Αναφορά σύγκρισης legacy/v2 για αυτή την ημέρα (μόνο ανάγνωση).")

    def handle(self, *args, **options):
        if options["compare"]:
            try:
                target = date.fromisoformat(options["compare"])
            except ValueError as exc:
                raise CommandError("Το --compare θέλει ημερομηνία YYYY-MM-DD.") from exc
            for line in compare_with_legacy(target).lines():
                self.stdout.write(line)
            return
        if options["max_pages"] is not None and options["max_pages"] < 1:
            raise CommandError("Το --max-pages πρέπει να είναι τουλάχιστον 1.")
        try:
            result = run_discovery(mode=options["mode"], dry_run=options["dry_run"], max_pages=options["max_pages"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        for line in result.lines():
            self.stdout.write(line)
        if result.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] δεν γράφτηκε τίποτα."))
        elif result.status == "success":
            self.stdout.write(self.style.SUCCESS("Η εκτέλεση ολοκληρώθηκε."))
        else:
            self.stdout.write(self.style.WARNING(f"Η εκτέλεση τερμάτισε με status={result.status}· ο cursor δεν προχώρησε."))
