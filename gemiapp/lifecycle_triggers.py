"""Glue between the existing writers of UserSubscription and the two new records: SubscriptionPeriod
history and lifecycle-email reservations.

Called from inside the existing transactions (Stripe webhook handlers, Superadmin complimentary
grant/revoke). Everything here runs in its own savepoint and never raises: a failure to write history
or to reserve an email is logged and must not roll back -- or fail and retry -- the entitlement change
that the caller is making. Emails are only *reserved* here; a worker sends them after commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction

from .lifecycle import enqueue
from .subscription_history import TIER_RANK, sync_complimentary_period, sync_stripe_period

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"canceled", "incomplete_expired"})


@dataclass(frozen=True)
class Before:
    paid: bool
    tier: str
    active_until: object

    @classmethod
    def of(cls, sub):
        return cls(paid=sub.has_active_paid_subscription, tier=sub.tier, active_until=sub.active_until)


def after_stripe_change(sub, before: Before, *, at=None, subscription_payload=None, terminal=False):
    change = None
    try:
        with transaction.atomic():
            change = sync_stripe_period(sub, at=at, subscription_payload=subscription_payload)
    except Exception:
        logger.exception("Could not record subscription history for subscription %s", sub.pk)

    user = sub.user
    # E4: first paid tier, or a strictly higher one. Renewals and downgrades never qualify.
    if sub.has_active_paid_subscription and (
        not before.paid or TIER_RANK.get(sub.tier, 0) > TIER_RANK.get(before.tier, 0)
    ):
        enqueue("e4", user, f"e4:{user.pk}:{sub.tier}", {"tier": sub.tier})
    # E8: a cancellation at period end was scheduled (button or Stripe Portal: both arrive here).
    if sub.active_until and sub.active_until != before.active_until:
        enqueue("e8", user, f"e8:{sub.stripe_subscription_id}:{sub.active_until.date().isoformat()}", {})
    # E10: the paid period really ended (deleted / terminal status), not a past_due that may recover.
    closed = change.closed if change else None
    if (terminal or sub.status in TERMINAL_STATUSES) and closed is not None \
            and closed.end_reason not in ("upgraded", "downgraded", "account_deleted") and not sub.has_entitlement:
        enqueue("e10", user, f"e10:{closed.pk}", {"period_id": closed.pk})
    return change


def after_complimentary_change(sub):
    try:
        with transaction.atomic():
            return sync_complimentary_period(sub)
    except Exception:
        logger.exception("Could not record complimentary history for subscription %s", sub.pk)
        return None
