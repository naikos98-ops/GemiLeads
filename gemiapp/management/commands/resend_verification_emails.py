from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode


class Command(BaseCommand):
    help = (
        "Στέλνει ξανά σύνδεσμο επιβεβαίωσης σε λογαριασμούς που έμειναν ανενεργοί. "
        "Χωρίς --email αφορά ΟΛΟΥΣ τους ανενεργούς, γι' αυτό τρέξε πρώτα με --dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            action="append",
            dest="emails",
            help="Περιόρισε σε συγκεκριμένο email. Επαναλαμβανόμενο για πολλαπλούς παραλήπτες.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Δείξε ποιοι θα λάβουν, χωρίς να σταλεί κανένα email.",
        )

    def handle(self, *args, **options):
        users = User.objects.filter(is_active=False).order_by("id")
        if options["emails"]:
            wanted = [address.strip().lower() for address in options["emails"]]
            users = users.filter(email__iregex=r"^(" + "|".join(wanted) + r")$")

        users = [user for user in users if user.email]
        if not users:
            self.stdout.write(self.style.WARNING("Δεν βρέθηκε ανενεργός λογαριασμός με email."))
            return

        base_url = settings.BASE_URL.rstrip("/")
        sent = 0
        for user in users:
            if options["dry_run"]:
                self.stdout.write(f"[dry-run] θα σταλεί σε #{user.id} {user.email}")
                continue

            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            verify_url = f"{base_url}{reverse('verify_email', kwargs={'uidb64': uid, 'token': token})}"
            context = {"verify_url": verify_url, "user": user}

            try:
                send_mail(
                    "Επιβεβαίωση email στο Gemi Leads",
                    render_to_string("emails/verification.txt", context),
                    settings.DEFAULT_FROM_EMAIL,
                    [user.email],
                    html_message=render_to_string("emails/verification.html", context),
                )
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"Απέτυχε για {user.email}: {exc}"))
                continue

            sent += 1
            self.stdout.write(self.style.SUCCESS(f"Εστάλη σε #{user.id} {user.email}"))

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"[dry-run] υποψήφιοι παραλήπτες: {len(users)}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Ολοκληρώθηκε. Εστάλησαν {sent} από {len(users)}."))
