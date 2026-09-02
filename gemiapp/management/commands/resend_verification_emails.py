from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from gemiapp.services import send_verification_email_now


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

        sent = 0
        for user in users:
            if options["dry_run"]:
                self.stdout.write(f"[dry-run] θα σταλεί σε #{user.id} {user.email}")
                continue

            # Sent inline, not queued: this command is run by hand to unblock specific
            # accounts, so a failure has to surface here rather than in a worker log.
            try:
                send_verification_email_now(user.pk)
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"Απέτυχε για {user.email}: {exc}"))
                continue

            sent += 1
            self.stdout.write(self.style.SUCCESS(f"Εστάλη σε #{user.id} {user.email}"))

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"[dry-run] υποψήφιοι παραλήπτες: {len(users)}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Ολοκληρώθηκε. Εστάλησαν {sent} από {len(users)}."))
