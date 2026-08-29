"""Re-queue cold-outreach emails that were marked "sent" but never actually left Brevo.

Before OUTREACH_DAILY_SEND_CAP existed, a batch past Brevo's daily quota still got a 250 OK
from the SMTP relay and was then silently dropped -- so the CompanyOutreach row reads "sent"
while the company was never contacted. uncontacted_companies_qs() excludes any company with a
row of any status, so these companies would never be tried again.

This command finds "sent" rows with NO "delivered"/"opened"/"click" engagement event (i.e. no
evidence the mail was ever handed on) and flips them back to "pending" so the normal worker
picks them up -- respecting the daily cap this time. A hard/soft bounce or a "request" event
counts as evidence the send really happened, so those rows are left alone.

    python manage.py requeue_dropped_outreach --dry-run
    python manage.py requeue_dropped_outreach --before 2026-08-28
    python manage.py requeue_dropped_outreach --apply
"""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from gemiapp.models import CompanyOutreach, EmailEngagementEvent

# Any of these, for a row's tag, means Brevo did accept the message -- not a silent drop.
DELIVERY_EVIDENCE = {"delivered", "opened", "unique_opened", "click", "request",
                     "hard_bounce", "hardBounce", "soft_bounce", "softBounce",
                     "blocked", "deferred", "spam", "unsubscribed"}


class Command(BaseCommand):
    help = 'Ξαναβάζει σε ουρά τα outreach email που φαίνονται "sent" αλλά ποτέ δεν έφυγαν από το Brevo.'

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Εκτέλεση των αλλαγών (χωρίς αυτό, dry-run).")
        parser.add_argument("--dry-run", action="store_true", help="Ρητό dry-run (default).")
        parser.add_argument("--before", help="Μόνο rows με created_at πριν από αυτή τη μέρα (YYYY-MM-DD).")
        parser.add_argument("--limit", type=int, default=0, help="Ανώτατο πλήθος rows (0 = χωρίς όριο).")

    def handle(self, *args, **opts):
        rows = CompanyOutreach.objects.filter(status="sent").select_related("company").order_by("created_at")

        if opts["before"]:
            try:
                cutoff = datetime.strptime(opts["before"], "%Y-%m-%d")
            except ValueError:
                raise CommandError("Η --before πρέπει να είναι YYYY-MM-DD.")
            cutoff = timezone.make_aware(cutoff)
            rows = rows.filter(created_at__lt=cutoff)

        # Tags that DO have delivery evidence -- one query, not one per row.
        tags_with_evidence = set(
            EmailEngagementEvent.objects.filter(event_type__in=DELIVERY_EVIDENCE)
            .values_list("tag", flat=True).distinct()
        )

        dropped = [r for r in rows if f"outreach:{r.company_id}" not in tags_with_evidence]
        if opts["limit"]:
            dropped = dropped[: opts["limit"]]

        total_sent = rows.count()
        self.stdout.write(f'"sent" rows στην εμβέλεια: {total_sent}')
        self.stdout.write(f"Χωρίς καμία ένδειξη παράδοσης (σιωπηλά dropped): {len(dropped)}")
        for r in dropped[:20]:
            self.stdout.write(f"  · {r.company.name} ({r.sent_to}) — {r.created_at:%Y-%m-%d}")
        if len(dropped) > 20:
            self.stdout.write(f"  … και άλλα {len(dropped) - 20}")

        if not dropped:
            self.stdout.write(self.style.SUCCESS("Τίποτα να ξαναμπεί σε ουρά."))
            return

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING("dry-run: καμία αλλαγή. Τρέξε ξανά με --apply."))
            return

        company_ids = [r.company_id for r in dropped]
        updated = CompanyOutreach.objects.filter(
            company_id__in=company_ids, status="sent"
        ).update(
            status="pending",
            error_message="Ξαναμπήκε σε ουρά: είχε μαρκαριστεί sent χωρίς παράδοση από Brevo.",
        )
        self.stdout.write(self.style.SUCCESS(f"{updated} rows → pending."))

        from django_q.tasks import async_task

        async_task("gemiapp.tasks.send_company_outreach_task", company_ids)
        self.stdout.write(
            "Έγινε enqueue το send_company_outreach_task. Θα στείλει μέχρι το daily cap "
            "και θα προγραμματίσει το υπόλοιπο για +24h."
        )
