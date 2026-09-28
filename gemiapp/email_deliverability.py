"""Email deliverability suppression: never attempt SMTP delivery to an address Brevo has
already reported as undeliverable.

Why this exists: an SMTP relay *accepts* a message before it knows whether the mailbox exists.
Brevo reports a hard bounce or a block later, through the webhook (gemiapp.email_tracking). Until
this module, nothing on the product side read those reports, so an address that bounced its
verification email -- and whose account was then activated by hand -- received every daily and
intraday digest, each one recorded as "sent", each one blocked by Brevo. Repeated sends to a known
dead address are exactly what degrades the sending domain's reputation.

This is deliverability, not consent. OutreachSuppression stays the cold-outreach list (it also
holds people who only opted out of prospecting); an outreach opt-out must never stop a password
reset. The suppression is keyed by the normalised address, never by User, so correcting an
address makes the user deliverable again.

Every product send path asks ``is_email_delivery_suppressed`` -- one helper, one query:
  - digests (daily, intraday, manual "yesterday") through services.digest_skip_reason
  - account verification (services.send_verification_email_now)
  - password reset (views.DeliverablePasswordResetForm and the allauth adapter)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from django.db import transaction
from django.utils import timezone

from .models import DigestDelivery, EmailDeliverySuppression

logger = logging.getLogger(__name__)

# Brevo's SMTP relay emits snake_case, its transactional API camelCase -- same event.
# Soft bounces are deliberately absent: a full mailbox or greylisting is temporary.
SUPPRESSING_EVENTS = {
    "hardBounce": EmailDeliverySuppression.Reason.HARD_BOUNCE,
    "hard_bounce": EmailDeliverySuppression.Reason.HARD_BOUNCE,
    "blocked": EmailDeliverySuppression.Reason.BLOCKED,
}

# The deterministic reason written to DigestDelivery.error_message and returned by
# services.digest_skip_reason when a send is suppressed before SMTP.
DELIVERY_SUPPRESSED_REASON = "Email delivery suppressed after hard bounce/blocked event"

_DIGEST_FREQUENCIES = frozenset(key for key, _ in DigestDelivery.FREQUENCIES)


def normalize_email(email) -> str:
    return (email or "").strip().lower()


def is_email_delivery_suppressed(email) -> bool:
    """True when ``email`` has an active deliverability suppression. The one check every product
    send path uses; an empty address is never suppressed (callers reject it on their own)."""
    normalized = normalize_email(email)
    if not normalized:
        return False
    return EmailDeliverySuppression.objects.filter(email=normalized, active=True).exists()


def record_email_delivery_suppression(email, event_type, *, seen_at=None) -> str:
    """Create or refresh the suppression for ``email`` from one Brevo event.

    Returns what happened: "created", "updated", "reactivated", "unchanged", "stale" (an event
    older than an operator's clear -- a replayed history must not undo the clear) or "ignored"
    (not a suppressing event / no address). Idempotent: replaying the same event is "unchanged".
    ``seen_at`` defaults to now; the backfill passes the event's own received_at.
    """
    reason = SUPPRESSING_EVENTS.get(event_type)
    normalized = normalize_email(email)
    if reason is None or not normalized:
        return "ignored"
    seen_at = seen_at or timezone.now()

    with transaction.atomic():
        row = EmailDeliverySuppression.objects.select_for_update().filter(email=normalized).first()
        if row is None:
            EmailDeliverySuppression.objects.create(
                email=normalized, reason=reason, first_seen_at=seen_at, last_seen_at=seen_at,
            )
            return "created"

        if not row.active:
            if row.cleared_at is not None and seen_at <= row.cleared_at:
                return "stale"
            row.active = True
            row.cleared_at = None
            row.reason = reason
            row.last_seen_at = max(row.last_seen_at, seen_at)
            row.first_seen_at = min(row.first_seen_at, seen_at)
            row.save(update_fields=["active", "cleared_at", "reason", "last_seen_at", "first_seen_at"])
            return "reactivated"

        changed = []
        if seen_at > row.last_seen_at:
            row.last_seen_at = seen_at
            row.reason = reason  # the most recent event names the current reason
            changed += ["last_seen_at", "reason"]
        if seen_at < row.first_seen_at:
            row.first_seen_at = seen_at
            changed.append("first_seen_at")
        if not changed:
            return "unchanged"
        row.save(update_fields=changed)
        return "updated"


def clear_email_delivery_suppression(email, *, now=None) -> bool:
    """Operator action: allow delivery to ``email`` again (e.g. the mailbox was created or the
    customer confirmed a typo fix). Keeps the row as history. Returns False when there was no
    active suppression. A later hard bounce/blocked event re-suppresses the address."""
    normalized = normalize_email(email)
    if not normalized:
        return False
    updated = EmailDeliverySuppression.objects.filter(email=normalized, active=True).update(
        active=False, cleared_at=now or timezone.now(),
    )
    return bool(updated)


@dataclass(frozen=True)
class DigestTag:
    user_id: int
    digest_date: date
    frequency: str


def parse_digest_tag(tag) -> DigestTag | None:
    """Parse ``digest:<user_id>:<YYYY-MM-DD>:<frequency>`` (services.digest_email_tag).
    Anything else -- wrong prefix, extra parts, non-integer id, invalid date, unknown frequency --
    is None, never an exception."""
    if not isinstance(tag, str):
        return None
    parts = tag.strip().split(":")
    if len(parts) != 4 or parts[0] != "digest":
        return None
    raw_user_id, raw_date, frequency = parts[1], parts[2], parts[3]
    if not raw_user_id.isdigit() or frequency not in _DIGEST_FREQUENCIES:
        return None
    try:
        digest_date = date.fromisoformat(raw_date)
    except ValueError:
        return None
    user_id = int(raw_user_id)
    if user_id <= 0:
        return None
    return DigestTag(user_id=user_id, digest_date=digest_date, frequency=frequency)


def reconcile_digest_delivery(tag, email, event_type) -> bool:
    """A digest SMTP accepted earlier was later reported hard-bounced/blocked: turn the matching
    DigestDelivery from "sent" into "failed". Returns True when a row changed.

    Conservative on purpose: only a well-formed digest tag, only the row that tag names, only
    while it still says "sent", and only when the bounced address is that user's current address
    -- a tag pointing at another user's delivery changes nothing.
    """
    reason = SUPPRESSING_EVENTS.get(event_type)
    parsed = parse_digest_tag(tag)
    normalized = normalize_email(email)
    if reason is None or parsed is None or not normalized:
        return False

    with transaction.atomic():
        delivery = (
            DigestDelivery.objects.select_for_update(of=("self",))
            .select_related("user")
            .filter(user_id=parsed.user_id, digest_date=parsed.digest_date, frequency=parsed.frequency)
            .first()
        )
        if delivery is None or delivery.status != "sent":
            return False
        if normalize_email(delivery.user.email) != normalized:
            logger.warning(
                "Brevo %s for digest tag of user %s does not match that user's address; "
                "DigestDelivery %s left unchanged.", event_type, parsed.user_id, delivery.pk,
            )
            return False
        delivery.status = "failed"
        delivery.error_message = f"Brevo reported {reason} after SMTP acceptance"
        delivery.save(update_fields=["status", "error_message"])
    return True
