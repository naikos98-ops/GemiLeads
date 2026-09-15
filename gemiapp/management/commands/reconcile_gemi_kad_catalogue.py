from django.core.management.base import BaseCommand, CommandError

from gemiapp.ingestion.kad_catalogue import radar_criteria_compatibility, reconcile_kad_catalogue


class Command(BaseCommand):
    help = (
        "Συνδέει τον κατάλογο ΚΑΔ της εφαρμογής (ActivityCode) με τα reference data ΓΕΜΗ (GemiKad) με βάση τον "
        "κωδικό και την έκδοση ΚΑΔ, και αναφέρει την κατάταξη κάθε ΚΑΔ και των αποθηκευμένων κριτηρίων των "
        "Radars. Μόνο τοπικά δεδομένα, χωρίς κλήση στο ΓΕΜΗ. Δεν αλλάζει ActivityCode, Radars ή τον επιλογέα "
        "ΚΑΔ. Μπορεί να τρέξει ξανά χωρίς αλλαγές. Τρέξε πρώτα με --dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Μόνο αναφορά πλήθους, χωρίς εγγραφές.")
        parser.add_argument("--batch-size", type=int, default=1000, help="Εγγραφές ActivityCode ανά batch (προεπιλογή: 1000).")
        parser.add_argument(
            "--list-radar-criteria", action="store_true",
            help="Εμφανίζει κάθε αποθηκευμένο κριτήριο ΚΑΔ (id Radar, κωδικός, κατάταξη), χωρίς ονόματα.",
        )

    def handle(self, *args, **options):
        if options["batch_size"] < 1:
            raise CommandError("Το --batch-size πρέπει να είναι τουλάχιστον 1.")
        report = reconcile_kad_catalogue(dry_run=options["dry_run"], batch_size=options["batch_size"])
        for line in report.lines():
            self.stdout.write(line)
        if options["list_radar_criteria"]:
            for radar_id, is_active, code, compatibility in radar_criteria_compatibility():
                reason = f" reason={compatibility.unresolved_reason}" if compatibility.unresolved_reason else ""
                self.stdout.write(
                    f"radar={radar_id} active={'yes' if is_active else 'no'} code={code} "
                    f"class={compatibility.classification}{reason}"
                )
        if report.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] δεν γράφτηκε τίποτα."))
        else:
            self.stdout.write(self.style.SUCCESS("Η συμφιλίωση ολοκληρώθηκε."))
