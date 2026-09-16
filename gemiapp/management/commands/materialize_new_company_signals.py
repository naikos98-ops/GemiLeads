from django.core.management.base import BaseCommand, CommandError

from gemiapp.new_company_signals import materialize_new_company_signals


class Command(BaseCommand):
    help = (
        "Δημιουργεί σήματα NEW_COMPANY (πάντα σε shadow) από τα ευρήματα του Discovery v2: μία εγγραφή ανά "
        "εταιρεία που το Gemi Leads είδε για πρώτη φορά, μαζί με τις καθυστερημένες δημοσιεύσεις. Δεν καλεί "
        "το ΓΕΜΗ, δεν δημιουργεί εταιρείες και δεν ειδοποιεί κανέναν. Όσες εταιρείες δεν υπάρχουν ακόμη "
        "τοπικά μένουν εκκρεμείς και υλοποιούνται σε επόμενη εκτέλεση. Μπορεί να τρέξει ξανά χωρίς αλλαγές."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Μόνο αναφορά πλήθους, χωρίς εγγραφές.")
        parser.add_argument("--batch-size", type=int, default=500, help="Εταιρείες ανά batch (προεπιλογή: 500).")
        parser.add_argument("--start-gemi-number", default="", help="Συνέχεια μετά από αυτόν τον αριθμό ΓΕΜΗ.")

    def handle(self, *args, **options):
        if options["batch_size"] < 1:
            raise CommandError("Το --batch-size πρέπει να είναι τουλάχιστον 1.")
        report = materialize_new_company_signals(
            batch_size=options["batch_size"], dry_run=options["dry_run"],
            start_gemi_number=options["start_gemi_number"],
        )
        for line in report.lines():
            self.stdout.write(line)
        if report.conflicts:
            self.stdout.write(self.style.ERROR(f"{report.conflicts} συγκρούσεις: δεν επικαλύφθηκε κανένα υπάρχον γεγονός."))
        if report.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] δεν γράφτηκε τίποτα."))
        else:
            self.stdout.write(self.style.SUCCESS("Η υλοποίηση ολοκληρώθηκε (shadow)."))
