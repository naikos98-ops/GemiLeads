"""The canonical Do Not Contact identity and lookup (D35, blueprint §51 «contact_suppressions»).

Authorization-free on purpose: internal engines (C8 materialization) and customer-facing services
(``gemiapp.organization_access``, which re-exports these names) both answer «is this suppressed for this
organization?» through exactly these functions, so the identity rule and the query exist once.

D35 v1 identity: ``contact_type = "company"`` with ``contact_value`` = the company's GEMI number under the
repository's single rule (``ingestion.discovery.normalize_gemi_number``: trimmed, digits only, positive, kept as text).
A company without a usable GEMI number has no suppression identity and is never considered suppressed -- no other
identity is invented. Every lookup is scoped by an explicit organization: another organization's record never counts.
"""

from django.apps import apps

SUPPRESSION_COMPANY = "company"
SUPPRESSION_CONTACT_TYPES = frozenset({SUPPRESSION_COMPANY})  # further contact points: Phase G


def _model(name):
    return apps.get_model("gemiapp", name)


def company_suppression_value(gemi_number):
    """The canonical company identity for suppression, or None when the GEMI number is not usable."""
    from .ingestion.discovery import normalize_gemi_number

    normalized = normalize_gemi_number(gemi_number)
    return normalized[0] if normalized else None


def suppression_identity(contact_type, contact_value):
    """The normalized value for a suppressible contact type, or None (unknown type or unusable value)."""
    if contact_type not in SUPPRESSION_CONTACT_TYPES:
        return None
    return company_suppression_value(contact_value)


def organization_pk(organization):
    """An explicit organization as its primary key; anything else is a programming error, never a global lookup."""
    Organization = _model("Organization")
    if isinstance(organization, Organization) and organization.pk is not None:
        return organization.pk
    if isinstance(organization, int) and not isinstance(organization, bool) and organization > 0:
        return organization
    raise ValueError("an explicit organization is required")


def is_contact_suppressed(organization, contact_type, contact_value) -> bool:
    """The canonical enforcement check: is this contact subject suppressed for this organization? Unknown types are
    not suppressible yet and answer False; an unusable value is never a match."""
    value = suppression_identity(contact_type, contact_value)
    if value is None:
        return False
    return _model("OrganizationContactSuppression").objects.filter(
        organization_id=organization_pk(organization), contact_type=contact_type, contact_value=value).exists()


def is_company_suppressed(organization, company) -> bool:
    """``is_contact_suppressed`` for a company, deriving its identity here so no caller re-implements it."""
    return is_contact_suppressed(organization, SUPPRESSION_COMPANY, getattr(company, "gemi_number", None))
