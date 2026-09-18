"""Customer routes of the Organization architecture (Gemi Leads 2.0).

Every view here reaches organization-owned data **only** through ``gemiapp.organization_access``: the
organization comes from the route and is authorized there against the logged-in user's membership, never from a
session, middleware or the user. Every refusal becomes the same 404, whatever its reason, so a route never tells a
caller whether an organization, company or opportunity exists. Views are GET-only reads; nothing here writes.
"""

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .organization_access import OrganizationAccessDenied, get_authorized_company_opportunity_page


@login_required
@require_GET
def company_opportunity_page(request, organization_id, company_id):
    """D29 / §36: one company, as one organization's member may see it."""
    try:
        page = get_authorized_company_opportunity_page(request.user, organization_id, company_id)
    except OrganizationAccessDenied:
        raise Http404()
    return render(request, "organizations/company_opportunity.html", {"page": page})
