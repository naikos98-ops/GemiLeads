"""SubscriptionPeriod: the append-only history of paid access (gemiapp.models.SubscriptionPeriod).

One entry point per source, both state-based and therefore idempotent: ``sync_stripe_period`` and
``sync_complimentary_period`` look at what the UserSubscription projection says *now*, compare it with
the open period, and close/open rows only when the two disagree. Calling either twice with the same
state writes nothing the second time, so a duplicate webhook cannot create a duplicate row.

Called only from the existing Stripe webhook handlers (gemiapp.billing), the Superadmin complimentary
grant/revoke (gemiapp.superadmin.services) and the daily lifecycle job (complimentary expiry is a
passage of time, not an event). Nothing here touches entitlement.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from .models import SubscriptionPeriod, UserLifecycleProfile, UserSubscription

TIER_RANK = {"pro": 1, "business": 2, "enterprise": 3, "custom": 4}

# Stripe's cancellation_details.reason -> our end_reason. Anything else (including payment_disputed,
# which is a dispute, not a refund) is recorded as unknown rather than guessed.
STRIPE_CANCELLATION_REASONS = {
    "cancellation_requested": "canceled",
    "payment_failed": "payment_failed",
}
PAYMENT_PROBLEM_STATUSES = {"past_due", "unpaid", "incomplete", "incomplete_expired"}


@dataclass(frozen=True)
class PeriodChange:
    closed: SubscriptionPeriod | None = None
    opened: SubscriptionPeriod | None = None


def user_ref_for(user) -> str:
    profile, _ = UserLifecycleProfile.objects.get_or_create(user=user)
    return str(profile.ref)


def _tier_change_reason(old_tier, new_tier):
    return "upgraded" if TIER_RANK.get(new_tier, 0) > TIER_RANK.get(old_tier, 0) else "downgraded"


def _apply(user, source, desired_tier, at, end_reason, stripe_subscription_id=""):
    ref = user_ref_for(user)
    open_period = (
        SubscriptionPeriod.objects.select_for_update()
        .filter(user_ref=ref, source=source, ended_at__isnull=True).first()
    )
    if open_period and open_period.tier == desired_tier:
        return PeriodChange()
    closed = opened = None
    if open_period:
        open_period.ended_at = at
        open_period.end_reason = _tier_change_reason(open_period.tier, desired_tier) if desired_tier else end_reason
        open_period.save(update_fields=["ended_at", "end_reason"])
        closed = open_period
    if desired_tier:
        opened = SubscriptionPeriod.objects.create(
            user=user, user_ref=ref, source=source, tier=desired_tier, started_at=at,
            stripe_subscription_id=stripe_subscription_id or "",
        )
    return PeriodChange(closed=closed, opened=opened)


def stripe_end_reason(subscription_payload: dict | None, status: str | None) -> str:
    details = (subscription_payload or {}).get("cancellation_details") or {}
    reason = STRIPE_CANCELLATION_REASONS.get(details.get("reason") or "")
    if reason:
        return reason
    if status in PAYMENT_PROBLEM_STATUSES:
        return "payment_failed"
    return "unknown"


def sync_stripe_period(sub: UserSubscription, *, at=None, subscription_payload=None) -> PeriodChange:
    """Make the open Stripe period agree with ``sub`` (call inside the handler's transaction).

    Open paid period <=> ``sub.has_active_paid_subscription`` (paid tier, status active) -- the same
    rule the product uses for paid access, so history and entitlement never disagree about what
    «paid» meant.
    """
    desired = sub.tier if sub.has_active_paid_subscription else None
    reason = stripe_end_reason(subscription_payload, sub.status)
    return _apply(sub.user, "stripe", desired, at or timezone.now(), reason, sub.stripe_subscription_id)


def sync_complimentary_period(sub: UserSubscription, *, at=None) -> PeriodChange:
    desired = sub.complimentary_tier if sub.has_valid_complimentary_access else None
    now = at or timezone.now()
    expired = sub.complimentary_until is not None and sub.complimentary_until <= now
    return _apply(sub.user, "complimentary", desired, now, "complimentary_expired" if expired else "canceled")


def sweep_expired_complimentary(*, now=None, dry_run=False) -> list[SubscriptionPeriod]:
    """Close complimentary periods whose access has lapsed by time alone (no event marks it)."""
    now = now or timezone.now()
    lapsed = []
    for period in SubscriptionPeriod.objects.filter(source="complimentary", ended_at__isnull=True, user__isnull=False):
        sub = getattr(period.user, "subscription", None)
        still_valid = sub is not None and sub.complimentary_tier == period.tier and (
            sub.complimentary_until is None or sub.complimentary_until >= now)
        if still_valid:
            continue
        lapsed.append(period)
        if not dry_run:
            period.ended_at = sub.complimentary_until if sub and sub.complimentary_until and sub.complimentary_until <= now else now
            period.end_reason = "complimentary_expired" if sub and sub.complimentary_until else "canceled"
            period.save(update_fields=["ended_at", "end_reason"])
    return lapsed


def months_paid(user) -> float:
    """Months of paid (Stripe) access, open periods counted up to now. 30.44 days = one month."""
    now = timezone.now()
    total = 0.0
    for period in SubscriptionPeriod.objects.filter(user=user, source="stripe"):
        total += ((period.ended_at or now) - period.started_at).total_seconds()
    return round(total / (30.44 * 86400), 2)


def is_former_paid(user) -> bool:
    """At least one closed paid (Stripe) period and no open period of any source."""
    periods = SubscriptionPeriod.objects.filter(user=user)
    if periods.filter(ended_at__isnull=True).exists():
        return False
    return periods.filter(source="stripe", ended_at__isnull=False).exists()


def close_all_for_deleted_account(user, *, at=None) -> int:
    """Close every open period with account_deleted (used by the deletion flow, PR 2)."""
    at = at or timezone.now()
    return SubscriptionPeriod.objects.filter(user=user, ended_at__isnull=True).update(
        ended_at=at, end_reason="account_deleted",
    )
