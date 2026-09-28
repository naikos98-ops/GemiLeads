"""Populate EmailDeliverySuppression from the hard-bounce/blocked events already in
EmailEngagementEvent.

The webhook only started writing the deliverability list with this release; every address Brevo
reported before that is still attempted by the digests. This replays the existing log, oldest
first, through the same writer the webhook uses (record_email_delivery_suppression), with each
event's own received_at as its time -- so first/last seen are the real ones, a re-run changes
nothing, and an event older than an operator's clear never undoes that clear.

DRY RUN BY DEFAULT: the replay runs inside a transaction that is rolled back, so the counters are
exactly what --apply would produce. Only --apply writes. Output is counters only (no addresses).
It does not touch OutreachSuppression (backfill_bounce_suppressions owns that), DigestDelivery,
users, preferences, subscriptions or organizations, and it sends nothing.
"""

from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from gemiapp.email_deliverability import SUPPRESSING_EVENTS, record_email_delivery_suppression
from gemiapp.models import EmailDeliverySuppression, EmailEngagementEvent

OUTCOMES = ("created", "updated", "reactivated", "unchanged", "stale", "ignored")


class Command(BaseCommand):
    help = (
        "Γεμίζει το EmailDeliverySuppression από τα ιστορικά hard bounce/blocked events. "
        "Dry run εξ ορισμού· γράφει μόνο με --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Γράψε πραγματικά (χωρίς αυτό: dry run).")

    def handle(self, *args, **options):
        apply = options["apply"]
        counts = Counter()

        with transaction.atomic():
            events = (
                EmailEngagementEvent.objects.filter(event_type__in=SUPPRESSING_EVENTS)
                .exclude(email="")
                .order_by("received_at", "id")
                .values_list("email", "event_type", "received_at")
            )
            for email, event_type, received_at in events.iterator():
                counts["events_examined"] += 1
                counts[record_email_delivery_suppression(email, event_type, seen_at=received_at)] += 1
            counts["active_suppressions_after"] = EmailDeliverySuppression.objects.filter(active=True).count()
            if not apply:
                transaction.set_rollback(True)

        mode = "APPLY" if apply else "DRY RUN (rolled back, nothing written)"
        self.stdout.write(f"mode={mode}")
        for key in ("events_examined", *OUTCOMES, "active_suppressions_after"):
            self.stdout.write(f"{key}={counts[key]}")
        if not apply:
            self.stdout.write(self.style.WARNING("Dry run — ξανατρέξε με --apply για εγγραφή."))
