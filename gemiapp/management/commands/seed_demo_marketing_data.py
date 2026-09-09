from datetime import timedelta
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from gemiapp.models import (
    ActivityCode,
    Company,
    CompanyActivity,
    CustomerRadar,
    DigestPreference,
    RadarMatch,
    UserCompanyLead,
    UserSubscription,
)


class Command(BaseCommand):
    help = "Δημιουργεί ή καθαρίζει ασφαλή, εικονικά δεδομένα marketing/demo για το demo@gemileads.gr."

    DEMO_GEMI_PREFIX = "99900010"

    def add_arguments(self, parser):
        parser.add_argument(
            "--clean",
            action="store_true",
            help="Καθαρίζει μόνο τα εικονικά δεδομένα marketing που έχουν δημιουργηθεί για το demo.",
        )

    def handle(self, *args, **options):
        user, created = User.objects.get_or_create(
            username="demo@gemileads.gr",
            defaults={"email": "demo@gemileads.gr", "first_name": "Demo", "last_name": "User"},
        )
        if created:
            user.set_password("demo12345")
            user.save()

        # Ensure subscription & digest preference
        sub, _ = UserSubscription.objects.get_or_create(user=user)
        sub.tier = "pro"
        sub.status = "active"
        sub.save()

        DigestPreference.objects.get_or_create(user=user)

        if options.get("clean"):
            self._clean_demo_data(user)
            self.stdout.write(self.style.SUCCESS("Καθαρίστηκαν επιτυχώς τα εικονικά δεδομένα marketing."))
            return

        self._seed_demo_data(user)
        self.stdout.write(self.style.SUCCESS("Δημιουργήθηκαν επιτυχώς τα εικονικά δεδομένα marketing για το demo@gemileads.gr!"))

    def _clean_demo_data(self, user):
        companies = Company.objects.filter(gemi_number__startswith=self.DEMO_GEMI_PREFIX)
        UserCompanyLead.objects.filter(company__in=companies).delete()
        RadarMatch.objects.filter(company__in=companies).delete()
        companies.delete()

        demo_radar_names = [
            "Logistics & Εφοδιαστική",
            "Tech Startups & IT",
            "Ανανεώσιμες Πηγές Ενέργειας",
            "Τουρισμός & Εστίαση",
        ]
        CustomerRadar.objects.filter(user=user, name__in=demo_radar_names).delete()

    def _seed_demo_data(self, user):
        today = timezone.localdate()

        # 1. Activities (KAD)
        kad_data = [
            ("52.29", "Άλλες συνοδευτικές της μεταφοράς δραστηριότητες"),
            ("62.01", "Δραστηριότητες προγραμματισμού ηλεκτρονικών συστημάτων"),
            ("43.21", "Ηλεκτρικές εγκαταστάσεις"),
            ("56.10", "Δραστηριότητες υπηρεσιών εστιατορίων και κινητών μονάδων εστίασης"),
        ]

        kad_objs = {}
        for code, desc in kad_data:
            ac, _ = ActivityCode.objects.get_or_create(
                code=code,
                defaults={"normalized_code": code.replace(".", ""), "description": desc, "search_text": f"{code} {desc}"},
            )
            kad_objs[code] = ac

        # 2. Customer Radars
        radars_spec = [
            ("Logistics & Εφοδιαστική", ["52.29"], ["ΘΕΣΣΑΛΟΝΙΚΗΣ"]),
            ("Tech Startups & IT", ["62.01"], ["ΑΤΤΙΚΗΣ"]),
            ("Ανανεώσιμες Πηγές Ενέργειας", ["43.21"], ["ΛΑΡΙΣΑΣ"]),
            ("Τουρισμός & Εστίαση", ["56.10"], ["ΧΑΝΙΩΝ"]),
        ]

        radar_objs = {}
        for name, codes, prefectures in radars_spec:
            r, _ = CustomerRadar.objects.update_or_create(
                user=user,
                name=name,
                defaults={
                    "is_active": True,
                    "prefectures": prefectures,
                    "legal_types": [],
                    "only_active": True,
                    "frequency": "daily",
                },
            )
            r.activity_codes.set([kad_objs[c] for c in codes])
            radar_objs[name] = r

        # 3. Fictional Companies
        companies_spec = [
            {
                "gemi_number": "999000101000",
                "vat_number": "999900101",
                "name": "ΑΙΓΑΙΟ LOGISTICS ΜΟΝ. Ι.Κ.Ε.",
                "legal_type": "Μονοπρόσωπη Ιδιωτική Κεφαλαιουχική Εταιρεία",
                "status": "Ενεργή",
                "incorporation_date": today - timedelta(days=1),
                "prefecture": "ΘΕΣΣΑΛΟΝΙΚΗΣ",
                "municipality": "Θεσσαλονίκη",
                "city": "Θεσσαλονίκη",
                "address": "Εγνατίας 154",
                "postal_code": "54636",
                "email": "info@aegean-logistics-demo.gr",
                "website": "https://www.aegean-logistics-demo.gr",
                "activities": [{"code": "52.29", "description": "Άλλες συνοδευτικές της μεταφοράς δραστηριότητες"}],
                "persons": [
                    {
                        "personName": "Ελένη Παπαδοπούλου",
                        "role": "Διαχειριστής & Νόμιμος Εκπρόσωπος",
                        "category": "Φυσικό Πρόσωπο",
                        "percentage": "100%",
                        "isRepresentativeAlone": True,
                    }
                ],
                "radar_name": "Logistics & Εφοδιαστική",
                "matched_codes": ["52.29"],
                "lead_status": "interested",
                "is_favorite": True,
                "notes": "Επικοινωνία με κ. Παπαδοπούλου στις 08/09. Ζήτησε προσφορά για ετήσιο συμβόλαιο εφοδιαστικής αλυσίδας.",
            },
            {
                "gemi_number": "999000102000",
                "vat_number": "999900102",
                "name": "HELLAS CLOUD & DATA LABS Α.Ε.",
                "legal_type": "Ανώνυμη Εταιρεία",
                "status": "Ενεργή",
                "incorporation_date": today - timedelta(days=2),
                "prefecture": "ΑΤΤΙΚΗΣ",
                "municipality": "Αθήνα",
                "city": "Αθήνα",
                "address": "Λεωφ. Κηφισίας 220",
                "postal_code": "15124",
                "email": "contact@hellascloud-demo.gr",
                "website": "https://www.hellascloud-demo.gr",
                "activities": [{"code": "62.01", "description": "Δραστηριότητες προγραμματισμού ηλεκτρονικών συστημάτων"}],
                "persons": [
                    {
                        "personName": "Νικόλαος Αλεξίου",
                        "role": "Διευθύνων Σύμβουλος",
                        "category": "Φυσικό Πρόσωπο",
                        "percentage": "",
                        "isRepresentativeAlone": True,
                    }
                ],
                "radar_name": "Tech Startups & IT",
                "matched_codes": ["62.01"],
                "lead_status": "contacted",
                "is_favorite": False,
                "notes": "Αποστολή εταιρικής παρουσίασης για cloud infrastructure & DevOps services.",
            },
            {
                "gemi_number": "999000103000",
                "vat_number": "999900103",
                "name": "GREEN GRID SOLAR SOLUTIONS Ι.Κ.Ε.",
                "legal_type": "Ιδιωτική Κεφαλαιουχική Εταιρεία",
                "status": "Ενεργή",
                "incorporation_date": today - timedelta(days=3),
                "prefecture": "ΛΑΡΙΣΑΣ",
                "municipality": "Λάρισα",
                "city": "Λάρισα",
                "address": "Φαρσάλων 45",
                "postal_code": "41335",
                "email": "sales@greengrid-demo.gr",
                "website": "https://www.greengrid-demo.gr",
                "activities": [{"code": "43.21", "description": "Ηλεκτρικές εγκαταστάσεις"}],
                "persons": [
                    {
                        "personName": "Γεώργιος Δημητρίου",
                        "role": "Διαχειριστής",
                        "category": "Φυσικό Πρόσωπο",
                        "percentage": "50%",
                        "isRepresentativeAlone": True,
                    }
                ],
                "radar_name": "Ανανεώσιμες Πηγές Ενέργειας",
                "matched_codes": ["43.21"],
                "lead_status": "new",
                "is_favorite": True,
                "notes": "",
            },
            {
                "gemi_number": "999000104000",
                "vat_number": "999900104",
                "name": "KALYPSO HOSPITALITY & TRADING Ε.Ε.",
                "legal_type": "Ετερόρρυθμη Εταιρεία",
                "status": "Ενεργή",
                "incorporation_date": today - timedelta(days=4),
                "prefecture": "ΧΑΝΙΩΝ",
                "municipality": "Χανιά",
                "city": "Χανιά",
                "address": "Ακτή Τομπάζη 12",
                "postal_code": "73132",
                "email": "info@kalypso-demo.gr",
                "website": "https://www.kalypso-demo.gr",
                "activities": [{"code": "56.10", "description": "Δραστηριότητες υπηρεσιών εστιατορίων και κινητών μονάδων εστίασης"}],
                "persons": [
                    {
                        "personName": "Μαρία Κωνσταντίνου",
                        "role": "Ομόρρυθμος Εταίρος",
                        "category": "Φυσικό Πρόσωπο",
                        "percentage": "60%",
                        "isRepresentativeAlone": True,
                    }
                ],
                "radar_name": "Τουρισμός & Εστίαση",
                "matched_codes": ["56.10"],
                "lead_status": "viewed",
                "is_favorite": False,
                "notes": "Επισκόπηση προφίλ εταιρείας.",
            },
        ]

        for spec in companies_spec:
            comp, _ = Company.objects.update_or_create(
                gemi_number=spec["gemi_number"],
                defaults={
                    "vat_number": spec["vat_number"],
                    "name": spec["name"],
                    "trade_names": spec["name"],
                    "legal_type": spec["legal_type"],
                    "status": spec["status"],
                    "is_active": True,
                    "incorporation_date": spec["incorporation_date"],
                    "gemi_office": f"Υπηρεσία ΓΕΜΗ {spec['prefecture']}",
                    "prefecture": spec["prefecture"],
                    "municipality": spec["municipality"],
                    "city": spec["city"],
                    "address": spec["address"],
                    "postal_code": spec["postal_code"],
                    "email": spec["email"],
                    "website": spec["website"],
                    "activities": spec["activities"],
                    "raw_data": {"persons": spec["persons"]},
                },
            )

            for act in spec["activities"]:
                CompanyActivity.objects.update_or_create(
                    company=comp,
                    code=act["code"],
                    defaults={"description": act["description"], "activity_type": "Κύρια"},
                )

            # Create UserCompanyLead
            lead, _ = UserCompanyLead.objects.update_or_create(
                user=user,
                company=comp,
                defaults={
                    "status": spec["lead_status"],
                    "is_favorite": spec["is_favorite"],
                    "notes": spec["notes"],
                },
            )

            # Create RadarMatch
            radar = radar_objs[spec["radar_name"]]
            RadarMatch.objects.update_or_create(
                radar=radar,
                company=comp,
                defaults={
                    "lead": lead,
                    "matched_on": spec["incorporation_date"],
                    "matched_activity_codes": spec["matched_codes"],
                    "match_reason": {
                        "activity_codes": spec["matched_codes"],
                        "prefectures": [spec["prefecture"]],
                    },
                },
            )
