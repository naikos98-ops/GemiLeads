"""Rebuild SubscriptionPeriod history from the stored Stripe webhook events (kept since 2026-08-28).

    python manage.py backfill_subscription_periods            # dry run: prints what would be written
    python manage.py backfill_subscription_periods --apply    # only after the Founder approved the dry run

Replays, in received order, every stored customer.subscription.created/updated/deleted payload (they carry
the full subscription: status and price) and maps each Stripe subscription to its user through
checkout.session.completed (client_reference_id) or the current UserSubscription row. The same rule as
live: an open period while status is "active" on a paid tier; a tier change closes and opens; anything
else closes with the Stripe cancellation reason (or unknown). Complimentary access is not in these events
and is not backfilled. Refuses --apply for a user who already has Stripe periods (no duplicates).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from gemiapp.billing import _extract_tier_from_items, _stripe_timestamp_to_datetime
from gemiapp.models import StripeWebhookEvent, SubscriptionPeriod, UserSubscription
from gemiapp.subscription_history import TIER_RANK, stripe_end_reason, user_ref_for

SUBSCRIPTION_EVENTS = ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted")


@dataclass
class PlannedPeriod:
    user_id: int
    tier: str
    started_at: object
    stripe_subscription_id: str
    ended_at: object = None
    end_reason: str = ""


@dataclass
class Plan:
    periods: list = field(default_factory=list)
    unmapped: set = field(default_factory=set)
    unreadable: int = 0


def build_plan() -> Plan:
    plan = Plan()
    owner = {}
    for event in StripeWebhookEvent.objects.filter(event_type="checkout.session.completed").order_by("received_at"):
        obj = (event.payload or {}).get("data", {}).get("object", {})
        if obj.get("subscription") and obj.get("client_reference_id"):
            owner[obj["subscription"]] = int(obj["client_reference_id"])
    for sub in UserSubscription.objects.exclude(stripe_subscription_id=""):
        owner.setdefault(sub.stripe_subscription_id, sub.user_id)

    open_by_sub = {}
    for event in StripeWebhookEvent.objects.filter(event_type__in=SUBSCRIPTION_EVENTS).order_by("received_at", "id"):
        payload = event.payload or {}
        obj = payload.get("data", {}).get("object", {})
        sub_id = obj.get("id")
        if not sub_id:
            plan.unreadable += 1
            continue
        user_id = owner.get(sub_id)
        if user_id is None:
            plan.unmapped.add(sub_id)
            continue
        try:
            tier = _extract_tier_from_items(obj.get("items", {}).get("data", []))
        except (KeyError, TypeError):
            plan.unreadable += 1
            continue
        at = _stripe_timestamp_to_datetime(payload.get("created")) or event.received_at
        status = obj.get("status")
        deleted = event.event_type == "customer.subscription.deleted"
        desired = tier if (tier and status == "active" and not deleted) else None
        current = open_by_sub.get(sub_id)
        if current and current.tier == desired:
            continue
        if current:
            current.ended_at = at
            current.end_reason = ("upgraded" if TIER_RANK.get(desired, 0) > TIER_RANK.get(current.tier, 0)
                                  else "downgraded") if desired else stripe_end_reason(obj, status)
            open_by_sub.pop(sub_id)
        if desired:
            period = PlannedPeriod(user_id=user_id, tier=desired, started_at=at, stripe_subscription_id=sub_id)
            plan.periods.append(period)
            open_by_sub[sub_id] = period
    return plan


class Command(BaseCommand):
    help = "Backfill SubscriptionPeriod from stored Stripe webhook events (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the periods (after approval).")

    def handle(self, *args, **opts):
        plan = build_plan()
        by_user = defaultdict(list)
        for period in plan.periods:
            by_user[period.user_id].append(period)
        for user_id, periods in sorted(by_user.items()):
            for p in periods:
                end = f"{p.ended_at:%Y-%m-%d %H:%M} ({p.end_reason})" if p.ended_at else "ανοιχτή"
                self.stdout.write(f"user {user_id} · {p.tier} · {p.started_at:%Y-%m-%d %H:%M} → {end} · {p.stripe_subscription_id}")
        self.stdout.write(f"periods={len(plan.periods)} users={len(by_user)} unmapped_subscriptions={len(plan.unmapped)} "
                          f"unreadable_events={plan.unreadable}")
        if not opts["apply"]:
            self.stdout.write("Dry run: τίποτα δεν γράφτηκε. Ξανά με --apply μετά από έγκριση.")
            return
        written = skipped = 0
        for user_id, periods in sorted(by_user.items()):
            user = User.objects.filter(pk=user_id).first()
            if user is None or SubscriptionPeriod.objects.filter(user=user, source="stripe").exists():
                skipped += 1
                continue
            ref = user_ref_for(user)
            with transaction.atomic():
                for p in periods:
                    SubscriptionPeriod.objects.create(
                        user=user, user_ref=ref, source="stripe", tier=p.tier, started_at=p.started_at,
                        ended_at=p.ended_at, end_reason=p.end_reason, stripe_subscription_id=p.stripe_subscription_id,
                    )
                    written += 1
        self.stdout.write(f"applied: periods_written={written} users_skipped(existing/deleted)={skipped}")
