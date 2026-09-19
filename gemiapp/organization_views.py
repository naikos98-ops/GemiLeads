"""Customer routes of the Organization architecture (Gemi Leads 2.0).

Every view here reaches organization-owned data **only** through ``gemiapp.organization_access``: the
organization comes from the route and is authorized there against the logged-in user's membership, never from a
session, middleware or the user. Every refusal becomes the same 404, whatever its reason, so a route never tells a
caller whether an organization, company or opportunity exists. The page is a GET-only read; the only mutations are
D30 Save, D31 Assign, D32 status changes and D33 notes, CSRF-protected POSTs that redirect back to the page.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from .organization_access import (
    AssignmentRefused, NoteRefused, OpportunityTransitionRefused, OrganizationAccessDenied, StatusChangeRefused,
    add_authorized_opportunity_note, assign_authorized_opportunity, get_authorized_company_opportunity_page,
    save_authorized_opportunity, set_authorized_opportunity_status,
)


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
