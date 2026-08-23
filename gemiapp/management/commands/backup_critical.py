"""Write the non-recoverable data to a file, for use over SSH or a shell."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from gemiapp.backups import backup_filename, backup_json, build_backup


class Command(BaseCommand):
    help = (
        "Εξάγει τα δεδομένα που ΔΕΝ ανακτώνται από το ΓΕΜΗ (χρήστες, συνδρομές, radars, "
        "leads, εναντιώσεις) σε ένα αρχείο JSON."
    )

    def add_arguments(self, parser):
        parser.add_argument("--output", help="Διαδρομή αρχείου (προεπιλογή: backups/<ημερομηνία>.json).")
        # Not named --stdout: call_command always passes a stdout kwarg of its own, which
        # would make this flag permanently true and silently ignore --output.
        parser.add_argument("--print", action="store_true", dest="print_only",
                            help="Εκτύπωση στην έξοδο αντί για αρχείο.")

    def handle(self, *args, **options):
        payload = build_backup()
        text = backup_json()

        if options["print_only"]:
            self.stdout.write(text)
            return

        path = Path(options["output"]) if options["output"] else Path("backups") / backup_filename()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"Αποτυχία εγγραφής στο {path}: {exc}") from exc

        counts = {k: len(v) for k, v in payload.items() if isinstance(v, list)}
        for name, n in counts.items():
            self.stdout.write(f"  {name:22} {n:>6}")
        self.stdout.write(self.style.SUCCESS(
            f"\nΑποθηκεύτηκε: {path}  ({path.stat().st_size / 1024:.1f} KB)"
        ))
        if not counts["users"]:
            self.stdout.write(self.style.WARNING("Προσοχή: κανένας χρήστης. Σίγουρα τρέχεις σε σωστή βάση;"))
