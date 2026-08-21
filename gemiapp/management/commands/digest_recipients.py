from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from gemiapp.services import digest_skip_reason


class Command(BaseCommand):
    help = (
        "Δείχνει ποιοι χρήστες θα λάβουν ένα digest και, για όσους δεν θα λάβουν, τον ακριβή λόγο. "
        "Δεν στέλνει τίποτα."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--frequency",
            default="intraday",
            choices=["daily", "intraday"],
            help="Ποιο digest να ελεγχθεί (προεπιλογή: intraday).",
        )

    def handle(self, *args, **options):
        frequency = options["frequency"]
        users = User.objects.select_related("subscription", "digest_preference").order_by("id")

        eligible = []
        blocked = []
        for user in users:
            reason = digest_skip_reason(user, frequency)
            (blocked if reason else eligible).append((user, reason))

        self.stdout.write(self.style.MIGRATE_HEADING(f"Digest: {frequency}"))
        self.stdout.write("")

        self.stdout.write(self.style.SUCCESS(f"ΘΑ ΛΑΒΟΥΝ ({len(eligible)}):"))
        for user, _ in eligible:
            tier = getattr(getattr(user, "subscription", None), "effective_tier", "-")
            self.stdout.write(f"  #{user.id:<5} {user.email or '(χωρίς email)':<40} tier={tier}")
        if not eligible:
            self.stdout.write("  (κανένας)")

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"ΔΕΝ ΘΑ ΛΑΒΟΥΝ ({len(blocked)}):"))
        for user, reason in blocked:
            self.stdout.write(f"  #{user.id:<5} {user.email or '(χωρίς email)':<40} {reason}")
        if not blocked:
            self.stdout.write("  (κανένας)")

        self.stdout.write("")
        self.stdout.write(f"Σύνολο χρηστών: {users.count()}")
