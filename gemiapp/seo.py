"""Search engine surface: sitemap and robots.txt.

Only genuinely public pages belong here. Everything behind @login_required (dashboard, radars,
leads, company detail, settings, superadmin) is deliberately excluded and marked noindex, both
because crawlers cannot reach it and because it would leak nothing but login redirects.
"""

from django.contrib.sitemaps import Sitemap
from django.http import HttpResponse
from django.urls import reverse


class StaticViewSitemap(Sitemap):
    """The public marketing and entry pages."""

    protocol = "https"
    changefreq = "daily"

    def items(self):
        return ["home", "pricing", "signup", "login"]

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        return {"home": 1.0, "pricing": 0.9, "signup": 0.7}.get(item, 0.5)


SITEMAPS = {"static": StaticViewSitemap}


def robots_txt(request):
    """Allow the public pages, keep crawlers out of authenticated and machine endpoints."""
    sitemap_url = request.build_absolute_uri(reverse("sitemap"))
    lines = [
        "User-agent: *",
        "Allow: /$",
        "Allow: /pricing/",
        "Allow: /signup/",
        "Allow: /login/",
        "Disallow: /admin/",
        "Disallow: /superadmin/",
        "Disallow: /dashboard/",
        "Disallow: /settings/",
        "Disallow: /radars/",
        "Disallow: /leads/",
        "Disallow: /companies/",
        "Disallow: /api/",
        "Disallow: /export/",
        "Disallow: /verify/",
        "Disallow: /reset/",
        "Disallow: /unsubscribe/",
        "Disallow: /resend-verification/",
        "",
        f"Sitemap: {sitemap_url}",
    ]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain")
