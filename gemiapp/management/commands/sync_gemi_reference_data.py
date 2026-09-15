from django.core.management.base import BaseCommand, CommandError

from gemiapp.ingestion.reference_data import REFERENCE_FAMILY_KEYS, sync_reference_data


class Command(BaseCommand):
    help = (
        "Συγχρονίζει τους τοπικούς πίνακες αναφοράς ΓΕΜΗ (ΚΑΔ 2008/2026, νομοί, δήμοι, καταστάσεις, νομικές "
        "μορφές, υπηρεσίες ΓΕΜΗ, θέματα αποφάσεων) μέσω του κοινού GemiClient. Κάθε απάντηση επικυρώνεται πριν "
        "γραφτεί οτιδήποτε και όλες οι αλλαγές εφαρμόζονται σε μία συναλλαγή. Δεν αλλάζει ActivityCode, "
        "εταιρείες ή Radars. Τρέξε πρώτα με --dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--family",
            action="append",
            dest="families",
            choices=REFERENCE_FAMILY_KEYS,
            help="Μόνο αυτή η οικογένεια (μπορεί να δοθεί πολλές φορές). Προεπιλογή: όλες.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Φέρνει και επικυρώνει τα δεδομένα και δείχνει τις αλλαγές, χωρίς να γράψει τίποτα.",
        )
        parser.add_argument(
            "--force-retire",
            action="store_true",
            help="Αποσύρει εγγραφές ακόμη κι όταν η πηγή επέστρεψε ύποπτα λίγα στοιχεία.",
        )

    def handle(self, *args, **options):
        try:
            result = sync_reference_data(
                options["families"], dry_run=options["dry_run"], force_retire=options["force_retire"],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        prefix = "[dry-run] " if result.dry_run else ""
        for key, counts in result.families.items():
            self.stdout.write(
                f"{prefix}{key}: fetched={counts.fetched} created={counts.created} updated={counts.updated} "
                f"reappeared={counts.reappeared} unchanged={counts.unchanged} retired={counts.retired}"
                + (" retirement_skipped" if counts.retirement_skipped else "")
            )
        for anomaly in result.anomalies:
            self.stdout.write(self.style.WARNING(f"{prefix}ανωμαλία: {anomaly}"))
        if result.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] δεν γράφτηκε τίποτα."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Ο συγχρονισμός ολοκληρώθηκε (run {result.run_id})."))
