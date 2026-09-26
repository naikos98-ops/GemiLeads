"""Explicit, create-only provisioning of an Organization for existing legacy Radar owners.

Called only by ``provision_existing_user_organizations``. It exists because the legacy Radar migration refuses
every Radar whose owner belongs to no Organization (``missing_organization``); this command gives those owners the
one tenant the Radar migration needs, and nothing else. It never migrates a Radar, lead, digest, Signal or
Opportunity, and never touches billing: the organization's entitlement stays derived from the owner's existing
``UserSubscription`` (``gemiapp.organization_entitlement``), which is only read here.

Population (narrowest default): users who own at least one live (not soft-deleted) ``CustomerRadar`` -- exactly
the owners the Radar migration examines. Everyone else is only counted, never provisioned.

Resolution, identical to the Radar migration's destination rule and independent of role:

* exactly one ``OrganizationMember`` (any role) -> ``already_provisioned``, untouched;
* more than one -> ``ambiguous_organization``, untouched;
* zero -> a candidate, unless the account is inactive (unverified signups are inactive), a known demo seed
  account, or staff/superuser (only with ``include_staff``).

A candidate gets, through the canonical ``create_organization``, one Organization named by
``default_organization_name`` with the user as OWNER and an empty profile, in a per-user transaction that
re-reads the user's memberships under a row lock first. Output is counters only: no names, emails or criteria.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q

from .models import CustomerRadar, OrganizationMember, UserCompanyLead
from .organization_entitlement import resolve_organization_entitlement
from .organizations import create_organization, default_organization_name

DEFAULT_LIMIT = 100
MAX_LIMIT = 10_000
# The only account the repository's seed commands create (seed_demo, seed_demo_marketing_data).
DEMO_USERNAMES = frozenset({"demo@gemileads.gr"})


class ProvisioningInvariantError(RuntimeError):
    """The post-create verification failed; raising rolls the user's transaction back."""


@dataclass
class ProvisioningReport:
    dry_run: bool
    counters: dict[str, int] = field(default_factory=lambda: {
        "users_examined": 0, "users_without_organization": 0, "eligible": 0, "provisioned": 0,
        "already_provisioned": 0, "ambiguous_organization": 0, "skipped_inactive": 0, "skipped_staff": 0,
        "skipped_demo": 0, "skipped_out_of_scope": 0, "race_skipped": 0, "errors": 0,
        "eligible_entitled": 0, "eligible_not_entitled": 0, "eligible_with_legacy_leads": 0,
        "legacy_radars_owned": 0, "legacy_radars_unblocked": 0, "legacy_radars_still_blocked": 0,
        "out_of_scope_active_users_without_organization": 0,
    })

    def increment(self, name: str, amount: int = 1):
        self.counters[name] += amount

    def lines(self):
        yield f"mode={'dry-run' if self.dry_run else 'write'} scope=legacy_radar_owners"
        for name, value in self.counters.items():
            yield f"{name}={value}"


def _live_radars(user_ref):
    return CustomerRadar.objects.filter(user_id=user_ref, deleted_at__isnull=True)


def _annotated_users():
    return (User.objects.select_related("subscription")
            .annotate(live_radars=Count("customer_radars", filter=Q(customer_radars__deleted_at__isnull=True),
                                        distinct=True),
                      memberships=Count("organization_memberships", distinct=True),
                      has_leads=Exists(UserCompanyLead.objects.filter(user_id=OuterRef("pk"))))
            .order_by("pk"))


def _is_demo(user) -> bool:
    return user.username in DEMO_USERNAMES or (user.email or "").lower() in DEMO_USERNAMES


def _is_staff(user) -> bool:
    return bool(user.is_staff or user.is_superuser)


def _owner_entitled(user) -> bool:
    subscription = getattr(user, "subscription", None)  # a missing row resolves to None, never an error
    return bool(subscription is not None and subscription.has_entitlement)


def _skip_reason(user, include_staff: bool):
    if not user.is_active:
        return "skipped_inactive"
    if _is_demo(user):
        return "skipped_demo"
    if _is_staff(user) and not include_staff:
        return "skipped_staff"
    return None


def _provision(user_id: int, include_staff: bool) -> bool:
    """Create the user's Organization unless something changed since selection. True when created."""
    with transaction.atomic():
        # Serializes with provision_organization_for_user and with a second run of this command.
        user = User.objects.select_for_update().select_related("subscription").get(pk=user_id)
        if OrganizationMember.objects.filter(user=user).exists() or _skip_reason(user, include_staff):
            return False
        entitled_before = _owner_entitled(user)
        created = create_organization(owner=user, name=default_organization_name(user))
        memberships = list(OrganizationMember.objects.filter(user=user))
        if (len(memberships) != 1 or memberships[0].pk != created.owner_membership.pk
                or memberships[0].role != OrganizationMember.OWNER
                or memberships[0].organization_id != created.organization.pk):
            raise ProvisioningInvariantError("the user does not have exactly one owner membership")
        if resolve_organization_entitlement(created.organization).entitled != entitled_before:
            raise ProvisioningInvariantError("the derived organization entitlement differs from the owner's")
    return True


def provision_existing_user_organizations(*, dry_run=False, limit=DEFAULT_LIMIT, user_id=None,
                                          include_staff=False) -> ProvisioningReport:
    report = ProvisioningReport(dry_run=dry_run)
    users = _annotated_users()
    if user_id is not None:
        users = users.filter(pk=user_id)
    else:
        users = users.filter(Exists(_live_radars(OuterRef("pk"))))
    for user in users[:limit]:
        report.increment("users_examined")
        if not user.live_radars:
            report.increment("skipped_out_of_scope")  # only reachable through --user-id
            continue
        report.increment("legacy_radars_owned", user.live_radars)
        if user.memberships == 1:
            report.increment("already_provisioned")
            continue
        if user.memberships > 1:
            report.increment("ambiguous_organization")
            report.increment("legacy_radars_still_blocked", user.live_radars)
            continue
        report.increment("users_without_organization")
        reason = _skip_reason(user, include_staff)
        if reason:
            report.increment(reason)
            report.increment("legacy_radars_still_blocked", user.live_radars)
            continue
        report.increment("eligible")
        report.increment("eligible_entitled" if _owner_entitled(user) else "eligible_not_entitled")
        if user.has_leads:
            report.increment("eligible_with_legacy_leads")
        if dry_run:
            report.increment("legacy_radars_unblocked", user.live_radars)
            continue
        try:
            created = _provision(user.pk, include_staff)
        except Exception:
            # The per-user transaction rolled back; counts only, never messages that could carry names or emails.
            report.increment("errors")
            report.increment("legacy_radars_still_blocked", user.live_radars)
            continue
        if created:
            report.increment("provisioned")
            report.increment("legacy_radars_unblocked", user.live_radars)
        else:
            # Another writer gave the user a membership (or the account changed) after selection; rerun to
            # classify them. Their Radars are counted neither as unblocked nor as still blocked.
            report.increment("race_skipped")
    if user_id is None:
        report.increment("out_of_scope_active_users_without_organization", User.objects.filter(
            is_active=True, organization_memberships__isnull=True,
        ).exclude(Exists(_live_radars(OuterRef("pk")))).count())
    return report
