"""Account deletion: self-service from Settings and the same flow as a Superadmin action.

Order (brief 2026-11-06, PR 2):
  1. An active Stripe subscription is cancelled immediately, without an automatic refund. If Stripe
     refuses, nothing is deleted: a deleted account that keeps being charged is the one outcome that
     must never happen.
  2. Open SubscriptionPeriods close with ``account_deleted``; deleting the user then sets their ``user``
     to NULL (SET_NULL), the anonymous ``user_ref`` stays.
  3. Personal data is scrubbed from this customer's StripeWebhookEvent payloads (ids, amounts, dates
     and statuses stay -- they are the transaction record) and the Brevo engagement events of the
     address are deleted.
  4. The User is deleted. CASCADE removes: UserSubscription, DigestPreference, DigestDelivery,
     CustomerRadar (+ RadarMatch), UserCompanyLead (+ notes), SupportMessage, UserLifecycleProfile,
     OrganizationMember, allauth EmailAddress / SocialAccount, admin LogEntry. SET_NULL keeps, without the
     person: SubscriptionPeriod, LifecycleEmail, ExitSurvey, AdminAuditLog (admin side). An organization
     the user owned alone is deleted with them; one with other members blocks self-service deletion.
     Not linked to the user and kept: EmailDeliverySuppression / OutreachSuppression (do-not-mail lists).
  5. E15 is reserved (no address stored) and the address travels only in the worker task's arguments.
  6. The caller logs the user out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import stripe
from django.db import transaction
from django.utils import timezone

from .lifecycle import lifecycle_enabled
from .models import (
    AdminAuditLog,
    EmailEngagementEvent,
    ExitSurvey,
    LifecycleEmail,
    Organization,
    OrganizationMember,
    StripeWebhookEvent,
    UserSubscription,
)
from .subscription_history import close_all_for_deleted_account, user_ref_for

logger = logging.getLogger(__name__)

TERMINAL = {"canceled", "incomplete_expired"}

# Keys that hold a person's details in Stripe objects. Their values are replaced, never the ids/amounts.
PERSONAL_KEYS = frozenset({
    "customer_details", "customer_email", "customer_name", "customer_address", "customer_phone",
    "customer_shipping", "customer_tax_ids", "billing_details", "receipt_email", "shipping", "shipping_details",
    "email", "name", "phone", "address", "tax_ids", "individual_name",
})
REDACTED = "[redacted]"


class AccountDeletionRefused(Exception):
    """The account cannot be deleted right now; the message is shown to the user as is."""


@dataclass
class DeletionResult:
    user_ref: str
    stripe_cancelled: bool
    periods_closed: int
    payloads_scrubbed: int
    engagement_deleted: int
    organizations_deleted: int


def _redact(value):
    if isinstance(value, dict):
        return {k: (REDACTED if k in PERSONAL_KEYS and v not in (None, "", [], {}) else _redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _mentions(payload, customer_ids, emails) -> bool:
    import json

    text = json.dumps(payload, default=str).lower()
    return any(c and c.lower() in text for c in customer_ids) or any(e and e.lower() in text for e in emails)


def scrub_webhook_payloads(*, customer_ids=(), emails=(), dry_run=False) -> int:
    """Redact personal fields in every stored Stripe event that mentions these customers or addresses."""
    customer_ids = {c for c in customer_ids if c}
    emails = {e for e in emails if e}
    if not (customer_ids or emails):
        return 0
    count = 0
    for event in StripeWebhookEvent.objects.only("pk", "payload").iterator():
        if not _mentions(event.payload, customer_ids, emails):
            continue
        scrubbed = _redact(event.payload)
        if scrubbed == event.payload:
            continue
        count += 1
        if not dry_run:
            StripeWebhookEvent.objects.filter(pk=event.pk).update(payload=scrubbed)
    return count


def _cancel_stripe(sub) -> bool:
    if sub is None or not sub.stripe_subscription_id or sub.status in TERMINAL:
        return False
    try:
        stripe.Subscription.cancel(sub.stripe_subscription_id, prorate=False, invoice_now=False)
    except stripe.error.InvalidRequestError as exc:
        # Already gone at Stripe (e.g. cancelled from the Dashboard): nothing left to charge.
        if getattr(exc, "code", "") == "resource_missing":
            return False
        raise AccountDeletionRefused("Δεν ήταν δυνατή η ακύρωση της συνδρομής. Δοκίμασε ξανά ή πάτα «Βοήθεια».") from exc
    except Exception as exc:
        logger.error("Account deletion: Stripe cancel failed (%s)", type(exc).__name__)
        raise AccountDeletionRefused("Δεν ήταν δυνατή η ακύρωση της συνδρομής. Δοκίμασε ξανά ή πάτα «Βοήθεια».") from exc
    return True


def owned_organization_ids(user):
    return list(OrganizationMember.objects.filter(user=user, role=OrganizationMember.OWNER)
                .values_list("organization_id", flat=True))


def check_deletable(user):
    owned = owned_organization_ids(user)
    if OrganizationMember.objects.filter(organization_id__in=owned).exclude(user=user).exists():
        raise AccountDeletionRefused(
            "Είσαι ιδιοκτήτης οργανισμού με άλλα μέλη. Γράψε μας από τη «Βοήθεια» για να μεταφερθεί πρώτα η ιδιοκτησία."
        )


def delete_account(user, *, reasons=(), text="", context="delete", actor=None) -> DeletionResult:
    check_deletable(user)
    sub = UserSubscription.objects.filter(user=user).first()
    stripe_cancelled = _cancel_stripe(sub)          # outside the transaction: a network call

    email, first_name = user.email, user.first_name
    customer_id = sub.stripe_customer_id if sub else ""
    with transaction.atomic():
        ref = user_ref_for(user)
        if reasons or text:
            ExitSurvey.objects.create(user=user, user_ref=ref, context=context, reasons=list(reasons), text=text[:2000])
        periods_closed = close_all_for_deleted_account(user)
        scrubbed = scrub_webhook_payloads(customer_ids=[customer_id], emails=[email])
        engagement = EmailEngagementEvent.objects.filter(email__iexact=email).delete()[0] if email else 0
        # Audit entries about this person keep the action but not their address.
        AdminAuditLog.objects.filter(target_type="User", target_id=str(user.pk)).update(target_repr=f"deleted:{ref}")
        solo_orgs = owned_organization_ids(user)          # check_deletable: nobody else is a member
        Organization.objects.filter(pk__in=solo_orgs).delete()
        organizations_deleted = len(solo_orgs)
        user_pk = user.pk
        user.delete()
        _reserve_e15(ref, user_pk, email, first_name)

    if actor is not None:
        from .superadmin.services import log_admin_action

        log_admin_action(actor, "delete_account", target_type="User", target_id=user_pk, target_repr=f"deleted:{ref}",
                         metadata={"stripe_cancelled": stripe_cancelled})
    return DeletionResult(ref, stripe_cancelled, periods_closed, scrubbed, engagement, organizations_deleted)


def _reserve_e15(ref, user_pk, email, first_name):
    """E15: the row stores no address; the address is handed to the worker only as a task argument,
    with save=False so django-q does not keep it in its task history after the send."""
    if not (lifecycle_enabled() and email):
        return
    row, created = LifecycleEmail.objects.get_or_create(
        dedupe_key=f"e15:{ref}",
        defaults={"kind": "e15", "user": None, "brevo_tag": f"lifecycle:e15:{ref}"},
    )
    if not created:
        return

    def _hand_off():
        try:
            from django_q.tasks import async_task

            async_task("gemiapp.tasks.send_account_deleted_email_task", row.pk, email, first_name, save=False)
        except Exception:
            logger.exception("Could not queue E15 %s; the address is not kept, so it will not be sent.", row.pk)
            LifecycleEmail.objects.filter(pk=row.pk).update(status="failed", reason="queue unavailable")

    transaction.on_commit(_hand_off)


def send_account_deleted_email(row_id, email, first_name="", *, now=None):
    """Worker side of E15. The address exists only in this call's arguments."""
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives
    from django.db.models import F
    from django.template.loader import render_to_string

    from .email_deliverability import is_email_delivery_suppressed
    from .email_headers import user_reply_to
    from .lifecycle_content import build_e15

    if not lifecycle_enabled():
        return "disabled"
    claimed = LifecycleEmail.objects.filter(pk=row_id, status="queued").update(status="sending",
                                                                                attempts=F("attempts") + 1)
    if not claimed:
        return "not_due"
    if is_email_delivery_suppressed(email):
        LifecycleEmail.objects.filter(pk=row_id).update(status="skipped", reason="suppressed")
        return "skipped"
    content = build_e15(first_name)
    context = {"content": content, "user": type("Recipient", (), {"email": email})}
    message = EmailMultiAlternatives(
        content.subject, render_to_string("emails/lifecycle.txt", context), settings.DEFAULT_FROM_EMAIL, [email],
        headers={"X-Mailin-Tag": LifecycleEmail.objects.get(pk=row_id).brevo_tag}, reply_to=user_reply_to(),
    )
    message.attach_alternative(render_to_string("emails/lifecycle.html", context), "text/html")
    try:
        message.send()
    except Exception as exc:
        # No retry: the address is not stored anywhere, by design.
        LifecycleEmail.objects.filter(pk=row_id).update(status="failed", reason=f"smtp: {type(exc).__name__}")
        return "failed"
    LifecycleEmail.objects.filter(pk=row_id).update(status="sent", sent_at=now or timezone.now())
    return "sent"
