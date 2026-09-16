"""Organization foundation (C1): the customer-tenant identity layer of blueprint §22-23.

Today Gemi Leads is ``User -> customer data``: CustomerRadar, UserCompanyLead (and its RadarMatch rows),
DigestPreference, DigestDelivery and UserSubscription all belong to a user, and billing and entitlements are
keyed on that user. The destination architecture is ``Organization -> customer data`` with users acting through
memberships. C1 creates only the destination identity: ``Organization``, ``OrganizationMember`` and
``OrganizationProfile``. No existing row is owned by an organization, no organization is created for existing
users, and no view, middleware, session or query uses these tables. Ownership moves in a later, explicitly
reviewed migration package -- not here.

Organization is not a GEMI Company
----------------------------------
A customer tenant and a monitored GEMI company are different things and are never linked: the tenant may be a
foreign business or an agency, and being monitored never makes a company a customer.

Roles (§22, §64)
----------------
OWNER, ADMIN, SALES_MANAGER, SALES_USER and VIEWER, exactly as the blueprint names them. C1 stores the role and
enforces valid values; it grants no permission and replaces nothing in Django auth. RBAC semantics arrive with
tenant-scoped authorization.

Creation
--------
``create_organization`` is the only approved way to create a tenant. In one transaction it creates the
organization, the creator's OWNER membership and the (possibly empty) profile, so an organization created
through the service always has at least one owner and a profile, and a failure leaves nothing behind. The
database does not force exactly one owner: several owners may become legitimate, and no such policy exists yet.

G5 -- multi-member organizations are blocked
--------------------------------------------
Release gate G5 requires tenant isolation before an organization may have more than one active member.
``add_organization_member`` exists only so later packages and tests have a correct primitive; it is internal,
wired to nothing, and the admin cannot add members. There are no invitations, no organization switcher and no
current-organization context anywhere.

Profile (§23)
-------------
The onboarding answer to «Τι πουλάς;» -- business, products, target customers, location -- stored as declared
free text. It says who the customer is, not what to monitor: nothing matches, filters or scores with it. The
structured targeting dimensions belong to the ICP (§24) and Radars (§25). The blueprint defines no website,
own-GEMI-number or completeness rule for the profile, so C1 adds none. No personal contact data is stored.

Nothing here calls GEMI, the web, Stripe or email.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction

PROFILE_FIELDS = ("business", "products", "target_customers", "location")


class OrganizationError(ValueError):
    """An organization request that violates the C1 invariants."""


@dataclass(frozen=True)
class CreatedOrganization:
    organization: object
    owner_membership: object
    profile: object


def _saved_active_user(user):
    if user is None or getattr(user, "pk", None) is None:
        raise OrganizationError("a saved user is required")
    if not user.is_active:
        raise OrganizationError("an inactive user cannot own or join an organization")
    return user


def create_organization(*, owner, name: str, **profile) -> CreatedOrganization:
    """Create an organization with its OWNER membership and profile, atomically."""
    Organization = apps.get_model("gemiapp", "Organization")
    OrganizationMember = apps.get_model("gemiapp", "OrganizationMember")
    OrganizationProfile = apps.get_model("gemiapp", "OrganizationProfile")
    _saved_active_user(owner)
    unknown = set(profile) - set(PROFILE_FIELDS)
    if unknown:
        raise OrganizationError(f"unknown profile fields: {sorted(unknown)}")
    name = (name or "").strip()
    values = {field: (profile.get(field) or "").strip() for field in PROFILE_FIELDS}

    organization = Organization(name=name)
    try:
        organization.full_clean()
        OrganizationProfile(organization=organization, **values).full_clean(exclude=["organization"])
    except ValidationError as exc:
        raise OrganizationError(f"invalid organization: {exc.message_dict}") from exc

    with transaction.atomic():
        organization.save()
        membership = OrganizationMember.objects.create(
            organization=organization, user=owner, role=OrganizationMember.OWNER,
        )
        profile_row = OrganizationProfile.objects.create(organization=organization, **values)
    return CreatedOrganization(organization=organization, owner_membership=membership, profile=profile_row)


def add_organization_member(organization, user, role: str):
    """INTERNAL ONLY -- not wired to any view, admin action, command or task.

    Multi-member organizations are blocked by release gate G5 until tenant isolation exists. This primitive is
    kept correct (valid role, one membership per user, active user) for later packages and tests.
    """
    OrganizationMember = apps.get_model("gemiapp", "OrganizationMember")
    _saved_active_user(user)
    if organization is None or organization.pk is None:
        raise OrganizationError("a saved organization is required")
    if role not in dict(OrganizationMember.ROLES):
        raise OrganizationError(f"unknown role: {role}")
    if OrganizationMember.objects.filter(organization=organization, user=user).exists():
        raise OrganizationError("the user is already a member of this organization")
    return OrganizationMember.objects.create(organization=organization, user=user, role=role)
