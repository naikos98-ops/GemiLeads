from datetime import timedelta
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone
from gemiapp.models import Company, DigestPreference


class Command(BaseCommand):
    help = "Δημιουργεί demo χρήστη και ενδεικτικές επιχειρήσεις."

    def handle(self, *args, **options):
        user, created = User.objects.get_or_create(username="demo@gemileads.gr", defaults={"email": "demo@gemileads.gr", "first_name": "Demo"})
        if created:
            user.set_password("demo12345")
            user.save()
        DigestPreference.objects.get_or_create(user=user)
        today = timezone.localdate()
        samples = [
            ("186420501000", "AEGEAN ROBOTICS ΙΚΕ", "Ιδιωτική Κεφαλαιουχική Εταιρεία", "ΑΤΤΙΚΗΣ", "ΑΘΗΝΑ", 0),
            ("186420502000", "NOMAD FOODS ΜΟΝΟΠΡΟΣΩΠΗ ΙΚΕ", "Ιδιωτική Κεφαλαιουχική Εταιρεία", "ΘΕΣΣΑΛΟΝΙΚΗΣ", "ΘΕΣΣΑΛΟΝΙΚΗ", 0),
            ("186420503000", "BLUE HARBOUR CONSULTING ΕΕ", "Ετερόρρυθμη Εταιρεία", "ΚΥΚΛΑΔΩΝ", "ΣΥΡΟΣ", 1),
            ("186420504000", "OLIVE GRID ENERGY ΑΕ", "Ανώνυμη Εταιρεία", "ΜΕΣΣΗΝΙΑΣ", "ΚΑΛΑΜΑΤΑ", 2),
            ("186420505000", "CIRCUIT CULTURE ΟΕ", "Ομόρρυθμη Εταιρεία", "ΑΧΑΪΑΣ", "ΠΑΤΡΑ", 3),
            ("186420506000", "NORTH STAR LOGISTICS ΙΚΕ", "Ιδιωτική Κεφαλαιουχική Εταιρεία", "ΕΒΡΟΥ", "ΑΛΕΞΑΝΔΡΟΥΠΟΛΗ", 5),
        ]
        for gemi, name, legal_type, prefecture, city, days in samples:
            Company.objects.update_or_create(gemi_number=gemi, defaults={
                "vat_number": gemi[:9], "name": name, "legal_type": legal_type, "status": "Ενεργή",
                "is_active": True, "incorporation_date": today - timedelta(days=days), "prefecture": prefecture,
                "municipality": city, "city": city, "activities": [{"code": "62.01", "description": "Υπηρεσίες τεχνολογίας"}],
            })
        self.stdout.write(self.style.SUCCESS("Demo: demo@gemileads.gr / demo12345"))
