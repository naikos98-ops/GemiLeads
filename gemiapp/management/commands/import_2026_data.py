from datetime import date
from django.core.management.base import BaseCommand
from gemiapp.services import import_companies_since_date


class Command(BaseCommand):
    help = "Imports all GEMI companies incorporated since 01/01/2026 up to today."

    def handle(self, *args, **options):
        self.stdout.write("Ξεκινάει η εισαγωγή εγγραφών ΓΕΜΗ από 01/01/2026 έως σήμερα...")
        created, updated = import_companies_since_date(date(2026, 1, 1))
        self.stdout.write(self.style.SUCCESS(f"Ολοκληρώθηκε! Νέες εγγραφές: {created}, Ενημερωμένες: {updated}"))
