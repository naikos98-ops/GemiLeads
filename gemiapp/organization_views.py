"""Customer routes of the Organization architecture (Gemi Leads 2.0).

Every view here reaches organization-owned data **only** through ``gemiapp.organization_access``: the
organization comes from the route and is authorized there against the logged-in user's membership, never from a
session, middleware or the user. Every refusal becomes the same 404, whatever its reason, so a route never tells a
caller whether an organization, company or opportunity exists. The page is a GET-only read; the only mutations are
D30 Save, D31 Assign, D32 status changes, D33 notes, D34 tasks and the company-level D35 Do Not Contact,
CSRF-protected POSTs that redirect back to the page. D37 adds the member's own notifications page (GET, read-only)
and its two self-scoped mark-read POSTs.

The customer workspace makes all of this reachable from the normal UI: the organization's dashboard, its
opportunity list (with the Saved view), its open tasks and its Radars -- four read-only GET pages -- and
``workspace_navigation``, the template context processor behind the workspace links in the product navigation. The
links are built from the user's own memberships, each carrying its explicit organization id; the processor is lazy,
so a page that never renders the product navigation (or a signed-out visitor) runs no query.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.functional import SimpleLazyObject
from django.views.decorators.http import require_GET, require_POST

from .organization_access import (
    AssignmentRefused, DoNotContactRefused, NoteRefused, OpportunityTransitionRefused, OrganizationAccessDenied,
    StatusChangeRefused, TaskRefused, add_authorized_opportunity_note, apply_authorized_company_do_not_contact,
    assign_authorized_opportunity, complete_authorized_opportunity_task, create_authorized_opportunity_task,
    get_authorized_company_opportunity_page, get_authorized_notifications, get_authorized_unread_notification_count,
    get_authorized_workspace_dashboard, get_authorized_workspace_opportunities, get_authorized_workspace_radars,
    get_authorized_workspace_tasks, get_workspace_navigation, mark_all_authorized_notifications_read,
    mark_authorized_notification_read, save_authorized_opportunity, set_authorized_opportunity_status,
)

# Which workspace section a route belongs to, for the navigation's active state.
WORKSPACE_SECTIONS = {
    "organization_dashboard": "dashboard",
    "organization_opportunities": "opportunities",
    "organization_company_opportunity": "opportunities",
    "organization_tasks": "tasks",
    "organization_radars": "radars",
    "organization_notifications": "notifications",
}


def workspace_navigation(request):
    """Template context processor: the signed-in user's organizations for the product navigation, and the active
    workspace section. Evaluated only when a template reads ``workspace_nav``."""
    match = getattr(request, "resolver_match", None)
    route_organization_id = match.kwargs.get("organization_id") if match else None
    section = WORKSPACE_SECTIONS.get(match.url_name, "") if match else ""
    if section == "opportunities" and match.url_name == "organization_opportunities"             and request.GET.get("status") == "saved":
        section = "saved"
    user = getattr(request, "user", None)
    return {"workspace_nav": SimpleLazyObject(lambda: get_workspace_navigation(user, route_organization_id)),
            "workspace_section": section}


def _optional_id(value):
    """A positive whole number from a query string, None when absent; anything else is a 404."""
    if value in (None, ""):
        return None
    if not (value.isascii() and value.isdigit()):
        raise Http404()
    return int(value)


@login_required
@require_GET
def workspace_dashboard(request, organization_id):
    """The organization's home screen: counts, top active opportunities, tasks needing attention, notifications."""
    try:
        dashboard = get_authorized_workspace_dashboard(request.user, organization_id)
    except OrganizationAccessDenied:
        raise Http404()
    return render(request, "organizations/dashboard.html", {"dashboard": dashboard})


@login_required
@require_GET
def workspace_opportunities(request, organization_id):
    """The organization's opportunities (one card per company), by status view and optionally by Radar."""
    try:
        listing = get_authorized_workspace_opportunities(
            request.user, organization_id, view=request.GET.get("status"),
            radar_id=_optional_id(request.GET.get("radar")), cursor=request.GET.get("cursor") or None)
    except OrganizationAccessDenied:
        raise Http404()
    return render(request, "organizations/opportunities.html", {"listing": listing})


@login_required
@require_GET
def workspace_tasks(request, organization_id):
    """The open tasks of the opportunities this member may see."""
    try:
        listing = get_authorized_workspace_tasks(request.user, organization_id)
    except OrganizationAccessDenied:
        raise Http404()
    return render(request, "organizations/tasks.html", {"listing": listing})


@login_required
@require_GET
def workspace_radars(request, organization_id):
    """The organization's Radars (read-only), for roles that may read them."""
    try:
        listing = get_authorized_workspace_radars(request.user, organization_id)
    except OrganizationAccessDenied:
        raise Http404()
    return render(request, "organizations/radars.html", {"listing": listing})


@login_required
@require_GET
def company_opportunity_page(request, organization_id, company_id):
    """D29 / §36: one company, as one organization's member may see it."""
    try:
        page = get_authorized_company_opportunity_page(request.user, organization_id, company_id)
    except OrganizationAccessDenied:
        raise Http404()
    return render(request, "organizations/company_opportunity.html", {"page": page})


@login_required
@require_POST
def save_opportunity(request, organization_id, opportunity_id):
    """D30: Save one explicit opportunity (NEW/VIEWED -> SAVED), then back to its company page.

    The destination is derived from the authorized opportunity, never from the request.
    """
    try:
        result = save_authorized_opportunity(request.user, organization_id, opportunity_id)
    except OrganizationAccessDenied:
        raise Http404()
    except OpportunityTransitionRefused as refused:
        messages.error(request, OpportunityTransitionRefused.MESSAGE)
        result = refused.result
    else:
        if result.changed:
            messages.success(request, "Η ευκαιρία αποθηκεύτηκε.")
    return redirect("organization_company_opportunity", organization_id=result.organization_id,
                    company_id=result.company_id)


@login_required
@require_POST
def assign_opportunity(request, organization_id, opportunity_id):
    """D31: assign one explicit opportunity to one sales user, then back to its company page.

    The posted member id is only a request: the service re-validates it against this organization's active sales
    users. The destination is derived from the authorized opportunity, never from the request.
    """
    try:
        result = assign_authorized_opportunity(request.user, organization_id, opportunity_id,
                                               request.POST.get("assignee_membership_id"))
    except OrganizationAccessDenied:
        raise Http404()
    except AssignmentRefused as refused:
        messages.error(request, str(refused))
        result = refused.result
    else:
        if result.changed:
            messages.success(request, "Η ευκαιρία ανατέθηκε.")
    return redirect("organization_company_opportunity", organization_id=result.organization_id,
                    company_id=result.company_id)


@login_required
@require_POST
def change_opportunity_status(request, organization_id, opportunity_id):
    """D32: move one explicit opportunity to one general sales status, then back to its company page.

    The posted status is only a request: the service accepts nothing outside its own target list. The destination
    is derived from the authorized opportunity, never from the request.
    """
    try:
        result = set_authorized_opportunity_status(request.user, organization_id, opportunity_id,
                                                   request.POST.get("status"))
    except OrganizationAccessDenied:
        raise Http404()
    except StatusChangeRefused as refused:
        messages.error(request, str(refused))
        result = refused.result
    else:
        if result.changed:
            messages.success(request, "Η κατάσταση της ευκαιρίας ενημερώθηκε.")
    return redirect("organization_company_opportunity", organization_id=result.organization_id,
                    company_id=result.company_id)


@login_required
@require_POST
def add_opportunity_note(request, organization_id, opportunity_id):
    """D33: append one note to one explicit opportunity, then back to its company page.

    Only ``body`` is read from the form; organization, opportunity and author come from the authorized context. The
    destination is derived from the authorized opportunity, never from the request.
    """
    try:
        result = add_authorized_opportunity_note(request.user, organization_id, opportunity_id,
                                                 request.POST.get("body"))
    except OrganizationAccessDenied:
        raise Http404()
    except NoteRefused as refused:
        messages.error(request, str(refused))
        result = refused.result
    else:
        messages.success(request, "Η σημείωση προστέθηκε.")
    return redirect("organization_company_opportunity", organization_id=result.organization_id,
                    company_id=result.company_id)


@login_required
@require_POST
def create_opportunity_task(request, organization_id, opportunity_id):
    """D34: create one task on one explicit opportunity, then back to its company page.

    Only ``title``, ``due_on`` and the optional ``assignee_membership_id`` are read from the form; everything else
    comes from the authorized context. The destination is derived from the authorized opportunity.
    """
    try:
        result = create_authorized_opportunity_task(request.user, organization_id, opportunity_id,
                                                    request.POST.get("title"), request.POST.get("due_on"),
                                                    request.POST.get("assignee_membership_id"))
    except OrganizationAccessDenied:
        raise Http404()
    except TaskRefused as refused:
        messages.error(request, str(refused))
        result = refused.result
    else:
        messages.success(request, "Η εργασία δημιουργήθηκε.")
    return redirect("organization_company_opportunity", organization_id=result.organization_id,
                    company_id=result.company_id)


@login_required
@require_POST
def complete_opportunity_task(request, organization_id, opportunity_id, task_id):
    """D34: complete one task of one explicit opportunity (again: no change), then back to its company page."""
    try:
        result = complete_authorized_opportunity_task(request.user, organization_id, opportunity_id, task_id)
    except OrganizationAccessDenied:
        raise Http404()
    if result.changed:
        messages.success(request, "Η εργασία ολοκληρώθηκε.")
    return redirect("organization_company_opportunity", organization_id=result.organization_id,
                    company_id=result.company_id)


@login_required
@require_POST
def company_do_not_contact(request, organization_id, company_id):
    """D35: suppress the whole company for this organization (every one of its opportunities becomes Do Not Contact),
    then back to the company page.

    Only ``reason`` and the ``confirm`` checkbox are read from the form; the suppressed identity, its source and
    creator are derived from the authorized context. The destination is derived from the route, never the request.
    """
    try:
        result = apply_authorized_company_do_not_contact(request.user, organization_id, company_id,
                                                         request.POST.get("reason"),
                                                         request.POST.get("confirm") == "yes")
    except OrganizationAccessDenied:
        raise Http404()
    except DoNotContactRefused as refused:
        messages.error(request, str(refused))
        result = refused.result
    else:
        if result.created or result.opportunities_changed:
            messages.success(request, "Η εταιρεία καταχωρίστηκε ως «Χωρίς επικοινωνία» για τον οργανισμό.")
    return redirect("organization_company_opportunity", organization_id=result.organization_id,
                    company_id=result.company_id)


@login_required
@require_GET
def notifications_page(request, organization_id):
    """D37: this membership's own in-app notifications in this organization. Read-only: nothing is generated here."""
    try:
        entries = get_authorized_notifications(request.user, organization_id)
        unread = get_authorized_unread_notification_count(request.user, organization_id)
    except OrganizationAccessDenied:
        raise Http404()
    return render(request, "organizations/notifications.html",
                  {"organization_id": organization_id, "entries": entries, "unread": unread})


@login_required
@require_POST
def mark_notification_read(request, organization_id, notification_id):
    """D37: mark one own notification read (already read: no change), then back to the notifications page."""
    try:
        result = mark_authorized_notification_read(request.user, organization_id, notification_id)
    except OrganizationAccessDenied:
        raise Http404()
    return redirect("organization_notifications", organization_id=result.organization_id)


@login_required
@require_POST
def mark_all_notifications_read(request, organization_id):
    """D37: mark every own unread notification in this organization read, then back to the notifications page."""
    try:
        result = mark_all_authorized_notifications_read(request.user, organization_id)
    except OrganizationAccessDenied:
        raise Http404()
    return redirect("organization_notifications", organization_id=result.organization_id)
