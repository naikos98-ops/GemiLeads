"""Suppress addresses that hard-bounced before the webhook started suppressing them.

EmailEngagementEvent has been recording hardBounce/blocked events since the Brevo webhook
went in, but nothing acted on them, so every one of those addresses is still eligible for
another outreach batch. This walks the existing log once and writes the suppression rows
that the webhook would write today. Safe to re-run -- suppression is unique on email and
existing rows are left alone.
"""

from django.core.management.base import BaseCommand

from gemiapp.email_tracking import _SUPPRESSING_EVENTS
from gemiapp.models import EmailEngagementEvent, OutreachSuppression


class Command(BaseCommand):
    help = "Βάζει στη λίστα απεγγραφών όσες διευθύνσεις έχουν ήδη κάνει hard bounce."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Δείχνει τι θα γινόταν χωρίς να γράψει τίποτα.",
        )

    def handle(self, *args, **options):
        bounced = set(
            EmailEngagementEvent.objects.filter(event_type__in=_SUPPRESSING_EVENTS)
            .exclude(email="")
            .values_list("email", flat=True)
        )
        normalized = {OutreachSuppression.normalize(e) for e in bounced}
        normalized.discard("")

        already = set(
            OutreachSuppression.objects.filter(email__in=normalized).values_list("email", flat=True)
        )
        missing = sorted(normalized - already)

        self.stdout.write(f"Hard-bounced διευθύνσεις στο log: {len(normalized)}")
        self.stdout.write(f"Ήδη στη λίστα απεγγραφών:        {len(already)}")
        self.stdout.write(f"Προς προσθήκη:                   {len(missing)}")

        if not missing:
            self.stdout.write(self.style.SUCCESS("Δεν χρειάζεται καμία αλλαγή."))
            return

        if options["dry_run"]:
            for email in missing[:20]:
                self.stdout.write(f"  θα προστεθεί: {email}")
            if len(missing) > 20:
                self.stdout.write(f"  ... και άλλες {len(missing) - 20}")
            self.stdout.write(self.style.WARNING("Dry run — δεν γράφτηκε τίποτα."))
            return

        OutreachSuppression.objects.bulk_create(
            [OutreachSuppression(email=e) for e in missing], ignore_conflicts=True
        )
        self.stdout.write(self.style.SUCCESS(f"Προστέθηκαν {len(missing)} διευθύνσεις."))
