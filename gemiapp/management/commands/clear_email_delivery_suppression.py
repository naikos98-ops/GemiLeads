"""Operator-only: allow delivery again to an address that was suppressed after a hard bounce or
block -- e.g. the customer created the mailbox, or Brevo's own blocklist entry was removed.

Never automatic (a login proves nothing about the mailbox). The row is kept with active=False
and cleared_at, so the history stays. If the address bounces again, the next Brevo event
re-suppresses it. Clearing here does NOT remove the address from Brevo's own blocklist: if Brevo
still blocks it, remove it in the Brevo dashboard as well, or the next send is simply blocked again.

A user who moved to a different address needs nothing here: suppression is per address.
"""

from django.core.management.base import BaseCommand, CommandError

from gemiapp.email_deliverability import clear_email_delivery_suppression, normalize_email
from gemiapp.models import EmailDeliverySuppression


class Command(BaseCommand):
    help = "Αίρει το delivery suppression μίας διεύθυνσης email (ρητή ενέργεια operator)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Η διεύθυνση που θα ξαναγίνει παραδόσιμη.")
        parser.add_argument("--dry-run", action="store_true", help="Δείξε την κατάσταση χωρίς αλλαγή.")

    def handle(self, *args, **options):
        email = normalize_email(options["email"])
        if not email:
            raise CommandError("Κενή διεύθυνση email.")

        row = EmailDeliverySuppression.objects.filter(email=email).first()
        if row is None or not row.active:
            state = "none" if row is None else f"already cleared at {row.cleared_at:%Y-%m-%d %H:%M}"
            self.stdout.write(f"No active delivery suppression ({state}). Nothing to do.")
            return

        self.stdout.write(
            f"Active suppression: reason={row.reason} first_seen={row.first_seen_at:%Y-%m-%d %H:%M} "
            f"last_seen={row.last_seen_at:%Y-%m-%d %H:%M}"
        )
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing changed."))
            return

        clear_email_delivery_suppression(email)
        self.stdout.write(self.style.SUCCESS(
            "Cleared. Product emails to this address are allowed again. "
            "Check Brevo's own blocklist too."
        ))
