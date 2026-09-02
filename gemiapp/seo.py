"""Search engine surface: sitemap and robots.txt.

Only genuinely public pages belong here. Everything behind @login_required (dashboard, radars,
leads, company detail, settings, superadmin) is deliberately excluded and marked noindex, both
because crawlers cannot reach it and because it would leak nothing but login redirects.
"""

from datetime import date

from django.contrib.sitemaps import Sitemap
from django.http import HttpResponse
from django.urls import reverse


class StaticViewSitemap(Sitemap):
    """The public marketing and entry pages.

    changefreq and priority are omitted deliberately: Google ignores both, and "daily" was
    inaccurate for static marketing pages. lastmod is hardcoded and must be bumped by hand
    when a page's visible content changes. Do not wire it to timezone.now() or to Company
    data: an always-fresh lastmod is the pattern Google learns to distrust.
    """

    protocol = "https"

    LAST_MODIFIED = {
        "home": date(2026, 9, 1),      # redesign: product-led hero, Features + trust strip removed
        "pricing": date(2026, 9, 1),   # headline rewrite, dark-mode + radius pass
        "signup": date(2026, 9, 1),    # headline rewrite, dark-mode fix on the help panel
        "login": date(2026, 9, 1),     # password-row markup fix
    }

    def items(self):
        return ["home", "pricing", "signup", "login"]

    def location(self, item):
        return reverse(item)

    def lastmod(self, item):
        return self.LAST_MODIFIED.get(item)


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
