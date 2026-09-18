"""Tenant isolation and organization authorization (release gate G5, blueprint §22, §62, §64, §82).

§62: no customer sees another's Radars, opportunities, notes, call history or suppression lists, and authorization
is enforced in the backend, «όχι μόνο frontend hiding». §82: «Organization A requests opportunity B -> 404/403», as
a fully automated regression. This module is that backend boundary for the Organization architecture. It adds no
route, view, middleware, session state, model or migration.

The pair is the boundary
------------------------
Every authorization decision starts from an explicit **(user, organization)** pair and the ``OrganizationMember``
row that joins them. There is no ``user.organization``: a user may belong to several organizations with different
roles, so a tenant is never inferred from a first or newest membership, a subscription, a Radar or a URL. No
membership, no access. The membership is re-read on every entry-point call and never cached, so deleting it or
changing its role takes effect on the very next call.

``get_organization_access_context`` returns an immutable ``OrganizationAccessContext`` (organization, user,
membership, role). A context is minted only here and carries a private issuer mark, so a hand-built context is
refused; it is meant for one unit of work and is never stored.

Django staff and superusers get **no** bypass: the admin site is a separate, read-only internal concern, and
customer-product access is membership only. An inactive or unsaved user, or an unsaved organization, is denied.

One error, nothing leaked
-------------------------
Every refusal is the same ``OrganizationAccessDenied`` with the same message -- not a member, a missing
capability, another tenant's object, a nonexistent object. A caller therefore cannot tell whether an organization,
Radar or opportunity exists elsewhere, and cannot enumerate ids. The error carries no id, name or detail. A future
route maps it to 404.

Capabilities (§64)
------------------
§64 is five lines: OWNER «όλα», ADMIN «organization management», SALES_MANAGER «team/leads», SALES_USER
«assigned/visible opportunities», VIEWER «read only». ``ROLE_CAPABILITIES`` encodes them once, over the tenant data
that exists today; nothing else in the code may compare role strings.

* OWNER -- every capability, through the same table (no side bypass).
* ADMIN -- organization management: the organization's settings (profile, ICP), its members and its Radars, and
  read access to its Radars and opportunities. Lead work -- changing an opportunity's workflow state, assigning it
  -- is SALES_MANAGER's «leads», so ADMIN does not get it.
* SALES_MANAGER -- team and leads: every opportunity, their workflow and their assignment, and read access to the
  configuration that produces them. «Team» is read as directing the sales team's leads (§40: «Sales Manager ->
  assign lead -> salesperson»), not administering membership.
* SALES_USER -- only assigned/visible opportunities. **Assignment does not exist yet**, so the only safe reading
  is that a sales user currently sees *no* opportunity: ``VIEW_ASSIGNED_OPPORTUNITIES`` is an
  assignment-dependent capability that is not yet satisfiable. A sales user's feed is a correctly shaped empty
  page and any opportunity id is denied -- never the organization's whole pipeline. The assignment package widens
  this through the same helpers.
* VIEWER -- read only: everything the organization can read, nothing it can change.

Platform data is not tenant data
--------------------------------
Industry templates and the GEMI reference tables are platform-owned and readable by any service; nothing here
scopes them. Tenant data is what hangs off an organization: its profile, ICP (and criteria), Radars (and criteria
and exclusions), opportunities (and their signals, score components and evidence), and its memberships. Children
never need a lookup of their own: authorization is inherited from the root they belong to.

Scoping
-------
Every read helper starts in SQL from ``organization_id = context.organization_id`` and applies the role's
visibility there; nothing loads globally and filters afterwards. The system-level engines -- matching, scoring and
live breakdown (C5-C7) -- evaluate every organization's Radars by design and must never serve a customer request;
customer code reads persisted opportunities through this module only.

The legacy user-owned product (CustomerRadar, UserCompanyLead, UserSubscription, billing) is untouched: G5 governs
the Organization architecture only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.apps import apps
from django.db import transaction
from django.db.models import Count

from .opportunities import get_opportunity_score_breakdown
from .company_signals import LIVE
from .opportunity_feed import (
    DEFAULT_PAGE_SIZE, FEED_ORDER, MAX_PAGE_SIZE, FeedFilters, OpportunityFeedPage, get_opportunity_feed,
)


class OrganizationAccessDenied(PermissionError):
    """The single refusal of the tenant boundary. Deliberately says nothing about why or about what."""

    MESSAGE = "organization access denied"

    def __init__(self):
        super().__init__(self.MESSAGE)


class Capability:
    VIEW_ORGANIZATION = "view_organization"
    VIEW_ORGANIZATION_SETTINGS = "view_organization_settings"
    MANAGE_ORGANIZATION = "manage_organization"
    MANAGE_MEMBERS = "manage_members"
    VIEW_RADARS = "view_radars"
    MANAGE_RADARS = "manage_radars"
    VIEW_ALL_OPPORTUNITIES = "view_all_opportunities"
    # Assignment-dependent: not yet satisfiable, because no assignment model exists.
    VIEW_ASSIGNED_OPPORTUNITIES = "view_assigned_opportunities"
    MANAGE_OPPORTUNITY_WORKFLOW = "manage_opportunity_workflow"
    ASSIGN_OPPORTUNITIES = "assign_opportunities"

    ALL = (VIEW_ORGANIZATION, VIEW_ORGANIZATION_SETTINGS, MANAGE_ORGANIZATION, MANAGE_MEMBERS, VIEW_RADARS,
           MANAGE_RADARS, VIEW_ALL_OPPORTUNITIES, VIEW_ASSIGNED_OPPORTUNITIES, MANAGE_OPPORTUNITY_WORKFLOW,
           ASSIGN_OPPORTUNITIES)


OWNER, ADMIN, SALES_MANAGER, SALES_USER, VIEWER = "owner", "admin", "sales_manager", "sales_user", "viewer"
_READ_ORGANIZATION = frozenset({
    Capability.VIEW_ORGANIZATION, Capability.VIEW_ORGANIZATION_SETTINGS, Capability.VIEW_RADARS,
    Capability.VIEW_ALL_OPPORTUNITIES, Capability.VIEW_ASSIGNED_OPPORTUNITIES,
})
# The one place a role becomes permissions (§64).
ROLE_CAPABILITIES = {
    OWNER: frozenset(Capability.ALL),
    ADMIN: _READ_ORGANIZATION | {Capability.MANAGE_ORGANIZATION, Capability.MANAGE_MEMBERS,
                                 Capability.MANAGE_RADARS},
    SALES_MANAGER: _READ_ORGANIZATION | {Capability.MANAGE_OPPORTUNITY_WORKFLOW, Capability.ASSIGN_OPPORTUNITIES},
    SALES_USER: frozenset({Capability.VIEW_ORGANIZATION, Capability.VIEW_ASSIGNED_OPPORTUNITIES}),
    VIEWER: _READ_ORGANIZATION,
}
# Capabilities whose scope depends on an assignment model that does not exist yet.
ASSIGNMENT_DEPENDENT = frozenset({Capability.VIEW_ASSIGNED_OPPORTUNITIES})

_ISSUER = object()


@dataclass(frozen=True)
class OrganizationAccessContext:
    organization_id: int
    user_id: int
    membership_id: int
    role: str
    _issuer: object = field(default=None, repr=False, compare=False)


def _model(name):
    return apps.get_model("gemiapp", name)


def get_organization_access_context(user, organization) -> OrganizationAccessContext:
    """The access context of this user inside this organization, from their membership. Read-only; one query."""
    User, Organization = apps.get_model("auth", "User"), _model("Organization")
    if not isinstance(user, User) or user.pk is None or not user.is_active:
        raise OrganizationAccessDenied()
    if not isinstance(organization, Organization) or organization.pk is None:
        raise OrganizationAccessDenied()
    membership = (_model("OrganizationMember").objects
                  .filter(organization_id=organization.pk, user_id=user.pk).only("pk", "role").first())
    if membership is None or membership.role not in ROLE_CAPABILITIES:
        raise OrganizationAccessDenied()
    return OrganizationAccessContext(organization_id=organization.pk, user_id=user.pk, membership_id=membership.pk,
                                     role=membership.role, _issuer=_ISSUER)


def _issued(context) -> OrganizationAccessContext:
    if not isinstance(context, OrganizationAccessContext) or context._issuer is not _ISSUER:
        raise OrganizationAccessDenied()
    return context


def can(context: OrganizationAccessContext, capability: str) -> bool:
    """Whether this membership holds a capability. The only role -> permission decision in the code."""
    return capability in ROLE_CAPABILITIES[_issued(context).role]


def require(context: OrganizationAccessContext, capability: str) -> OrganizationAccessContext:
    if not can(context, capability):
        raise OrganizationAccessDenied()
    return context


# --- scoped reads (SQL-first) ----------------------------------------------------------------------

def organization_radars_for(context: OrganizationAccessContext):
    """This organization's Radars, if the role may read them."""
    require(context, Capability.VIEW_RADARS)
    return _model("OrganizationRadar").objects.filter(organization_id=context.organization_id)


def organization_opportunities_for(context: OrganizationAccessContext):
    """The opportunities this membership may see, scoped in SQL. A sales user sees none until assignment exists."""
    Opportunity = _model("Opportunity")
    scoped = Opportunity.objects.filter(organization_id=context.organization_id)
    if can(context, Capability.VIEW_ALL_OPPORTUNITIES):
        return scoped
    if can(context, Capability.VIEW_ASSIGNED_OPPORTUNITIES):
        return scoped.none()  # assignment-dependent: not yet satisfiable
    raise OrganizationAccessDenied()


def _object_id(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OrganizationAccessDenied()
    return value


# --- entry points: (user, organization) ----------------------------------------------------------

def get_authorized_radar(user, organization, radar_id):
    """One of this organization's Radars, or the single refusal. Never loads a Radar globally."""
    context = get_organization_access_context(user, organization)
    radar = organization_radars_for(context).filter(pk=_object_id(radar_id)).first()
    if radar is None:
        raise OrganizationAccessDenied()
    return radar


def get_authorized_opportunity(user, organization, opportunity_id):
    """One opportunity this membership may see, or the single refusal (§82). The foundation of the D29 page."""
    context = get_organization_access_context(user, organization)
    opportunity = (organization_opportunities_for(context).filter(pk=_object_id(opportunity_id))
                   .select_related("radar", "company", "first_signal", "latest_signal").first())
    if opportunity is None:
        raise OrganizationAccessDenied()
    return opportunity


def get_authorized_opportunity_score_breakdown(user, organization, opportunity_id):
    """The frozen C8 explanation of an opportunity this membership may see («Why this lead»). Read-only."""
    return get_opportunity_score_breakdown(get_authorized_opportunity(user, organization, opportunity_id))


def get_authorized_opportunity_feed(user, organization, filters: FeedFilters | None = None, *,
                                    limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None) -> OpportunityFeedPage:
    """The C9 feed as this membership may see it. C9 itself is unchanged and still requires an explicit
    organization; this layer decides whether and how much of it a member may read."""
    context = get_organization_access_context(user, organization)
    filters = filters or FeedFilters()
    if not isinstance(filters, FeedFilters):
        raise OrganizationAccessDenied()
    if filters.radar_ids:
        # A foreign Radar id is refused exactly like a missing one: nothing about the other tenant leaks.
        owned = organization_radars_for(context).filter(pk__in=list(filters.radar_ids)).count()
        if owned != len(set(filters.radar_ids)):
            raise OrganizationAccessDenied()
    if can(context, Capability.VIEW_ALL_OPPORTUNITIES):
        return get_opportunity_feed(organization, filters, limit=limit, cursor=cursor)
    if can(context, Capability.VIEW_ASSIGNED_OPPORTUNITIES):
        # Assignment-dependent visibility: a correctly shaped empty page, never the organization's pipeline.
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise OrganizationAccessDenied()
        return OpportunityFeedPage(organization_id=context.organization_id, cards=(), limit=limit, next_cursor=None)
    raise OrganizationAccessDenied()


def get_authorized_organization_profile(user, organization):
    """The organization's declared profile (§23), for roles that may read its settings."""
    context = require(get_organization_access_context(user, organization), Capability.VIEW_ORGANIZATION_SETTINGS)
    return _model("OrganizationProfile").objects.filter(organization_id=context.organization_id).first()


def get_authorized_icp_criteria(user, organization):
    """The organization's ICP (§24) as its immutable C2 value, for roles that may read its settings."""
    from .organization_icp import get_organization_icp_criteria

    require(get_organization_access_context(user, organization), Capability.VIEW_ORGANIZATION_SETTINGS)
    return get_organization_icp_criteria(organization)


def organization_members_for(context: OrganizationAccessContext):
    """The organization's memberships (ids and roles only) for roles that administer members or direct the team's
    leads. Scoped in SQL; no email, name or other personal field is selected."""
    if not (can(context, Capability.MANAGE_MEMBERS) or can(context, Capability.ASSIGN_OPPORTUNITIES)):
        raise OrganizationAccessDenied()
    return (_model("OrganizationMember").objects.filter(organization_id=context.organization_id)
            .only("pk", "organization_id", "user_id", "role").order_by("pk"))


# --- D29: the company opportunity page -------------------------------------------------------------

_PAGE_FIELDS = (
    "pk", "organization_id", "radar_id", "company_id", "score", "score_class", "status", "primary_reason_code",
    "scored_as_of", "score_rule_version", "match_rule_version", "latest_signal_id", "radar__name",
    "latest_signal__signal_type", "latest_signal__detected_at", "latest_signal__mode", "company__gemi_number",
    "company__name", "company__trade_names",
)


def get_authorized_company_opportunity_page(user, organization_id, company_id):
    """The §36 page of one company, as this membership may see it, or the single refusal.

    Both ids come from the route and are resolved here, never trusted: a nonexistent organization, a non-member,
    another tenant's company, a company with no visible opportunity and a sales user without assignments are all
    the same ``OrganizationAccessDenied``. Only opportunities whose current capture rests on a **LIVE** signal
    survive -- SHADOW validation data never reaches a customer -- and the primary is chosen in the C9 feed order
    among the survivors. The company's other data are loaded only once access is established.
    """
    from .company_opportunity_page import build_company_opportunity_page

    organization = _organization_by_id(organization_id)
    context = get_organization_access_context(user, organization)
    rows = list(
        organization_opportunities_for(context)
        .filter(company_id=_object_id(company_id), latest_signal__mode=LIVE)
        .select_related("radar", "latest_signal", "company").only(*_PAGE_FIELDS)
        .order_by(*FEED_ORDER)
    )
    if not rows:
        raise OrganizationAccessDenied()
    live_signal_counts = dict(
        _model("OpportunitySignal").objects.filter(opportunity_id__in=[row.pk for row in rows], signal__mode=LIVE)
        .values("opportunity_id").annotate(n=Count("signal_id", distinct=True)).values_list("opportunity_id", "n")
    )
    workflow = can(context, Capability.MANAGE_OPPORTUNITY_WORKFLOW)
    save_actions = {row.pk: _save_action(row.status) if workflow else None for row in rows}
    return build_company_opportunity_page(organization=organization, rows=rows, live_signal_counts=live_signal_counts,
                                          save_actions=save_actions)


def _organization_by_id(organization_id):
    """An organization named by a route id, or the single refusal -- never a hint that it exists."""
    organization = _model("Organization").objects.filter(pk=_object_id(organization_id)).only("pk", "name").first()
    if organization is None:
        raise OrganizationAccessDenied()
    return organization


# --- D30: Save --------------------------------------------------------------------------------------
#
# Save is one narrow lifecycle action on one explicit opportunity: NEW or VIEWED -> SAVED, and SAVED stays SAVED
# without a write. Every later §39 state is refused and left untouched, so Save never moves an opportunity
# backwards. There is no unsave and no general transition graph: item 32 (statuses) owns lifecycle policy.

SAVED = "saved"
SAVE_ALLOWED_FROM = frozenset({"new", "viewed"})


def _save_action(status: str):
    """What the page may offer for Save on a row: an active action, the settled saved state, or nothing."""
    if status == SAVED:
        return "saved"
    return "save" if status in SAVE_ALLOWED_FROM else None


@dataclass(frozen=True)
class SaveOpportunityResult:
    organization_id: int
    company_id: int
    opportunity_id: int
    status: str
    changed: bool  # False for the idempotent SAVED -> SAVED path, which writes nothing


class OpportunityTransitionRefused(Exception):
    """The opportunity is visible to this member, but its current state does not allow Save. Nothing changed."""

    MESSAGE = "Η ευκαιρία δεν μπορεί να αποθηκευτεί από την τρέχουσα κατάστασή της."

    def __init__(self, result: SaveOpportunityResult):
        super().__init__(self.MESSAGE)
        self.result = result


def save_authorized_opportunity(user, organization_id, opportunity_id) -> SaveOpportunityResult:
    """D30: move one customer-visible opportunity to SAVED, if this membership may and its state allows.

    The organization and the opportunity both come from the route and are resolved here: a nonexistent or foreign
    organization, a non-member, a role without ``manage_opportunity_workflow``, another tenant's or a nonexistent
    opportunity, and a SHADOW-backed one are all the same ``OrganizationAccessDenied``. The row is re-read under a
    lock inside the transaction, so a concurrent Save sees SAVED and takes the no-write path. PostgreSQL provides
    the row lock; SQLite serialises writes differently, so its tests prove the behaviour, not the locking.
    """
    organization = _organization_by_id(organization_id)
    context = require(get_organization_access_context(user, organization), Capability.MANAGE_OPPORTUNITY_WORKFLOW)
    with transaction.atomic():
        row = (organization_opportunities_for(context)
               .filter(pk=_object_id(opportunity_id), latest_signal__mode=LIVE)
               .select_for_update(of=("self",)).only("pk", "organization_id", "company_id", "status").first())
        if row is None:
            raise OrganizationAccessDenied()
        result = SaveOpportunityResult(organization_id=row.organization_id, company_id=row.company_id,
                                       opportunity_id=row.pk, status=row.status, changed=False)
        if row.status == SAVED:
            return result  # idempotent: no write at all
        if row.status not in SAVE_ALLOWED_FROM:
            raise OpportunityTransitionRefused(result)
        row.status = SAVED
        row.save(update_fields=["status", "updated_at"])
    return SaveOpportunityResult(organization_id=row.organization_id, company_id=row.company_id,
                                 opportunity_id=row.pk, status=SAVED, changed=True)
