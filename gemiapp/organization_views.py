"""Customer routes of the Organization architecture (Gemi Leads 2.0).

Every view here reaches organization-owned data **only** through ``gemiapp.organization_access``: the
organization comes from the route and is authorized there against the logged-in user's membership, never from a
session, middleware or the user. Every refusal becomes the same 404, whatever its reason, so a route never tells a
caller whether an organization, company or opportunity exists. The page is a GET-only read; the one mutation is
D30 Save, a CSRF-protected POST that redirects back to the page.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from .organization_access import (
    OpportunityTransitionRefused, OrganizationAccessDenied, get_authorized_company_opportunity_page,
    save_authorized_opportunity,
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
