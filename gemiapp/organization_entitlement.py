"""Organization entitlement -- the compatibility layer between user-owned billing and organization-owned 2.0.

Billing stays **user-owned**: Stripe checkout, webhooks and ``UserSubscription`` are untouched, and nothing here
writes anything. An organization has no subscription of its own; its entitlement is **derived, read-only, from its
owner's existing ``UserSubscription``**:

    Organization -> the owner OrganizationMember -> that User -> UserSubscription -> ``has_entitlement``

The rule is the legacy product's own paid-access rule, reused rather than restated: an active paid subscription
(pro, business, enterprise, custom) or valid complimentary access -- exactly ``UserSubscription.has_entitlement``,
whose SQL twin is ``models.entitlement_q``. It is the same line the legacy app draws for every paid feature
(``radar_limit > 0``: a free, cancelled, lapsed or expired account gets 0). No tier, price or limit is added.

It fails closed. An organization is entitled only when it has **exactly one** owner membership, that owner's
account is active and that owner holds an entitlement. No owner, several owners, an inactive owner, a missing
subscription row or a free/inactive/expired plan all resolve to "not entitled". Another member's subscription never
counts -- only the owner's -- and staff/superuser flags play no part.

Two forms of the same rule: ``resolve_organization_entitlement`` (one organization, with a reason, for operators and
tests) and ``entitled_organizations`` (a queryset, so the tenant boundary can apply it inside its own SQL).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.apps import apps
from django.db.models import Count, Exists, OuterRef, Subquery
from django.db.models.functions import Coalesce

from .models import entitlement_q

ENTITLED = "entitled"
NO_ORGANIZATION = "no_organization"
NO_OWNER = "no_owner"
MULTIPLE_OWNERS = "multiple_owners"
OWNER_INACTIVE = "owner_inactive"
OWNER_NOT_ENTITLED = "owner_not_entitled"
FREE_TIER = "free"


@dataclass(frozen=True)
class OrganizationEntitlement:
    organization_id: int | None
    entitled: bool
    reason: str                  # one of the constants above
    owner_user_id: int | None    # only when there is exactly one owner
    effective_tier: str          # the owner's effective tier when entitled, otherwise "free"


def _model(name):
    return apps.get_model("gemiapp", name)


def resolve_organization_entitlement(organization) -> OrganizationEntitlement:
    """The organization's entitlement, derived from its single owner's subscription. Read-only; one query."""
    Organization, Member = _model("Organization"), _model("OrganizationMember")
    if not isinstance(organization, Organization) or organization.pk is None:
        return OrganizationEntitlement(None, False, NO_ORGANIZATION, None, FREE_TIER)
    owners = list(Member.objects.filter(organization_id=organization.pk, role=Member.OWNER)
                  .select_related("user", "user__subscription").order_by("pk")[:2])
    if not owners:
        return OrganizationEntitlement(organization.pk, False, NO_OWNER, None, FREE_TIER)
    if len(owners) > 1:
        return OrganizationEntitlement(organization.pk, False, MULTIPLE_OWNERS, None, FREE_TIER)
    owner = owners[0].user
    if not owner.is_active:
        return OrganizationEntitlement(organization.pk, False, OWNER_INACTIVE, owner.pk, FREE_TIER)
    subscription = getattr(owner, "subscription", None)  # a missing row resolves to None, never an error
    if subscription is None or not subscription.has_entitlement:
        return OrganizationEntitlement(organization.pk, False, OWNER_NOT_ENTITLED, owner.pk, FREE_TIER)
    return OrganizationEntitlement(organization.pk, True, ENTITLED, owner.pk, subscription.effective_tier)


def entitled_organizations(now=None):
    """Organizations that are entitled by the rule above, as a queryset (the SQL form of the same rule)."""
    Organization, Member = _model("Organization"), _model("OrganizationMember")
    owners = Member.objects.filter(organization_id=OuterRef("pk"), role=Member.OWNER)
    owner_count = owners.order_by().values("organization_id").annotate(n=Count("pk")).values("n")
    return (Organization.objects.annotate(owner_memberships=Coalesce(Subquery(owner_count), 0))
            .filter(owner_memberships=1)
            .filter(Exists(owners.filter(user__is_active=True).filter(entitlement_q("user__subscription__", now)))))
