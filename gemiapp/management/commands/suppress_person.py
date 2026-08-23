from django.core.management.base import BaseCommand, CommandError

from gemiapp.kad import normalize_kad_search
from gemiapp.models import Company, PersonSuppression


class Command(BaseCommand):
    help = (
        "Απόκρυψη προσώπου που άσκησε δικαίωμα εναντίωσης (άρθρο 21 ΓΚΠΔ). "
        "Χωρίς --gemi η απόκρυψη ισχύει σε όλες τις επιχειρήσεις."
    )

    def add_arguments(self, parser):
        parser.add_argument("name", help="Ονοματεπώνυμο όπως εμφανίζεται στο ΓΕΜΗ.")
        parser.add_argument("--gemi", help="Περιορισμός σε μία επιχείρηση (αριθμός ΓΕΜΗ).")
        parser.add_argument("--reason", default="", help="Εσωτερική σημείωση τεκμηρίωσης.")
        parser.add_argument("--undo", action="store_true", help="Άρση της απόκρυψης.")
        parser.add_argument("--dry-run", action="store_true", help="Δείχνει τι θα γίνει, χωρίς αλλαγές.")

    def handle(self, *args, **options):
        name = options["name"].strip()
        if not name:
            raise CommandError("Το ονοματεπώνυμο δεν μπορεί να είναι κενό.")
        normalized = normalize_kad_search(name)

        company = None
        if options["gemi"]:
            try:
                company = Company.objects.get(gemi_number=options["gemi"])
            except Company.DoesNotExist:
                raise CommandError(f"Δεν βρέθηκε επιχείρηση με ΓΕΜΗ {options['gemi']}.")

        # Reported before acting so the operator can see the blast radius of a global
        # suppression, and catch a misspelled name that would match nothing.
        affected = [
            c for c in Company.objects.exclude(raw_data={}).iterator(chunk_size=500)
            if (company is None or c.pk == company.pk)
            and any(normalize_kad_search(str(p.get("personName") or p.get("businessName") or "")) == normalized
                    for p in (c.raw_data or {}).get("persons") or []
                    if isinstance(p, dict) and not p.get("dtTo"))
        ]
        scope = company.name if company else "όλες τις επιχειρήσεις"
        self.stdout.write(f"Πρόσωπο: {name}")
        self.stdout.write(f"Εμβέλεια: {scope}")
        self.stdout.write(f"Εμφανίζεται σε {len(affected)} επιχειρήσεις:")
        for c in affected[:20]:
            self.stdout.write(f"  · {c.name} ({c.gemi_number})")
        if len(affected) > 20:
            self.stdout.write(f"  … και άλλες {len(affected) - 20}")
        if not affected:
            self.stdout.write(self.style.WARNING(
                "Καμία αντιστοίχιση. Έλεγξε την ορθογραφία, το όνομα πρέπει να είναι όπως στο ΓΕΜΗ."
            ))

        if options["undo"]:
            deleted, _ = PersonSuppression.objects.filter(
                normalized_name=normalized, company=company
            ).delete()
            if options["dry_run"]:
                self.stdout.write(self.style.WARNING("--dry-run: δεν έγινε καμία αλλαγή."))
                return
            self.stdout.write(self.style.SUCCESS(
                f"Άρση απόκρυψης: {deleted} εγγραφή/ές." if deleted else "Δεν υπήρχε ενεργή απόκρυψη."
            ))
            return

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("--dry-run: δεν έγινε καμία αλλαγή."))
            return

        suppression, created = PersonSuppression.objects.get_or_create(
            normalized_name=normalized, company=company,
            defaults={"full_name": name, "reason": options["reason"]},
        )
        if not created and options["reason"]:
            suppression.reason = options["reason"]
            suppression.save(update_fields=["reason"])
        self.stdout.write(self.style.SUCCESS(
            f"Το πρόσωπο αποκρύπτεται πλέον σε {scope}."
            if created else "Υπήρχε ήδη απόκρυψη για αυτό το πρόσωπο."
        ))
