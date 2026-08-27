"""Phase 5g: display-only helpers for billing templates.

Nothing here may be used to gate entitlement or authorization -- that remains
UserSubscription.has_active_paid_subscription / has_valid_complimentary_access / effective_tier
/ has_entitlement, untouched by this module. This module only decides what a human should read
and which of several mutually-exclusive lifecycle *actions* a template should offer, reusing the
existing status taxonomies from billing.py so the classification never drifts from what the
mutation endpoints themselves already enforce.
"""
from django import template

from ..billing import (
    CANCELABLE_STRIPE_SUBSCRIPTION_STATUSES,
    PROBLEMATIC_PAYMENT_STRIPE_SUBSCRIPTION_STATUSES,
    TERMINAL_STRIPE_SUBSCRIPTION_STATUSES,
)

register = template.Library()

STRIPE_STATUS_LABELS = {
    "active": "Ενεργή",
    "trialing": "Δοκιμαστική περίοδος",
    "past_due": "Εκκρεμεί πληρωμή",
    "unpaid": "Ανεξόφλητη",
    "incomplete": "Η πληρωμή δεν ολοκληρώθηκε",
    "incomplete_expired": "Έληξε",
    "canceled": "Ακυρωμένη",
    "paused": "Σε παύση",
}


@register.filter
def stripe_status_label(status):
    """Human-readable Greek label for a raw Stripe subscription status. Never renders the raw
    value to a user -- an unrecognised status gets a safe, generic fallback instead."""
    return STRIPE_STATUS_LABELS.get(status, "Άγνωστη κατάσταση")


@register.filter
def billing_state(sub):
    """One canonical lifecycle state for a UserSubscription, display purposes only.

    Priority order (item 20 of the Phase 5g brief): payment problem > scheduled cancellation >
    scheduled downgrade > normal active > terminal > complimentary-only > none. Each state maps
    to exactly one settings/pricing UI branch, so templates never need to re-derive this
    ordering themselves or risk showing two contradictory actions at once.
    """
    if not sub:
        return "none"
    if sub.stripe_subscription_id and sub.status in PROBLEMATIC_PAYMENT_STRIPE_SUBSCRIPTION_STATUSES:
        return "payment_problem"
    if sub.active_until:
        return "scheduled_cancellation"
    if sub.scheduled_tier:
        return "scheduled_downgrade"
    if sub.stripe_subscription_id and sub.status in CANCELABLE_STRIPE_SUBSCRIPTION_STATUSES:
        return "normal_active"
    if sub.stripe_subscription_id and sub.status in TERMINAL_STRIPE_SUBSCRIPTION_STATUSES:
        return "terminal"
    if sub.has_valid_complimentary_access:
        return "complimentary_only"
    return "none"
