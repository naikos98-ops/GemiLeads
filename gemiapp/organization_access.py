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
* SALES_USER -- only assigned/visible opportunities. Since D31 that is exactly the opportunities whose
  ``assigned_to`` is **this membership** (never "this user": a user's memberships in other organizations, or an
  earlier membership, grant nothing). The scope is applied in SQL before anything is aggregated, so a sales user
  never sees a sibling opportunity of the same company that is assigned to someone else or to nobody.
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

Membership is necessary, not sufficient (compatibility layer)
------------------------------------------------------------
Billing stays user-owned. An organization's paid access is derived from its single owner's existing subscription
(``gemiapp.organization_entitlement``), and a context is minted only for a member of an **entitled** organization --
checked inside the same one membership query. A member of an organization that is not entitled gets
``OrganizationNotEntitled``, a subclass of the single refusal with the same message: callers that only know the
refusal still deny, and the customer views show the legacy paywall instead of a 404. It is raised only after the
membership is established, so it tells a non-member nothing. Another member's subscription never counts.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date

from django.apps import apps
from django.db import IntegrityError, transaction
from django.db.models import Case, Count, Exists, F, OuterRef, Q, Value, When
from django.utils import timezone

from .organization_entitlement import entitled_organizations
from .contact_suppressions import (  # the canonical D35 identity and lookup, re-exported for customer code
    SUPPRESSION_COMPANY, SUPPRESSION_CONTACT_TYPES, company_suppression_value, is_company_suppressed,
    is_contact_suppressed,
)
from .opportunities import get_opportunity_score_breakdown
from .company_signals import LIVE
from .opportunity_feed import (
    DEFAULT_PAGE_SIZE, FEED_ORDER, MAX_PAGE_SIZE, FeedError, FeedFilters, OpportunityFeedPage, get_opportunity_feed,
)


class OrganizationAccessDenied(PermissionError):
    """The single refusal of the tenant boundary. Deliberately says nothing about why or about what."""

    MESSAGE = "organization access denied"

    def __init__(self):
        super().__init__(self.MESSAGE)


class OrganizationNotEntitled(OrganizationAccessDenied):
    """The caller *is* a member, but the organization's owner holds no entitlement (compatibility layer). Same
    message as every refusal; raised only once the membership is established."""


class Capability:
    VIEW_ORGANIZATION = "view_organization"
    VIEW_ORGANIZATION_SETTINGS = "view_organization_settings"
    MANAGE_ORGANIZATION = "manage_organization"
    MANAGE_MEMBERS = "manage_members"
    VIEW_RADARS = "view_radars"
    MANAGE_RADARS = "manage_radars"
    VIEW_ALL_OPPORTUNITIES = "view_all_opportunities"
    # Scoped by assignment: only opportunities assigned to this very membership (D31).
    VIEW_ASSIGNED_OPPORTUNITIES = "view_assigned_opportunities"
    MANAGE_OPPORTUNITY_WORKFLOW = "manage_opportunity_workflow"
    ASSIGN_OPPORTUNITIES = "assign_opportunities"
    # D32: the general sales statuses. Granted to sales users too, but always applied through the same visibility
    # scope, so a sales user can only ever reach the opportunities assigned to their own membership.
    UPDATE_OPPORTUNITY_STATUS = "update_opportunity_status"
    # D33: writing a note. Reading notes needs no capability of its own: they are read with their opportunity.
    ADD_OPPORTUNITY_NOTE = "add_opportunity_note"
    # D34: creating and completing tasks. Reading them follows the opportunity, like notes.
    MANAGE_OPPORTUNITY_TASKS = "manage_opportunity_tasks"
    # D35: company-level Do Not Contact. Never a sales user's: it changes every opportunity of the company,
    # including sibling rows a sales user cannot see.
    MANAGE_CONTACT_SUPPRESSIONS = "manage_contact_suppressions"

    ALL = (VIEW_ORGANIZATION, VIEW_ORGANIZATION_SETTINGS, MANAGE_ORGANIZATION, MANAGE_MEMBERS, VIEW_RADARS,
           MANAGE_RADARS, VIEW_ALL_OPPORTUNITIES, VIEW_ASSIGNED_OPPORTUNITIES, MANAGE_OPPORTUNITY_WORKFLOW,
           ASSIGN_OPPORTUNITIES, UPDATE_OPPORTUNITY_STATUS, ADD_OPPORTUNITY_NOTE, MANAGE_OPPORTUNITY_TASKS,
           MANAGE_CONTACT_SUPPRESSIONS)


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
    SALES_MANAGER: _READ_ORGANIZATION | {Capability.MANAGE_OPPORTUNITY_WORKFLOW, Capability.ASSIGN_OPPORTUNITIES,
                                         Capability.UPDATE_OPPORTUNITY_STATUS, Capability.ADD_OPPORTUNITY_NOTE,
                                         Capability.MANAGE_OPPORTUNITY_TASKS, Capability.MANAGE_CONTACT_SUPPRESSIONS},
    SALES_USER: frozenset({Capability.VIEW_ORGANIZATION, Capability.VIEW_ASSIGNED_OPPORTUNITIES,
                           Capability.UPDATE_OPPORTUNITY_STATUS, Capability.ADD_OPPORTUNITY_NOTE,
                           Capability.MANAGE_OPPORTUNITY_TASKS}),
    VIEWER: _READ_ORGANIZATION,
}
# Capabilities whose scope is the membership's own assignments (satisfiable since D31).
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
                  .filter(organization_id=organization.pk, user_id=user.pk).only("pk", "role")
                  .annotate(organization_entitled=Exists(entitled_organizations().filter(pk=OuterRef("organization_id"))))
                  .first())
    if membership is None or membership.role not in ROLE_CAPABILITIES:
        raise OrganizationAccessDenied()
    if not membership.organization_entitled:
        raise OrganizationNotEntitled()
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
    """The opportunities this membership may see, scoped in SQL: the whole organization's, or -- for a sales user --
    only those assigned to this membership."""
    Opportunity = _model("Opportunity")
    scoped = Opportunity.objects.filter(organization_id=context.organization_id)
    if can(context, Capability.VIEW_ALL_OPPORTUNITIES):
        return scoped
    if can(context, Capability.VIEW_ASSIGNED_OPPORTUNITIES):
        return scoped.filter(assigned_to_id=context.membership_id)
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
    return _feed_for(context, organization, filters, limit=limit, cursor=cursor)


def _feed_for(context: OrganizationAccessContext, organization, filters, *, limit, cursor, latest_signal_mode=None):
    """The C9 feed inside this membership's visibility. ``latest_signal_mode`` is passed through to C9 unchanged."""
    filters = filters or FeedFilters()
    if not isinstance(filters, FeedFilters):
        raise OrganizationAccessDenied()
    if filters.radar_ids:
        # A foreign Radar id is refused exactly like a missing one: nothing about the other tenant leaks.
        owned = organization_radars_for(context).filter(pk__in=list(filters.radar_ids)).count()
        if owned != len(set(filters.radar_ids)):
            raise OrganizationAccessDenied()
    if can(context, Capability.VIEW_ALL_OPPORTUNITIES):
        return get_opportunity_feed(organization, filters, limit=limit, cursor=cursor,
                                    latest_signal_mode=latest_signal_mode)
    if can(context, Capability.VIEW_ASSIGNED_OPPORTUNITIES):
        # Only this membership's assignments, restricted inside C9's SQL before cards are aggregated.
        return get_opportunity_feed(organization, filters, limit=limit, cursor=cursor,
                                    assigned_to_membership_id=context.membership_id,
                                    latest_signal_mode=latest_signal_mode)
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
    "company__name", "company__trade_names", "assigned_to_id", "assigned_at", "assigned_to__user__first_name",
    "assigned_to__user__last_name", "assigned_to__role", "assigned_to__user__is_active",
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
        .select_related("radar", "latest_signal", "company", "assigned_to__user").only(*_PAGE_FIELDS)
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
    assigning = can(context, Capability.ASSIGN_OPPORTUNITIES)
    assignees = tuple(assignable_members_for(context)) if assigning else ()
    assign_actions = {row.pk: bool(assignees) and row.status in ASSIGN_ALLOWED_FROM | {ASSIGNED} for row in rows}
    assignments = {
        row.pk: (member_display_name(row.assigned_to.user.first_name, row.assigned_to.user.last_name,
                                     row.assigned_to_id), row.assigned_at)
        for row in rows if row.assigned_to_id is not None
    }
    # D32: every row here is already inside this membership's visibility scope (a sales user's are all their own).
    updating = can(context, Capability.UPDATE_OPPORTUNITY_STATUS)
    status_actions = {row.pk: status_targets_for(row.status) if updating else () for row in rows}
    # D33: the notes of exactly these authorized, LIVE-backed rows -- one bounded query, never one per row.
    notes, notes_truncated = _page_notes(context, [row.pk for row in rows])
    note_actions = {row.pk: can(context, Capability.ADD_OPPORTUNITY_NOTE) for row in rows}
    # D34: the tasks of the same rows (one bounded query) and, per row, what this member may do with them.
    tasks, tasks_truncated = _page_tasks(context, [row.pk for row in rows])
    task_actions = _page_task_actions(context, rows)
    # D35: the company's Do Not Contact state in this organization, and whether this member may apply it.
    do_not_contact = _page_do_not_contact(context, rows[0].company.gemi_number)
    unread = _unread_notification_count(context)  # D37: this membership's own unread count, read-only
    return build_company_opportunity_page(organization=organization, rows=rows, live_signal_counts=live_signal_counts,
                                          save_actions=save_actions, assign_actions=assign_actions,
                                          assignees=assignees, assignments=assignments, status_actions=status_actions,
                                          notes=notes, notes_truncated=notes_truncated, note_actions=note_actions,
                                          tasks=tasks, tasks_truncated=tasks_truncated, task_actions=task_actions,
                                          today=timezone.localdate(), do_not_contact=do_not_contact,
                                          unread_notifications=unread)


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
    with _mutation():
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
        _lock_memberships(context, (context.membership_id,))  # the audit actor must still be this membership
        previous = row.status
        row.status = SAVED
        row.save(update_fields=["status", "updated_at"])
        _audit(context, AUDIT.OPPORTUNITY_SAVED, company_id=row.company_id, opportunity_id=row.pk,
               previous_status=previous, new_status=SAVED)
    return SaveOpportunityResult(organization_id=row.organization_id, company_id=row.company_id,
                                 opportunity_id=row.pk, status=SAVED, changed=True)


# --- D31: Assign ------------------------------------------------------------------------------------
#
# §40: «Sales Manager -> assign lead -> salesperson», with assigned_to and assigned_at. One explicit opportunity is
# assigned to one SALES_USER membership of the same organization. From NEW, VIEWED or SAVED the first assignment
# moves the opportunity to ASSIGNED; while ASSIGNED it may be reassigned to another sales user, and the same one is
# a no-op without any write. Every later §39 state is refused and left untouched. There is no unassign. §40's
# activity line («Nikos assigned Company X to Maria») belongs to the audit log (item 36): nothing here writes
# history, and nothing notifies anyone (item 37).

ASSIGNED = "assigned"
ASSIGN_ALLOWED_FROM = frozenset({"new", "viewed", "saved"})
ASSIGNEE_ROLE = SALES_USER


@dataclass(frozen=True)
class Assignee:
    """A sales user who may receive an assignment: the membership id and a display name -- never an email."""

    membership_id: int
    display_name: str


def member_display_name(first_name: str, last_name: str, membership_id: int) -> str:
    """The name shown for a member. Usernames are email addresses in this product, so they are never used."""
    name = f"{first_name or ''} {last_name or ''}".strip()
    return name or f"Πωλητής #{membership_id}"


def assignable_members_for(context: OrganizationAccessContext):
    """The active SALES_USER members of this organization, for a membership that may assign. Scoped in SQL."""
    require(context, Capability.ASSIGN_OPPORTUNITIES)
    rows = (_model("OrganizationMember").objects
            .filter(organization_id=context.organization_id, role=ASSIGNEE_ROLE, user__is_active=True)
            .order_by("pk").values_list("pk", "user__first_name", "user__last_name"))
    return [Assignee(membership_id=pk, display_name=member_display_name(first, last, pk)) for pk, first, last in rows]


@dataclass(frozen=True)
class AssignmentResult:
    organization_id: int
    company_id: int
    opportunity_id: int
    status: str
    assigned_membership_id: int | None
    assigned_user_id: int | None
    previous_assigned_membership_id: int | None
    changed: bool  # False for the same-assignee no-op, which writes nothing


class AssignmentRefused(Exception):
    """The opportunity is visible to this member but cannot be assigned as asked. Nothing changed.

    ``reason`` is ``state`` (a later §39 state) or ``assignee`` (not an active sales user of this organization --
    one message for every such case, so a posted id reveals nothing about other organizations' members).
    """

    MESSAGES = {
        "state": "Η ευκαιρία δεν μπορεί να ανατεθεί από την τρέχουσα κατάστασή της.",
        "assignee": "Ο πωλητής που επιλέχθηκε δεν είναι διαθέσιμος για ανάθεση.",
    }

    def __init__(self, reason: str, result: AssignmentResult):
        super().__init__(self.MESSAGES[reason])
        self.reason = reason
        self.result = result


def _membership_id(value):
    """A posted membership id as a positive integer, or None -- never an exception that says why."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit() and not value.startswith("0"):
        return int(value)
    return None


def assign_authorized_opportunity(user, organization_id, opportunity_id, assignee_membership_id) -> AssignmentResult:
    """D31: assign one customer-visible opportunity to one sales user of the same organization.

    Access is decided exactly like Save: a nonexistent or foreign organization, a non-member, a role without
    ``assign_opportunities``, another tenant's, a nonexistent or a SHADOW-backed opportunity are all the same
    ``OrganizationAccessDenied``. Only then is the request itself judged (state, assignee), which may be refused
    with ``AssignmentRefused``. The opportunity is re-read under a lock and the assignee is validated inside the
    same transaction; PostgreSQL provides the row lock, SQLite only proves the behaviour.
    """
    organization = _organization_by_id(organization_id)
    context = require(get_organization_access_context(user, organization), Capability.ASSIGN_OPPORTUNITIES)
    with _mutation():
        row = (organization_opportunities_for(context)
               .filter(pk=_object_id(opportunity_id), latest_signal__mode=LIVE)
               .select_for_update(of=("self",))
               .only("pk", "organization_id", "company_id", "status", "assigned_to_id", "assigned_at").first())
        if row is None:
            raise OrganizationAccessDenied()
        previous = row.assigned_to_id

        def result(changed, membership_id, user_id, status):
            return AssignmentResult(organization_id=row.organization_id, company_id=row.company_id,
                                    opportunity_id=row.pk, status=status, assigned_membership_id=membership_id,
                                    assigned_user_id=user_id, previous_assigned_membership_id=previous,
                                    changed=changed)

        if row.status not in ASSIGN_ALLOWED_FROM and row.status != ASSIGNED:
            raise AssignmentRefused("state", result(False, previous, None, row.status))
        wanted = _membership_id(assignee_membership_id)
        assignee = None
        if wanted is not None:
            assignee = (_model("OrganizationMember").objects
                        .filter(pk=wanted, organization_id=row.organization_id, role=ASSIGNEE_ROLE,
                                user__is_active=True)
                        .only("pk", "user_id").first())
        if assignee is None:
            raise AssignmentRefused("assignee", result(False, previous, None, row.status))
        if row.status == ASSIGNED and previous == assignee.pk:
            return result(False, assignee.pk, assignee.user_id, ASSIGNED)  # the same assignment: no write at all
        # The audit actor must still be this membership, and the assignee (who will be notified) must still exist.
        if assignee.pk not in _lock_memberships(context, (context.membership_id, assignee.pk)):
            raise AssignmentRefused("assignee", result(False, previous, None, row.status))
        previous_status = row.status
        row.status = ASSIGNED
        row.assigned_to_id = assignee.pk
        row.assigned_at = timezone.now()
        row.save(update_fields=["status", "assigned_to", "assigned_at", "updated_at"])
        event = _audit(context, AUDIT.OPPORTUNITY_REASSIGNED if previous_status == ASSIGNED else AUDIT.OPPORTUNITY_ASSIGNED,
                       company_id=row.company_id, opportunity_id=row.pk, previous_status=previous_status,
                       new_status=ASSIGNED, previous_assignee_id=previous, new_assignee_id=assignee.pk)
        if assignee.pk != context.membership_id:  # nobody is told about their own action
            _model("OrganizationNotification").objects.create(
                organization_id=row.organization_id, recipient_id=assignee.pk, notification_type=NOTIFY.ASSIGNMENT,
                opportunity_id=row.pk, source_audit_event_id=event.pk)
    return result(True, assignee.pk, assignee.user_id, ASSIGNED)


# --- D32: Statuses ----------------------------------------------------------------------------------
#
# §39 lists the pipeline and nothing more: no transition graph, no terminal states, no order. D32 therefore
# imposes no funnel. It owns only the general sales statuses below; SAVED stays D30's action, ASSIGNED D31's,
# and DO_NOT_CONTACT is reserved for item 35, which brings suppression with it. NEW and VIEWED are never
# targets and nothing sets VIEWED on a read. Any non-terminal opportunity may move to any D32 target; a terminal
# one is final here (no reopen). Assignment is a separate field and survives every status change. Provenance
# is only the current status and updated_at until the audit log (item 36); nothing notifies anyone (item 37).

STATUS_TARGETS = ("contacted", "interested", "follow_up", "won", "lost", "not_relevant")  # also the UI order
STATUS_SOURCES = frozenset({"new", "viewed", SAVED, ASSIGNED, "contacted", "interested", "follow_up"})
TERMINAL_STATUSES = frozenset({"won", "lost", "not_relevant", "do_not_contact"})


def status_targets_for(status: str) -> tuple:
    """The D32 targets a row in ``status`` may move to: every target except the current one, none if terminal."""
    if status not in STATUS_SOURCES:
        return ()
    return tuple(target for target in STATUS_TARGETS if target != status)


@dataclass(frozen=True)
class StatusChangeResult:
    organization_id: int
    company_id: int
    opportunity_id: int
    previous_status: str
    current_status: str
    changed: bool  # False for the same-status no-op, which writes nothing


class StatusChangeRefused(Exception):
    """The opportunity is visible to this member but the requested status change is not allowed. Nothing changed.

    ``reason`` is ``target`` (not one of the D32 targets) or ``terminal`` (the opportunity is already final).
    """

    MESSAGES = {
        "target": "Η κατάσταση που επιλέχθηκε δεν είναι διαθέσιμη.",
        "terminal": "Η ευκαιρία βρίσκεται σε τελική κατάσταση και δεν αλλάζει.",
    }

    def __init__(self, reason: str, result: StatusChangeResult):
        super().__init__(self.MESSAGES[reason])
        self.reason = reason
        self.result = result


def set_authorized_opportunity_status(user, organization_id, opportunity_id, target_status) -> StatusChangeResult:
    """D32: move one customer-visible opportunity to one general sales status.

    Access is decided first and exactly like Save and Assign: a nonexistent or foreign organization, a non-member,
    a role without ``update_opportunity_status``, another tenant's, a nonexistent, a SHADOW-backed opportunity and
    -- for a sales user -- any opportunity not assigned to this membership are all the same
    ``OrganizationAccessDenied``. Only then is the request judged: an unknown or specialised target, or a terminal
    opportunity, is ``StatusChangeRefused``; the same status again is a no-op without any write. The row is
    re-read under a lock, so concurrent changes serialise on PostgreSQL (SQLite proves behaviour, not locking).
    """
    organization = _organization_by_id(organization_id)
    context = require(get_organization_access_context(user, organization), Capability.UPDATE_OPPORTUNITY_STATUS)
    with _mutation():
        row = (organization_opportunities_for(context)
               .filter(pk=_object_id(opportunity_id), latest_signal__mode=LIVE)
               .select_for_update(of=("self",)).only("pk", "organization_id", "company_id", "status").first())
        if row is None:
            raise OrganizationAccessDenied()
        previous = row.status

        def result(current, changed):
            return StatusChangeResult(organization_id=row.organization_id, company_id=row.company_id,
                                      opportunity_id=row.pk, previous_status=previous, current_status=current,
                                      changed=changed)

        target = target_status if isinstance(target_status, str) and target_status in STATUS_TARGETS else None
        if target is None:
            raise StatusChangeRefused("target", result(previous, False))
        if previous in TERMINAL_STATUSES:
            raise StatusChangeRefused("terminal", result(previous, False))
        if previous == target:
            return result(previous, False)  # the same status again: no write at all
        _lock_memberships(context, (context.membership_id,))  # the audit actor must still be this membership
        row.status = target
        row.save(update_fields=["status", "updated_at"])
        _audit(context, AUDIT.OPPORTUNITY_STATUS_CHANGED, company_id=row.company_id, opportunity_id=row.pk,
               previous_status=previous, new_status=target)
    return result(target, True)


# --- D33: Notes -------------------------------------------------------------------------------------
#
# §41: «opportunity_notes», «Προσωπικές/team notes», «Πάντα tenant-isolated»; §62: no customer ever sees another's
# notes. A note is written on one explicit opportunity by the exact membership in the authorized context, and read
# only together with an opportunity that membership may already read -- no separate "all notes" path exists.
# Append-only: no edit, no delete, no dedupe (the same text twice is two entries). Adding a note changes no status,
# assignment, score, feed or timeline, writes no audit entry (item 36) and notifies nobody (item 37).

NOTE_MAX_LENGTH = 4000
NOTES_PAGE_LIMIT = 50
FORMER_MEMBER_LABEL = "Πρώην μέλος"


@dataclass(frozen=True)
class NoteResult:
    organization_id: int
    company_id: int
    opportunity_id: int
    note_id: int | None
    author_membership_id: int | None


class NoteRefused(Exception):
    """The opportunity is visible and writable for this member, but the body is not a valid note. Nothing stored."""

    MESSAGES = {
        "blank": "Η σημείωση δεν μπορεί να είναι κενή.",
        "too_long": f"Η σημείωση ξεπερνά το όριο των {NOTE_MAX_LENGTH} χαρακτήρων.",
        "invalid": "Η σημείωση περιέχει χαρακτήρες που δεν επιτρέπονται.",
    }

    def __init__(self, reason: str, result: NoteResult):
        super().__init__(self.MESSAGES[reason])
        self.reason = reason
        self.result = result


def normalize_note_body(value):
    """(body, None) for a valid note, or (None, reason). Plain text: line endings become «\n», the ends are
    trimmed, inner lines and spacing are kept as typed. NUL cannot be stored by PostgreSQL, so it is refused."""
    if not isinstance(value, str):
        return None, "blank"
    body = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return None, "blank"
    if "\x00" in body:
        return None, "invalid"
    if len(body) > NOTE_MAX_LENGTH:
        return None, "too_long"
    return body, None


def add_authorized_opportunity_note(user, organization_id, opportunity_id, body) -> NoteResult:
    """D33: append one note to one customer-visible opportunity, authored by this very membership.

    Access is decided first and like every Phase D action: a nonexistent or foreign organization, a non-member, a
    role without ``add_opportunity_note``, another tenant's, a nonexistent or SHADOW-backed opportunity and -- for
    a sales user -- one not assigned to this membership are all the same ``OrganizationAccessDenied``. Only then is
    the body judged (``NoteRefused``). Organization, opportunity and author all come from the authorized context and
    row, never from the request.

    Membership deletion race: the context was resolved before the transaction, so the author is never taken from
    it blindly. Inside the one transaction the opportunity is locked first and then the *exact* acting membership
    (same id, organization, user and role) is re-read ``FOR UPDATE``; a membership that is gone or changed is the
    same ``OrganizationAccessDenied``, never a note with ``author=NULL``. Order -- opportunity, then membership --
    is the order every Phase D service locks in and the order Django's ``SET_NULL`` collector touches rows when a
    membership is deleted (referencing rows first, the member row last), so the two cannot deadlock. Either the
    note commits while the membership is locked and a later deletion clears its author, or the deletion commits
    first and the note is denied. As defence in depth, a foreign-key failure at commit (PostgreSQL checks these
    constraints when the transaction ends) is also turned into the same denial rather than a server error. SQLite
    serialises writers, so its tests prove the behaviour, not the row locks.
    """
    organization = _organization_by_id(organization_id)
    context = require(get_organization_access_context(user, organization), Capability.ADD_OPPORTUNITY_NOTE)
    try:
        with transaction.atomic():
            row = (organization_opportunities_for(context)
                   .filter(pk=_object_id(opportunity_id), latest_signal__mode=LIVE)
                   .select_for_update(of=("self",)).only("pk", "organization_id", "company_id").first())
            if row is None or row.organization_id != context.organization_id:
                raise OrganizationAccessDenied()
            author = (_model("OrganizationMember").objects.select_for_update()
                      .filter(pk=context.membership_id, organization_id=context.organization_id,
                              user_id=context.user_id, role=context.role)
                      .values_list("pk", flat=True).first())
            if author is None:
                raise OrganizationAccessDenied()  # removed or changed since the context was resolved
            text, reason = normalize_note_body(body)
            if reason is not None:
                raise NoteRefused(reason, NoteResult(organization_id=row.organization_id, company_id=row.company_id,
                                                     opportunity_id=row.pk, note_id=None, author_membership_id=None))
            note = _model("OpportunityNote").objects.create(organization_id=row.organization_id,
                                                            opportunity_id=row.pk, author_id=author, body=text)
            _audit(context, AUDIT.NOTE_ADDED, company_id=row.company_id, opportunity_id=row.pk, note_id=note.pk)
    except IntegrityError:
        raise OrganizationAccessDenied()
    return NoteResult(organization_id=row.organization_id, company_id=row.company_id, opportunity_id=row.pk,
                      note_id=note.pk, author_membership_id=author)


def _page_notes(context: OrganizationAccessContext, opportunity_ids):
    """The newest ``NOTES_PAGE_LIMIT`` notes across these already-authorized opportunities, grouped by opportunity:
    ({opportunity_id: ((note_id, body, created_at, author display name), ...)}, truncated). One query."""
    if not opportunity_ids:
        return {}, False
    rows = list(_model("OpportunityNote").objects
                .filter(organization_id=context.organization_id, opportunity_id__in=opportunity_ids)
                .order_by("-created_at", "-pk")
                .values_list("pk", "opportunity_id", "body", "created_at", "author_id", "author__user__first_name",
                             "author__user__last_name")[:NOTES_PAGE_LIMIT + 1])
    grouped = {}
    for pk, opportunity_id, text, created_at, author_id, first, last in rows[:NOTES_PAGE_LIMIT]:
        author = member_display_name(first, last, author_id) if author_id is not None else FORMER_MEMBER_LABEL
        grouped.setdefault(opportunity_id, []).append((pk, text, created_at, author))
    return {key: tuple(value) for key, value in grouped.items()}, len(rows) > NOTES_PAGE_LIMIT


# --- D34: Tasks -------------------------------------------------------------------------------------
#
# §42 «tasks»: «Call tomorrow / Follow up Friday / Check again next week», «Δεν χτίζουμε full project management
# system». One task is a title and a due *date* on one explicit opportunity; the only mutations are create and
# complete. No edit, delete, cancel or reopen; no reminder or TASK_DUE notification (item 37); no audit row (item
# 36); no status, assignment, score, note, timeline or feed side effect. «Overdue» is derived when read, never
# stored. Reading tasks follows the opportunity. Mutating them needs ``manage_opportunity_tasks``; a sales user
# additionally completes only the tasks assigned to their own membership.
#
# Locks, in one order for every task mutation: the opportunity, then the task (for completion), then the
# memberships involved in ascending id order. That is the order Django's SET_NULL collector touches rows when a
# membership is deleted (referencing opportunities, notes and tasks first, the member row last), so a concurrent
# deletion and a task mutation cannot deadlock: either the deletion commits first and the mutation is denied, or
# the mutation commits while the memberships are locked and the deletion then clears the references.

TASK_TITLE_MAX_LENGTH = 200
TASKS_PAGE_LIMIT = 50
TASK_UNASSIGNED_LABEL = "Χωρίς ανάθεση"
# Who may be a task's assignee: managers of the organization, or the one sales user the opportunity is assigned to.
TASK_MANAGER_ASSIGNEE_ROLES = frozenset({OWNER, SALES_MANAGER})
TASK_SALES_ASSIGNEE_ROLE = SALES_USER
_DUE_ON = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class TaskResult:
    organization_id: int
    company_id: int
    opportunity_id: int
    task_id: int | None
    assigned_membership_id: int | None
    completed: bool
    changed: bool  # False for completing an already completed task, which writes nothing


class TaskRefused(Exception):
    """The opportunity is visible and task-writable for this member, but the request is not a valid task.
    Nothing stored. One message covers every unusable assignee, so a posted id reveals nothing about members."""

    MESSAGES = {
        "title_blank": "Ο τίτλος της εργασίας δεν μπορεί να είναι κενός.",
        "title_too_long": f"Ο τίτλος της εργασίας ξεπερνά το όριο των {TASK_TITLE_MAX_LENGTH} χαρακτήρων.",
        "title_invalid": "Ο τίτλος της εργασίας περιέχει χαρακτήρες που δεν επιτρέπονται.",
        "due_invalid": "Η προθεσμία πρέπει να είναι έγκυρη ημερομηνία.",
        "due_past": "Η προθεσμία δεν μπορεί να είναι στο παρελθόν.",
        "assignee": "Το μέλος που επιλέχθηκε δεν μπορεί να αναλάβει αυτή την εργασία.",
    }

    def __init__(self, reason: str, result: TaskResult):
        super().__init__(self.MESSAGES[reason])
        self.reason = reason
        self.result = result


def normalize_task_title(value):
    """(title, None) or (None, reason): plain text, ends trimmed, inner spacing kept, 1-200 characters, no NUL."""
    if not isinstance(value, str):
        return None, "title_blank"
    title = value.strip()
    if not title:
        return None, "title_blank"
    if "\x00" in title:
        return None, "title_invalid"
    if len(title) > TASK_TITLE_MAX_LENGTH:
        return None, "title_too_long"
    return title, None


def parse_task_due_on(value, today):
    """(date, None) or (None, reason). Only the canonical YYYY-MM-DD form; today or later. No natural language."""
    if not isinstance(value, str) or not _DUE_ON.fullmatch(value.strip()):
        return None, "due_invalid"
    try:
        due = date.fromisoformat(value.strip())
    except ValueError:
        return None, "due_invalid"
    if due < today:
        return None, "due_past"
    return due, None


def task_is_overdue(completed_at, due_on, today) -> bool:
    """Derived, never stored: an open task whose due date is before today. A task due today is not overdue."""
    return completed_at is None and due_on < today


def _completes_any_task(context: OrganizationAccessContext) -> bool:
    """Managers complete every task on an opportunity they manage; a sales user only their own (checked per task)."""
    return can(context, Capability.MANAGE_OPPORTUNITY_TASKS) and can(context, Capability.VIEW_ALL_OPPORTUNITIES)


def _may_complete_task(context: OrganizationAccessContext, task_assigned_to_id) -> bool:
    if not can(context, Capability.MANAGE_OPPORTUNITY_TASKS):
        return False
    return _completes_any_task(context) or task_assigned_to_id == context.membership_id


def _eligible_task_assignee(member, opportunity_assigned_to_id) -> bool:
    """A locked membership row (as a dict) that may hold a task on this opportunity."""
    if member is None or not member["user__is_active"]:
        return False
    if member["role"] in TASK_MANAGER_ASSIGNEE_ROLES:
        return True
    return member["role"] in (TASK_SALES_ASSIGNEE_ROLE,) and member["pk"] == opportunity_assigned_to_id


def _lock_memberships(context: OrganizationAccessContext, ids):
    """Re-read and lock these memberships of the context's organization, in ascending id order (one query), and
    check that the acting one is still exactly the membership the context was issued for."""
    rows = (_model("OrganizationMember").objects.select_for_update(of=("self",))
            .filter(pk__in=sorted({i for i in ids if i is not None}), organization_id=context.organization_id)
            .order_by("pk").values("pk", "role", "user_id", "user__is_active"))
    locked = {row["pk"]: row for row in rows}
    actor = locked.get(context.membership_id)
    if actor is None or (actor["user_id"], actor["role"]) != (context.user_id, context.role):
        raise OrganizationAccessDenied()  # removed or changed since the context was resolved
    return locked


def _task_opportunity(context: OrganizationAccessContext, opportunity_id):
    row = (organization_opportunities_for(context)
           .filter(pk=_object_id(opportunity_id), latest_signal__mode=LIVE)
           .select_for_update(of=("self",)).only("pk", "organization_id", "company_id", "assigned_to_id").first())
    if row is None or row.organization_id != context.organization_id:
        raise OrganizationAccessDenied()
    return row


def create_authorized_opportunity_task(user, organization_id, opportunity_id, title, due_on,
                                       assignee_membership_id=None) -> TaskResult:
    """D34: create one open task on one customer-visible opportunity.

    Access first, like every Phase D action: a nonexistent or foreign organization, a non-member, a role without
    ``manage_opportunity_tasks``, another tenant's, a nonexistent or SHADOW-backed opportunity and -- for a sales
    user -- one not assigned to this membership are all the same ``OrganizationAccessDenied``. Then the request:
    title, due date (today or later, in the configured time zone) and assignee, each a ``TaskRefused``.

    Assignee: an active OWNER or SALES_MANAGER of the organization, or the sales user the opportunity is assigned
    to -- never anyone else, and a sales user only themselves. Omitted, it defaults to the opportunity's valid sales
    user, else to the acting member. The explicit value is re-validated here, never trusted from the form.
    """
    organization = _organization_by_id(organization_id)
    context = require(get_organization_access_context(user, organization), Capability.MANAGE_OPPORTUNITY_TASKS)
    explicit = assignee_membership_id is not None and assignee_membership_id != ""
    wanted = _membership_id(assignee_membership_id) if explicit else None
    try:
        with transaction.atomic():
            row = _task_opportunity(context, opportunity_id)

            def refused(reason):
                return TaskRefused(reason, TaskResult(
                    organization_id=row.organization_id, company_id=row.company_id, opportunity_id=row.pk,
                    task_id=None, assigned_membership_id=None, completed=False, changed=False))

            locked = _lock_memberships(context, (context.membership_id, wanted, row.assigned_to_id))
            text, reason = normalize_task_title(title)
            if reason is not None:
                raise refused(reason)
            due, reason = parse_task_due_on(due_on, timezone.localdate())
            if reason is not None:
                raise refused(reason)
            if explicit:
                if wanted is None:
                    raise refused("assignee")
                if not _completes_any_task(context) and wanted != context.membership_id:
                    raise refused("assignee")  # a sales user assigns only to themselves
                if not _eligible_task_assignee(locked.get(wanted), row.assigned_to_id):
                    raise refused("assignee")
                assignee = wanted
            else:
                salesperson = locked.get(row.assigned_to_id)
                if (salesperson is not None and salesperson["role"] in (TASK_SALES_ASSIGNEE_ROLE,)
                        and _eligible_task_assignee(salesperson, row.assigned_to_id)):
                    assignee = salesperson["pk"]
                else:
                    assignee = context.membership_id
            task = _model("OpportunityTask").objects.create(
                organization_id=row.organization_id, opportunity_id=row.pk, title=text, due_on=due,
                created_by_id=context.membership_id, assigned_to_id=assignee)
            _audit(context, AUDIT.TASK_CREATED, company_id=row.company_id, opportunity_id=row.pk, task_id=task.pk,
                   new_assignee_id=assignee)
    except IntegrityError:
        raise OrganizationAccessDenied()
    return TaskResult(organization_id=row.organization_id, company_id=row.company_id, opportunity_id=row.pk,
                      task_id=task.pk, assigned_membership_id=assignee, completed=False, changed=True)


def complete_authorized_opportunity_task(user, organization_id, opportunity_id, task_id) -> TaskResult:
    """D34: complete one open task of one customer-visible opportunity; completing it again writes nothing.

    The task is resolved only inside the authorized opportunity (organization + opportunity + task, never a global
    lookup), so a foreign, missing or mismatched task is the same ``OrganizationAccessDenied`` as everything else. A
    sales user may complete only a task assigned to their own membership; managers any task they can see.
    """
    organization = _organization_by_id(organization_id)
    context = require(get_organization_access_context(user, organization), Capability.MANAGE_OPPORTUNITY_TASKS)
    try:
        with transaction.atomic():
            row = _task_opportunity(context, opportunity_id)
            task = (_model("OpportunityTask").objects.select_for_update()
                    .filter(pk=_object_id(task_id), opportunity_id=row.pk, organization_id=row.organization_id)
                    .only("pk", "assigned_to_id", "completed_at", "completed_by_id").first())
            if task is None:
                raise OrganizationAccessDenied()
            _lock_memberships(context, (context.membership_id,))
            if not _may_complete_task(context, task.assigned_to_id):
                raise OrganizationAccessDenied()
            result = TaskResult(organization_id=row.organization_id, company_id=row.company_id,
                                opportunity_id=row.pk, task_id=task.pk, assigned_membership_id=task.assigned_to_id,
                                completed=True, changed=False)
            if task.completed_at is not None:
                return result  # already completed: no write, completer and time untouched
            task.completed_at = timezone.now()
            task.completed_by_id = context.membership_id
            task.save(update_fields=["completed_at", "completed_by"])
            _audit(context, AUDIT.TASK_COMPLETED, company_id=row.company_id, opportunity_id=row.pk, task_id=task.pk)
    except IntegrityError:
        raise OrganizationAccessDenied()
    return TaskResult(organization_id=result.organization_id, company_id=result.company_id,
                      opportunity_id=result.opportunity_id, task_id=result.task_id,
                      assigned_membership_id=result.assigned_membership_id, completed=True, changed=True)


def _page_tasks(context: OrganizationAccessContext, opportunity_ids):
    """Up to ``TASKS_PAGE_LIMIT`` tasks across these already-authorized opportunities, in one query: open tasks
    first (due date, creation, id), then completed ones (newest completion first). Grouped by opportunity."""
    if not opportunity_ids:
        return {}, False
    is_open = Q(completed_at__isnull=True)
    rows = list(_model("OpportunityTask").objects
                .filter(organization_id=context.organization_id, opportunity_id__in=opportunity_ids)
                .order_by(Case(When(is_open, then=Value(0)), default=Value(1)),
                          Case(When(is_open, then=F("due_on"))).asc(),
                          Case(When(is_open, then=F("created_at"))).asc(),
                          F("completed_at").desc(),
                          Case(When(is_open, then=F("id")), default=-F("id")).asc())
                .values_list("pk", "opportunity_id", "title", "due_on", "completed_at", "created_by_id",
                             "created_by__user__first_name", "created_by__user__last_name", "assigned_to_id",
                             "assigned_to__user__first_name", "assigned_to__user__last_name", "completed_by_id",
                             "completed_by__user__first_name", "completed_by__user__last_name")[:TASKS_PAGE_LIMIT + 1])

    def name(member_id, first, last, missing):
        return member_display_name(first, last, member_id) if member_id is not None else missing

    grouped = {}
    for (pk, opportunity_id, title, due_on, completed_at, creator, c_first, c_last, assignee, a_first, a_last,
         completer, d_first, d_last) in rows[:TASKS_PAGE_LIMIT]:
        grouped.setdefault(opportunity_id, []).append((
            pk, title, due_on, completed_at, name(assignee, a_first, a_last, TASK_UNASSIGNED_LABEL),
            name(creator, c_first, c_last, FORMER_MEMBER_LABEL),
            name(completer, d_first, d_last, FORMER_MEMBER_LABEL) if completed_at else None,
            completed_at is None and _may_complete_task(context, assignee)))
    return {key: tuple(value) for key, value in grouped.items()}, len(rows) > TASKS_PAGE_LIMIT


def _page_task_actions(context: OrganizationAccessContext, rows):
    """Per row: None when this member cannot create tasks; otherwise (assignee options, default assignee id,
    default assignee name). Options are only offered to managers -- a sales user's tasks are always their own."""
    if not can(context, Capability.MANAGE_OPPORTUNITY_TASKS):
        return {row.pk: None for row in rows}
    managers = []
    if _completes_any_task(context):
        managers = [(pk, member_display_name(first, last, pk)) for pk, first, last in
                    _model("OrganizationMember").objects
                    .filter(organization_id=context.organization_id, role__in=TASK_MANAGER_ASSIGNEE_ROLES,
                            user__is_active=True)
                    .order_by("pk").values_list("pk", "user__first_name", "user__last_name")]
    own_name = dict(managers).get(context.membership_id)
    actions = {}
    for row in rows:
        salesperson = None
        if (row.assigned_to_id is not None and row.assigned_to.role in (TASK_SALES_ASSIGNEE_ROLE,)
                and row.assigned_to.user.is_active):
            salesperson = (row.assigned_to_id, member_display_name(row.assigned_to.user.first_name,
                                                                   row.assigned_to.user.last_name, row.assigned_to_id))
        if not _completes_any_task(context):
            # A sales user only ever sees rows assigned to them: the task is theirs, with no selector.
            actions[row.pk] = ((), context.membership_id, salesperson[1] if salesperson else "")
            continue
        options = tuple(managers) + ((salesperson,) if salesperson else ())
        default = salesperson or (context.membership_id, own_name or member_display_name("", "", context.membership_id))
        actions[row.pk] = (options, default[0], default[1])
    return actions


# --- D35: Do Not Contact ----------------------------------------------------------------------------
#
# §51 «contact_suppressions»: organization-scoped, «Πρέπει να υπερισχύει οποιουδήποτε AI/Radar». D35 v1 suppresses a
# *company* for one organization: contact_type "company", contact_value = the normalized GEMI number (no phone or
# email enters 2.0; Phase G adds those contact points). Applying it creates at most one suppression row and moves
# every opportunity this organization has for the company to DO_NOT_CONTACT -- the only path that ever sets that
# status (D32 refuses it as a target). No unsuppress, no reason edit, no expiry. Assignment, notes, tasks, score,
# signals and the feed's contents are untouched; other organizations are never affected. The enforcement read is
# ``is_contact_suppressed`` / ``is_company_suppressed`` (``gemiapp.contact_suppressions``, re-exported here): every
# future contact workflow must ask it first. C8 asks it too, so an opportunity created after the suppression is born
# DO_NOT_CONTACT.

DO_NOT_CONTACT = "do_not_contact"
# The reasons a company-level suppression may carry: §51's list without «email unsubscribe», because one address
# opting out must never suppress a whole company.
DNC_COMPANY_REASONS = ("explicit_objection", "call_objection", "compliance_registry", "manual")
DNC_REASON_LABELS = {
    "explicit_objection": "Ρητή αντίρρηση της εταιρείας",
    "call_objection": "Αντίρρηση σε τηλεφωνική επικοινωνία",
    "compliance_registry": "Μητρώο συμμόρφωσης",
    "manual": "Απόφαση του οργανισμού",
    "email_unsubscribe": "Απεγγραφή email",
}
DNC_SOURCE = "manual"


@dataclass(frozen=True)
class DoNotContactResult:
    organization_id: int
    company_id: int
    suppression_id: int | None
    created: bool                 # False when the suppression already existed (idempotent)
    opportunities_changed: int    # rows moved to DO_NOT_CONTACT by this request


class DoNotContactRefused(Exception):
    """The company is visible to this member, but the request cannot be applied. Nothing stored."""

    MESSAGES = {
        "confirm": "Επιβεβαιώστε ότι δεν θέλετε επικοινωνία με αυτή την εταιρεία.",
        "reason": "Επιλέξτε έγκυρο λόγο για τη μη επικοινωνία.",
        "identity": "Η εταιρεία δεν έχει έγκυρο αριθμό ΓΕΜΗ· η μη επικοινωνία δεν μπορεί να καταχωριστεί.",
    }

    def __init__(self, reason: str, result: DoNotContactResult):
        super().__init__(self.MESSAGES[reason])
        self.reason = reason
        self.result = result


def apply_authorized_company_do_not_contact(user, organization_id, company_id, reason, confirmed) -> DoNotContactResult:
    """D35: suppress one company for this organization and settle every one of its opportunities as DO_NOT_CONTACT.

    Access first: a nonexistent or foreign organization, a non-member, a role without ``manage_contact_suppressions``
    and a company without any customer-visible LIVE opportunity in this organization are all the same
    ``OrganizationAccessDenied``. Then the request: an explicit confirmation, one of the company-level reasons and a
    usable GEMI number, each a ``DoNotContactRefused``. Type, value, source and creator are derived here, never read
    from the request.

    One transaction, one lock order: the company row (``FOR NO KEY UPDATE``, the lock C8 also takes before it
    creates an opportunity, so a concurrent materialization either finishes first and is swept up here, or waits and
    then sees the suppression), every opportunity of the company in this organization (ascending id), the acting
    membership (re-validated), then the suppression insert -- the order Django's SET_NULL collector touches rows when a
    membership is deleted. Repeating is idempotent: an existing suppression is kept as it is (its reason,
    creator and time are never rewritten) and only rows not yet DO_NOT_CONTACT are updated, in one statement. A
    concurrent identical insert is caught by the unique constraint and converges on the existing row. SQLite proves
    the behaviour, not PostgreSQL's row locks.
    """
    organization = _organization_by_id(organization_id)
    context = require(get_organization_access_context(user, organization), Capability.MANAGE_CONTACT_SUPPRESSIONS)
    company_pk = _object_id(company_id)
    Suppression = _model("OrganizationContactSuppression")
    try:
        with transaction.atomic():
            # Nothing about the company is returned before access is decided below.
            _model("Company").objects.select_for_update(no_key=True).filter(pk=company_pk).values_list("pk").first()
            rows = list(organization_opportunities_for(context).filter(company_id=company_pk)
                        .select_for_update(of=("self",)).order_by("pk")
                        .values_list("pk", "status", "latest_signal__mode", "company__gemi_number"))
            if not any(mode == LIVE for _, _, mode, _ in rows):
                raise OrganizationAccessDenied()  # the entry point must be a customer-visible LIVE opportunity
            _lock_memberships(context, (context.membership_id,))

            def refused(why):
                return DoNotContactRefused(why, DoNotContactResult(
                    organization_id=context.organization_id, company_id=company_pk, suppression_id=None,
                    created=False, opportunities_changed=0))

            if confirmed is not True:
                raise refused("confirm")
            if not isinstance(reason, str) or reason not in DNC_COMPANY_REASONS:
                raise refused("reason")
            value = company_suppression_value(rows[0][3])
            if value is None:
                raise refused("identity")
            identity = dict(organization_id=context.organization_id, contact_type=SUPPRESSION_COMPANY,
                            contact_value=value)
            suppression_id = Suppression.objects.filter(**identity).values_list("pk", flat=True).first()
            created = False
            if suppression_id is None:
                try:
                    with transaction.atomic():
                        suppression_id = Suppression.objects.create(
                            **identity, reason=reason, source=DNC_SOURCE, created_by_id=context.membership_id).pk
                    created = True
                except IntegrityError:
                    suppression_id = Suppression.objects.filter(**identity).values_list("pk", flat=True).first()
                    if suppression_id is None:
                        raise
            pending = [pk for pk, status, _, _ in rows if status != DO_NOT_CONTACT]
            changed = 0
            if pending:
                changed = (_model("Opportunity").objects
                           .filter(pk__in=pending, organization_id=context.organization_id, company_id=company_pk)
                           .update(status=DO_NOT_CONTACT, updated_at=timezone.now()))
            if created or changed:
                # One company-level event, never one per row: the rows it settled are not named, so no sibling
                # opportunity is ever revealed through the history.
                _audit(context, AUDIT.SUPPRESSION_ADDED if created else AUDIT.SUPPRESSION_REAPPLIED,
                       company_id=company_pk, suppression_id=suppression_id,
                       reason=reason if created else "", new_status=DO_NOT_CONTACT)
    except IntegrityError:
        raise OrganizationAccessDenied()
    return DoNotContactResult(organization_id=context.organization_id, company_id=company_pk,
                              suppression_id=suppression_id, created=created, opportunities_changed=changed)


def _page_do_not_contact(context: OrganizationAccessContext, gemi_number):
    """The page's view of the company's suppression in this organization: (state, reason label, created_at, reasons).
    ``state`` is "suppressed", "available" (this member may apply it), "unavailable" (no GEMI identity) or None."""
    value = company_suppression_value(gemi_number)
    existing = None
    if value is not None:
        existing = (_model("OrganizationContactSuppression").objects
                    .filter(organization_id=context.organization_id, contact_type=SUPPRESSION_COMPANY,
                            contact_value=value)
                    .values_list("reason", "created_at").first())
    if existing is not None:
        return ("suppressed", DNC_REASON_LABELS.get(existing[0], existing[0]), existing[1], ())
    if not can(context, Capability.MANAGE_CONTACT_SUPPRESSIONS):
        return (None, "", None, ())
    if value is None:
        return ("unavailable", "", None, ())
    return ("available", "", None, tuple((reason, DNC_REASON_LABELS[reason]) for reason in DNC_COMPANY_REASONS))


# --- D36: Audit log ---------------------------------------------------------------------------------
#
# §52 «Κάθε σημαντική ενέργεια: actor, organization, action, entity, timestamp, metadata». Every Phase D mutation that
# actually changes state writes exactly one OrganizationAuditEvent inside its own transaction (``_audit``); refused and
# no-op calls write none. The actor is the membership re-validated under lock in that same transaction. Reading
# follows D29 visibility: the company's events for the opportunities this membership may see (never a sibling row a
# sales user cannot see), plus the company-level Do Not Contact events that name no opportunity. Not the B6 timeline,
# not note or task content, no notification (item 37).

class _AuditActions:
    OPPORTUNITY_SAVED = "opportunity_saved"
    OPPORTUNITY_ASSIGNED = "opportunity_assigned"
    OPPORTUNITY_REASSIGNED = "opportunity_reassigned"
    OPPORTUNITY_STATUS_CHANGED = "opportunity_status_changed"
    NOTE_ADDED = "note_added"
    TASK_CREATED = "task_created"
    TASK_COMPLETED = "task_completed"
    SUPPRESSION_ADDED = "suppression_added"
    SUPPRESSION_REAPPLIED = "suppression_reapplied"


AUDIT = _AuditActions
# Events that concern the company as a whole and name no opportunity.
AUDIT_COMPANY_LEVEL_ACTIONS = frozenset({AUDIT.SUPPRESSION_ADDED, AUDIT.SUPPRESSION_REAPPLIED})
AUDIT_PAGE_LIMIT = 50
AUDIT_ACTION_LABELS = {
    AUDIT.OPPORTUNITY_SAVED: "Αποθήκευση ευκαιρίας",
    AUDIT.OPPORTUNITY_ASSIGNED: "Ανάθεση ευκαιρίας",
    AUDIT.OPPORTUNITY_REASSIGNED: "Επανανάθεση ευκαιρίας",
    AUDIT.OPPORTUNITY_STATUS_CHANGED: "Ενημέρωση κατάστασης",
    AUDIT.NOTE_ADDED: "Νέα σημείωση",
    AUDIT.TASK_CREATED: "Νέα εργασία",
    AUDIT.TASK_COMPLETED: "Ολοκλήρωση εργασίας",
    AUDIT.SUPPRESSION_ADDED: "Καταχώριση μη επικοινωνίας",
    AUDIT.SUPPRESSION_REAPPLIED: "Επανεφαρμογή μη επικοινωνίας",
}


@contextmanager
def _mutation():
    """One mutation transaction for the Phase D services: a foreign-key failure at commit (a membership removed
    concurrently) is the same safe denial as everything else, never a server error."""
    try:
        with transaction.atomic():
            yield
    except IntegrityError:
        raise OrganizationAccessDenied()


def _audit(context: OrganizationAccessContext, action: str, *, company_id, **details):
    """Write one audit event for a change that has just happened in the caller's transaction. Only typed references
    and what changed; the actor is the context's membership, already re-validated under lock by the caller."""
    return _model("OrganizationAuditEvent").objects.create(organization_id=context.organization_id,
                                                           actor_id=context.membership_id, action=action,
                                                           company_id=company_id, **details)


@dataclass(frozen=True)
class AuditEntry:
    event_id: int
    action: str
    action_label: str
    actor_display_name: str
    created_at: object
    opportunity_id: int | None
    radar_name: str
    previous_status: str
    new_status: str
    previous_assignee_display_name: str
    new_assignee_display_name: str
    note_id: int | None
    task_id: int | None
    reason: str


def get_authorized_company_audit_events(user, organization_id, company_id, *, limit: int = AUDIT_PAGE_LIMIT) -> tuple:
    """D36 read model: the newest audit events of one company as this membership may see them (newest first, id as
    the tie-break, at most ``AUDIT_PAGE_LIMIT``). Access is exactly D29's: no visible LIVE opportunity of the company
    for this membership -> ``OrganizationAccessDenied``. One bounded query for the events."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= AUDIT_PAGE_LIMIT:
        raise OrganizationAccessDenied()
    organization = _organization_by_id(organization_id)
    context = get_organization_access_context(user, organization)
    company_pk = _object_id(company_id)
    visible = organization_opportunities_for(context).filter(company_id=company_pk, latest_signal__mode=LIVE)
    if not visible.exists():
        raise OrganizationAccessDenied()
    rows = (_model("OrganizationAuditEvent").objects
            .filter(organization_id=context.organization_id, company_id=company_pk)
            .filter(Q(opportunity_id__in=visible.values("pk"))
                    | Q(opportunity__isnull=True, action__in=AUDIT_COMPANY_LEVEL_ACTIONS))
            .order_by("-created_at", "-pk")
            .values_list("pk", "action", "created_at", "opportunity_id", "opportunity__radar__name", "previous_status",
                         "new_status", "note_id", "task_id", "reason",
                         "actor_id", "actor__user__first_name", "actor__user__last_name",
                         "previous_assignee_id", "previous_assignee__user__first_name",
                         "previous_assignee__user__last_name",
                         "new_assignee_id", "new_assignee__user__first_name", "new_assignee__user__last_name")[:limit])

    def name(member_id, first, last):
        return member_display_name(first, last, member_id) if member_id is not None else ""

    return tuple(AuditEntry(
        event_id=pk, action=action, action_label=AUDIT_ACTION_LABELS.get(action, action),
        actor_display_name=name(actor, a_first, a_last) or FORMER_MEMBER_LABEL, created_at=created_at,
        opportunity_id=opportunity_id, radar_name=radar_name or "", previous_status=previous_status,
        new_status=new_status, previous_assignee_display_name=name(prev, p_first, p_last),
        new_assignee_display_name=name(new, n_first, n_last), note_id=note_id, task_id=task_id, reason=reason,
    ) for (pk, action, created_at, opportunity_id, radar_name, previous_status, new_status, note_id, task_id, reason,
           actor, a_first, a_last, prev, p_first, p_last, new, n_first, n_last) in rows)


# --- D37: In-app notifications ----------------------------------------------------------------------
#
# §48: «notifications», types NEW_OPPORTUNITY, PRIORITY_SIGNAL, RADAR_MATCH, TASK_DUE, ASSIGNMENT, «Unread counter».
# Only ASSIGNMENT (above, in D31's transaction) and TASK_DUE (``gemiapp.notifications``, daily 08:00 Europe/Athens)
# are emitted; the three pipeline types are reserved. Everything here is recipient-scoped: (user, organization) ->
# the exact membership -> its own notifications, in SQL, never a global lookup. Reads never write; marking read is
# idempotent and writes no audit event. A notification whose opportunity this membership can no longer see is shown
# without its details or link, so a notification is never a way around G5.


class _NotificationTypes:
    NEW_OPPORTUNITY = "new_opportunity"
    PRIORITY_SIGNAL = "priority_signal"
    RADAR_MATCH = "radar_match"
    TASK_DUE = "task_due"
    ASSIGNMENT = "assignment"


NOTIFY = _NotificationTypes
NOTIFICATIONS_PAGE_LIMIT = 50
NOTIFICATION_TYPE_LABELS = {
    NOTIFY.NEW_OPPORTUNITY: "Νέα ευκαιρία",
    NOTIFY.PRIORITY_SIGNAL: "Σήμα προτεραιότητας",
    NOTIFY.RADAR_MATCH: "Ταίριασμα Radar",
    NOTIFY.TASK_DUE: "Προθεσμία εργασίας",
    NOTIFY.ASSIGNMENT: "Ανάθεση ευκαιρίας",
}


@dataclass(frozen=True)
class NotificationEntry:
    notification_id: int
    notification_type: str
    type_label: str
    created_at: object
    read_at: object | None
    available: bool              # the related opportunity is still visible to this membership
    company_id: int | None       # only when available
    company_name: str
    radar_name: str
    task_title: str              # TASK_DUE, only when available; the members' own text, escaped when rendered
    due_on: object | None


@dataclass(frozen=True)
class NotificationReadResult:
    organization_id: int
    changed: int                 # rows marked read by this call (0 for an idempotent repeat)


def _member_context(user, organization_id) -> OrganizationAccessContext:
    organization = _organization_by_id(organization_id)
    return require(get_organization_access_context(user, organization), Capability.VIEW_ORGANIZATION)


def _own_notifications(context: OrganizationAccessContext):
    return _model("OrganizationNotification").objects.filter(organization_id=context.organization_id,
                                                              recipient_id=context.membership_id)


def _unread_notification_count(context: OrganizationAccessContext) -> int:
    return _own_notifications(context).filter(read_at__isnull=True).count()


def get_authorized_unread_notification_count(user, organization_id) -> int:
    """This membership's unread notifications in this organization. One read-only COUNT; generates nothing."""
    return _unread_notification_count(_member_context(user, organization_id))


def get_authorized_notifications(user, organization_id, *, limit: int = NOTIFICATIONS_PAGE_LIMIT) -> tuple:
    """This membership's newest notifications (``-created_at, -id``, at most 50). Two bounded read-only queries: the
    notifications with their typed references, and which of their opportunities are still visible (LIVE, G5 scope)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= NOTIFICATIONS_PAGE_LIMIT:
        raise OrganizationAccessDenied()
    return _notification_entries(_member_context(user, organization_id), limit)


def _notification_entries(context: OrganizationAccessContext, limit: int) -> tuple:
    rows = list(_own_notifications(context).order_by("-created_at", "-pk")
                .values_list("pk", "notification_type", "created_at", "read_at", "due_on", "opportunity_id",
                             "opportunity__company_id", "opportunity__company__name", "opportunity__radar__name",
                             "task__title")[:limit])
    opportunity_ids = {row[5] for row in rows if row[5] is not None}
    visible = set()
    if opportunity_ids:
        visible = set(organization_opportunities_for(context)
                      .filter(pk__in=opportunity_ids, latest_signal__mode=LIVE).values_list("pk", flat=True))
    entries = []
    for pk, kind, created_at, read_at, due_on, opportunity_id, company_id, company_name, radar_name, title in rows:
        available = opportunity_id in visible
        entries.append(NotificationEntry(
            notification_id=pk, notification_type=kind, type_label=NOTIFICATION_TYPE_LABELS.get(kind, kind),
            created_at=created_at, read_at=read_at, available=available,
            company_id=company_id if available else None, company_name=company_name if available else "",
            radar_name=radar_name if available else "", task_title=(title or "") if available else "",
            due_on=due_on))
    return tuple(entries)


def mark_authorized_notification_read(user, organization_id, notification_id) -> NotificationReadResult:
    """Mark one of this membership's own notifications read. Resolved by organization + recipient + id in SQL; any
    other id is the same ``OrganizationAccessDenied``. Already read: no write, ``read_at`` unchanged."""
    context = _member_context(user, organization_id)
    mine = _own_notifications(context).filter(pk=_object_id(notification_id))
    found = mine.values_list("pk", "read_at").first()
    if found is None:
        raise OrganizationAccessDenied()
    if found[1] is not None:
        return NotificationReadResult(organization_id=context.organization_id, changed=0)  # already read: no write
    changed = mine.filter(read_at__isnull=True).update(read_at=timezone.now())
    return NotificationReadResult(organization_id=context.organization_id, changed=changed)


def mark_all_authorized_notifications_read(user, organization_id) -> NotificationReadResult:
    """Mark every unread notification of this membership in this organization read -- nothing of any other
    membership or organization. Zero unread: no write."""
    context = _member_context(user, organization_id)
    unread = _own_notifications(context).filter(read_at__isnull=True)
    changed = unread.update(read_at=timezone.now()) if unread.exists() else 0  # nothing unread: no write
    return NotificationReadResult(organization_id=context.organization_id, changed=changed)


# --- Customer workspace: navigation, dashboard and lists ---------------------------------------------------
#
# The normal customer UI of the Organization architecture. Every entry point here is a read-only GET helper built
# only from the pieces above: the organization comes from the route and is authorized against the membership
# (navigation reads the user's own memberships, and every link it offers carries its explicit organization id --
# nothing here picks a tenant for a request); visibility is the same SQL scope (a sales user sees only their own
# assignments); and, exactly as on the D29 page, only opportunities whose current capture rests on a LIVE signal
# ever reach a customer. Opportunity counts are companies -- one C9 card per company -- like the lists they open.

ROLE_LABELS = {OWNER: "Ιδιοκτήτης", ADMIN: "Διαχειριστής", SALES_MANAGER: "Διευθυντής πωλήσεων",
               SALES_USER: "Πωλητής", VIEWER: "Μόνο ανάγνωση"}
# Every §39 status that is not terminal: still being worked.
ACTIVE_OPPORTUNITY_STATUSES = ("new", "viewed", SAVED, ASSIGNED, "contacted", "interested", "follow_up")
ACTIVE_VIEW, ALL_VIEW = "active", "all"
WORKSPACE_PREVIEW_LIMIT = 5
WORKSPACE_TASK_LIMIT = 100


@dataclass(frozen=True)
class Workspace:
    organization_id: int
    name: str
    role_label: str
    can_view_radars: bool


@dataclass(frozen=True)
class WorkspaceNavigation:
    workspaces: tuple = ()
    current: Workspace | None = None
    current_is_route: bool = False   # ``current`` is the organization named by the route (not merely the only one)

    @property
    def home(self) -> Workspace | None:
        """Where a single workspace entry point leads (mobile navigation): the current one, else the first."""
        return self.current or (self.workspaces[0] if self.workspaces else None)


@dataclass(frozen=True)
class WorkspaceOpportunity:
    """One company card of the workspace lists, from its primary LIVE-backed opportunity."""

    company_id: int
    company_name: str
    gemi_number: str
    score: int
    score_class: str
    score_class_label: str
    status: str
    status_label: str
    reason_label: str
    radar_name: str
    other_radar_count: int
    assignee_name: str             # "" when the primary opportunity is not assigned
    latest_signal_label: str
    latest_signal_detected_at: object


@dataclass(frozen=True)
class WorkspaceTask:
    task_id: int
    title: str                     # the members' own plain text, escaped when rendered
    due_on: object
    overdue: bool
    due_today: bool
    company_id: int
    company_name: str
    radar_name: str
    assignee_name: str


@dataclass(frozen=True)
class WorkspaceDashboard:
    workspace: Workspace
    active_opportunities: int
    new_opportunities: int
    saved_opportunities: int
    open_tasks: int
    overdue_tasks: int
    unread_notifications: int
    radars: int | None             # None when the role may not read Radars
    active_radars: int | None
    top_opportunities: tuple       # WorkspaceOpportunity, active ones, in C9 feed order
    attention_tasks: tuple         # WorkspaceTask, open ones, earliest due date first
    recent_notifications: tuple    # NotificationEntry, newest first


@dataclass(frozen=True)
class WorkspaceOpportunityList:
    workspace: Workspace
    view: str
    view_label: str
    views: tuple                   # (value, label) choices of the status filter
    radar_id: int | None
    radar_options: tuple           # (id, name) of the organization's Radars; empty when the role may not read them
    rows: tuple
    next_cursor: str | None


@dataclass(frozen=True)
class WorkspaceTaskList:
    workspace: Workspace
    tasks: tuple
    truncated: bool


@dataclass(frozen=True)
class WorkspaceRadar:
    radar_id: int
    name: str
    active: bool
    score_threshold: int | None
    kads: int
    regions: int
    legal_forms: int
    signal_types: int
    exclusions: int
    opportunities: int             # companies with a visible LIVE-backed opportunity found by this Radar
    summary: tuple = ()            # (label, readable values) per criterion kind that the Radar uses


@dataclass(frozen=True)
class WorkspaceRadarList:
    workspace: Workspace
    radars: tuple
    can_manage: bool = False       # may create, edit and (de)activate Radars


def get_workspace_navigation(user, current_organization_id=None) -> WorkspaceNavigation:
    """The organizations this user is a member of and may use, for the navigation. One query over the user's own
    memberships, keeping only entitled organizations (an organization whose owner has no entitlement offers no
    link); a signed-out or inactive user has none. ``current`` is the route's organization when the user is its
    member, otherwise the user's only organization; with several and none named by the route there is no current
    one."""
    User = apps.get_model("auth", "User")
    if not isinstance(user, User) or user.pk is None or not user.is_active:
        return WorkspaceNavigation()
    workspaces = []
    for membership_id, organization_id, name, role in (
            _model("OrganizationMember").objects.filter(user_id=user.pk, role__in=tuple(ROLE_CAPABILITIES))
            .filter(organization__in=entitled_organizations())
            .order_by("organization__name", "organization_id")
            .values_list("pk", "organization_id", "organization__name", "role")):
        context = OrganizationAccessContext(organization_id=organization_id, user_id=user.pk,
                                            membership_id=membership_id, role=role, _issuer=_ISSUER)
        workspaces.append(Workspace(organization_id=organization_id, name=name, role_label=ROLE_LABELS[role],
                                    can_view_radars=can(context, Capability.VIEW_RADARS)))
    current = next((entry for entry in workspaces if entry.organization_id == current_organization_id), None)
    on_route = current is not None
    if current is None and len(workspaces) == 1:
        current = workspaces[0]
    return WorkspaceNavigation(workspaces=tuple(workspaces), current=current, current_is_route=on_route)


def _workspace_entry(user, organization_id):
    """(context, organization, workspace) for a member of the route's organization, or the single refusal."""
    organization = _organization_by_id(organization_id)
    context = require(get_organization_access_context(user, organization), Capability.VIEW_ORGANIZATION)
    return context, organization, Workspace(organization_id=organization.pk, name=organization.name,
                                            role_label=ROLE_LABELS[context.role],
                                            can_view_radars=can(context, Capability.VIEW_RADARS))


def _live_opportunities(context: OrganizationAccessContext):
    """The opportunities this membership may see whose current capture rests on a LIVE signal (the D29 rule)."""
    return organization_opportunities_for(context).filter(latest_signal__mode=LIVE)


def _open_tasks(context: OrganizationAccessContext):
    """Open tasks of the opportunities this membership may see (D34: tasks are read with their opportunity)."""
    return _model("OpportunityTask").objects.filter(organization_id=context.organization_id,
                                                    completed_at__isnull=True,
                                                    opportunity__in=_live_opportunities(context))


def _workspace_tasks(context: OrganizationAccessContext, limit: int, today):
    rows = list(_open_tasks(context).order_by("due_on", "created_at", "pk")
                .values_list("pk", "title", "due_on", "opportunity__company_id", "opportunity__company__name",
                             "opportunity__radar__name", "assigned_to_id", "assigned_to__user__first_name",
                             "assigned_to__user__last_name")[:limit + 1])
    tasks = tuple(
        WorkspaceTask(task_id=pk, title=title, due_on=due_on, overdue=task_is_overdue(None, due_on, today),
                      due_today=due_on == today, company_id=company_id, company_name=company_name,
                      radar_name=radar_name,
                      assignee_name=(member_display_name(first, last, assignee) if assignee is not None
                                     else TASK_UNASSIGNED_LABEL))
        for pk, title, due_on, company_id, company_name, radar_name, assignee, first, last in rows[:limit])
    return tasks, len(rows) > limit


def _workspace_opportunities(context: OrganizationAccessContext, organization, filters: FeedFilters, *, limit,
                             cursor):
    """One page of LIVE-backed C9 cards inside this membership's scope, with the company's name and the primary
    opportunity's assignee. A malformed cursor or filter is refused like everything else."""
    from .company_opportunity_page import REASON_LABELS, SCORE_CLASS_LABELS, SIGNAL_LABELS, STATUS_LABELS

    try:
        page = _feed_for(context, organization, filters, limit=limit, cursor=cursor, latest_signal_mode=LIVE)
    except FeedError:
        raise OrganizationAccessDenied() from None
    if not page.cards:
        return (), None
    names = dict(_model("Company").objects.filter(pk__in=[card.company_id for card in page.cards])
                 .values_list("pk", "name"))
    assignees = {pk: (member, first, last) for pk, member, first, last in
                 organization_opportunities_for(context)
                 .filter(pk__in=[card.primary_opportunity_id for card in page.cards])
                 .values_list("pk", "assigned_to_id", "assigned_to__user__first_name", "assigned_to__user__last_name")}
    rows = []
    for card in page.cards:
        primary = card.opportunities[0]
        member, first, last = assignees.get(card.primary_opportunity_id, (None, "", ""))
        rows.append(WorkspaceOpportunity(
            company_id=card.company_id, company_name=names.get(card.company_id, ""),
            gemi_number=card.company_gemi_number, score=card.score, score_class=card.score_class,
            score_class_label=SCORE_CLASS_LABELS.get(card.score_class, card.score_class), status=card.status,
            status_label=STATUS_LABELS.get(card.status, card.status),
            reason_label=REASON_LABELS.get(card.primary_reason_code, "—") if card.primary_reason_code else "—",
            radar_name=primary.radar_name, other_radar_count=card.opportunity_count - 1,
            assignee_name=member_display_name(first, last, member) if member is not None else "",
            latest_signal_label=SIGNAL_LABELS.get(primary.latest_signal_type, primary.latest_signal_type),
            latest_signal_detected_at=card.primary_signal_detected_at))
    return tuple(rows), page.next_cursor


def get_authorized_workspace_dashboard(user, organization_id) -> WorkspaceDashboard:
    """The organization's home screen as this membership may see it. Read-only, a bounded number of queries."""
    context, organization, workspace = _workspace_entry(user, organization_id)
    companies = _live_opportunities(context).aggregate(
        active=Count("company_id", distinct=True, filter=Q(status__in=ACTIVE_OPPORTUNITY_STATUSES)),
        new=Count("company_id", distinct=True, filter=Q(status="new")),
        saved=Count("company_id", distinct=True, filter=Q(status=SAVED)))
    today = timezone.localdate()
    tasks = _open_tasks(context).aggregate(open=Count("pk"), overdue=Count("pk", filter=Q(due_on__lt=today)))
    radars = active_radars = None
    if workspace.can_view_radars:
        counted = organization_radars_for(context).aggregate(total=Count("pk"), active=Count("pk", filter=Q(active=True)))
        radars, active_radars = counted["total"], counted["active"]
    top, _ = _workspace_opportunities(context, organization, FeedFilters(statuses=ACTIVE_OPPORTUNITY_STATUSES),
                                      limit=WORKSPACE_PREVIEW_LIMIT, cursor=None)
    attention, _ = _workspace_tasks(context, WORKSPACE_PREVIEW_LIMIT, today)
    return WorkspaceDashboard(
        workspace=workspace, active_opportunities=companies["active"], new_opportunities=companies["new"],
        saved_opportunities=companies["saved"], open_tasks=tasks["open"], overdue_tasks=tasks["overdue"],
        unread_notifications=_unread_notification_count(context), radars=radars, active_radars=active_radars,
        top_opportunities=top, attention_tasks=attention,
        recent_notifications=_notification_entries(context, WORKSPACE_PREVIEW_LIMIT))


def opportunity_list_views() -> tuple:
    """The status filter of the opportunity list: active (default), every §39 status, all."""
    from .company_opportunity_page import STATUS_LABELS

    return ((ACTIVE_VIEW, "Ενεργές"), *STATUS_LABELS.items(), (ALL_VIEW, "Όλες"))


def get_authorized_workspace_opportunities(user, organization_id, *, view=None, radar_id=None,
                                           cursor=None) -> WorkspaceOpportunityList:
    """The organization's opportunities as this membership may see them: LIVE-backed C9 cards, one per company,
    filtered by status view and optionally by one of the organization's own Radars. Unknown views fall back to the
    active ones; a foreign or missing Radar is the single refusal."""
    context, organization, workspace = _workspace_entry(user, organization_id)
    views = opportunity_list_views()
    labels = dict(views)
    view = view if view in labels else ACTIVE_VIEW
    statuses = (ACTIVE_OPPORTUNITY_STATUSES if view == ACTIVE_VIEW else () if view == ALL_VIEW else (view,))
    radar_ids = (_object_id(radar_id),) if radar_id is not None else ()
    radar_options = (tuple(organization_radars_for(context).order_by("name", "pk").values_list("pk", "name"))
                     if workspace.can_view_radars else ())
    rows, next_cursor = _workspace_opportunities(context, organization,
                                                 FeedFilters(statuses=statuses, radar_ids=radar_ids),
                                                 limit=DEFAULT_PAGE_SIZE, cursor=cursor)
    return WorkspaceOpportunityList(workspace=workspace, view=view, view_label=labels[view], views=views,
                                    radar_id=radar_id, radar_options=radar_options, rows=rows,
                                    next_cursor=next_cursor)


def get_authorized_workspace_tasks(user, organization_id) -> WorkspaceTaskList:
    """The open tasks of the opportunities this membership may see, earliest due date first (at most 100)."""
    context, _, workspace = _workspace_entry(user, organization_id)
    tasks, truncated = _workspace_tasks(context, WORKSPACE_TASK_LIMIT, timezone.localdate())
    return WorkspaceTaskList(workspace=workspace, tasks=tasks, truncated=truncated)


def get_authorized_workspace_radars(user, organization_id) -> WorkspaceRadarList:
    """The organization's Radars with their criteria counts, for roles that may read them. Read-only."""
    from .company_opportunity_page import SIGNAL_LABELS

    context, _, workspace = _workspace_entry(user, organization_id)
    radars = list(organization_radars_for(context).order_by("-active", "name", "pk")
                  .values_list("pk", "name", "active", "score_threshold"))
    ids = [row[0] for row in radars]

    def labels(model, *fields, render):
        """Readable criteria per Radar (one query per criteria table), in insertion order."""
        grouped = {}
        if ids:
            for row in (_model(model).objects.filter(radar_id__in=ids).order_by("radar_id", "pk")
                        .values_list("radar_id", *fields)):
                grouped.setdefault(row[0], []).append(render(*row[1:]))
        return grouped

    def kad(code, version, description=None):
        text = f"{code} ({version.replace('kad_', '')})" if version else code
        return f"{text} {description}".strip() if description else text

    criteria = {
        "kads": labels("OrganizationRadarKad", "kad__source_id", "kad__kad_version", "kad__description", render=kad),
        "regions": labels("OrganizationRadarRegion", "prefecture__description", "municipality__description",
                          render=lambda prefecture, municipality: prefecture or municipality or "—"),
        "legal_forms": labels("OrganizationRadarLegalForm", "legal_type__description",
                              render=lambda description: description or "—"),
        "signal_types": labels("OrganizationRadarSignalType", "signal_type",
                               render=lambda value: SIGNAL_LABELS.get(value, value)),
        "exclusions": labels("OrganizationRadarExclusion", "kad__source_id", "kad__kad_version",
                             "prefecture__description", "municipality__description", "legal_type__description",
                             render=lambda code, version, prefecture, municipality, legal: (
                                 kad(code, version) if code else prefecture or municipality or legal or "—")),
    }
    headings = (("kads", "ΚΑΔ"), ("regions", "Περιοχές"), ("legal_forms", "Νομικές μορφές"),
                ("signal_types", "Γεγονότα"), ("exclusions", "Εξαιρούνται"))
    opportunities = {}
    if ids:
        opportunities = dict(_live_opportunities(context).filter(radar_id__in=ids).order_by().values("radar_id")
                             .annotate(n=Count("company_id", distinct=True)).values_list("radar_id", "n"))
    return WorkspaceRadarList(workspace=workspace, can_manage=can(context, Capability.MANAGE_RADARS), radars=tuple(
        WorkspaceRadar(radar_id=pk, name=name, active=active, score_threshold=threshold,
                       opportunities=opportunities.get(pk, 0),
                       summary=tuple((heading, criteria[key][pk]) for key, heading in headings if criteria[key].get(pk)),
                       **{key: len(values.get(pk, ())) for key, values in criteria.items()})
        for pk, name, active, threshold in radars))


# --- Organization Radars: create, edit, activate ---------------------------------------------------------------
#
# The C3 domain service (``gemiapp.organization_radars``) owns every Radar rule and writes root and criteria in one
# transaction. This layer decides only who may call it: a member of the route's organization, whose organization is
# entitled (the context already requires that), holding ``manage_radars`` -- OWNER and ADMIN in the §64 table; a
# sales manager, sales user or viewer is the same refusal as a stranger. A Radar id is resolved only inside the
# organization, so another tenant's Radar is the single refusal too. Inside the one transaction the acting
# membership is re-read under lock (removed or changed since the context was resolved -> refusal). A domain rejection
# (``RadarError``) becomes ``RadarRefused`` and writes nothing. Configuration only: creating, editing or activating a
# Radar runs no matching, creates no signal, opportunity, notification or audit event and schedules nothing.


class RadarRefused(Exception):
    """The Radar definition broke a C3 rule. Nothing was written; ``error`` is the domain ``RadarError``."""

    def __init__(self, error):
        super().__init__(str(error))
        self.error = error


@dataclass(frozen=True)
class RadarEditor:
    workspace: Workspace
    radar_id: int | None          # None when creating
    definition: object | None     # the stored C3 RadarDefinition when editing


@dataclass(frozen=True)
class RadarWriteResult:
    organization_id: int
    radar_id: int
    name: str
    active: bool


def _radar_manager(user, organization_id):
    context, organization, workspace = _workspace_entry(user, organization_id)
    require(context, Capability.MANAGE_RADARS)
    return context, organization, workspace


def _organization_radar(context: OrganizationAccessContext, radar_id):
    radar = organization_radars_for(context).filter(pk=_object_id(radar_id)).first()
    if radar is None:
        raise OrganizationAccessDenied()
    return radar


def get_authorized_radar_editor(user, organization_id, radar_id=None) -> RadarEditor:
    """What the create/edit form starts from, for a member who may manage this organization's Radars."""
    from .organization_radars import get_organization_radar_definition

    context, organization, workspace = _radar_manager(user, organization_id)
    if radar_id is None:
        return RadarEditor(workspace=workspace, radar_id=None, definition=None)
    radar = _organization_radar(context, radar_id)
    return RadarEditor(workspace=workspace, radar_id=radar.pk,
                       definition=get_organization_radar_definition(organization, radar))


def _write_radar(user, organization_id, radar_id, write):
    from .organization_radars import RadarError

    context, organization, _ = _radar_manager(user, organization_id)
    radar = _organization_radar(context, radar_id) if radar_id is not None else None
    try:
        with _mutation():
            _lock_memberships(context, [context.membership_id])
            row = write(organization, radar)
    except RadarError as error:
        raise RadarRefused(error) from None
    return RadarWriteResult(organization_id=organization.pk, radar_id=row.pk, name=row.name, active=row.active)


def create_authorized_organization_radar(user, organization_id, definition) -> RadarWriteResult:
    """Create one Radar of this organization from a complete C3 definition (validated again by the domain)."""
    from .organization_radars import create_organization_radar

    return _write_radar(user, organization_id, None,
                        lambda organization, _: create_organization_radar(organization, definition))


def replace_authorized_organization_radar(user, organization_id, radar_id, definition) -> RadarWriteResult:
    """Replace the whole configuration of one of this organization's Radars, all-or-nothing."""
    from .organization_radars import replace_organization_radar

    return _write_radar(user, organization_id, radar_id,
                        lambda organization, radar: replace_organization_radar(organization, radar, definition))


def set_authorized_organization_radar_active(user, organization_id, radar_id, active: bool) -> RadarWriteResult:
    """Activate or deactivate one of this organization's Radars (an active one needs a positive criterion)."""
    from .organization_radars import set_organization_radar_active

    return _write_radar(user, organization_id, radar_id,
                        lambda organization, radar: set_organization_radar_active(organization, radar, active))
