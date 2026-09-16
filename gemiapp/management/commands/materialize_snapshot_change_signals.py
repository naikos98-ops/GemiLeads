from django.core.management.base import BaseCommand, CommandError

from gemiapp.snapshot_change_signals import materialize_all_snapshot_change_signals


class Command(BaseCommand):
    help = (
        "Συγκρίνει διαδοχικά snapshots εταιρειών (B3) και δημιουργεί σήματα Tier-1 (πάντα σε shadow): αλλαγή "
        "κατάστασης, προσθήκη/αφαίρεση ΚΑΔ, αλλαγή νομικής μορφής, αλλαγή δήμου έδρας. Δεν καλεί το ΓΕΜΗ, "
        "δεν αγγίζει Radars, matching ή monitoring και δεν ειδοποιεί κανέναν. Μπορεί να τρέξει ξανά χωρίς "
        "διπλά γεγονότα."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Πλήρης ανίχνευση και αναφορά, χωρίς εγγραφές.")
        parser.add_argument("--batch-size", type=int, default=500, help="Snapshots ανά batch (προεπιλογή: 500).")
        parser.add_argument("--start-snapshot-id", type=int, default=0, help="Συνέχεια μετά από αυτό το id snapshot.")

    def handle(self, *args, **options):
        if options["batch_size"] < 1:
            raise CommandError("Το --batch-size πρέπει να είναι τουλάχιστον 1.")
        if options["start_snapshot_id"] < 0:
            raise CommandError("Το --start-snapshot-id δεν μπορεί να είναι αρνητικό.")
        report = materialize_all_snapshot_change_signals(
            batch_size=options["batch_size"], dry_run=options["dry_run"],
            start_snapshot_id=options["start_snapshot_id"],
        )
        for line in report.lines():
            self.stdout.write(line)
        if report.conflicts:
            self.stdout.write(self.style.ERROR(f"{report.conflicts} συγκρούσεις: δεν επικαλύφθηκε κανένα υπάρχον γεγονός."))
        if report.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] δεν γράφτηκε τίποτα."))
        else:
            self.stdout.write(self.style.SUCCESS("Η ανίχνευση ολοκληρώθηκε (shadow)."))
