"""Keep the django.contrib.sites record in step with the deployment's own BASE_URL.

The sites framework (pulled in by allauth, SITE_ID = 1) is created by Django with the placeholder domain
``example.com`` and nothing in this project ever changed it. Everything that asks ``get_current_site()``
therefore got the placeholder: the sitemap emitted https://example.com/... URLs, Django's password-reset
email built its link from ``{{ domain }}``, and allauth prefixed its email subjects with "[example.com]".

The record is now written from settings.SITE_DOMAIN (the host of an explicitly configured BASE_URL) after
every ``migrate`` -- which Render already runs as its pre-deploy command -- so production, staging and a
developer's machine each describe themselves and nobody edits the row by hand.

What it deliberately does not touch:
- the row's id (SITE_ID). allauth scopes database-configured SocialApps by site *id*; this project configures
  Google/Apple in settings, and the OAuth callback URL is built from the request, not from this record.
- anything when SITE_DOMAIN is empty (BASE_URL not set) or a known placeholder: a misconfigured environment
  must leave an existing record alone rather than overwrite it with localhost or example.com.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

PLACEHOLDER_DOMAINS = frozenset({"example.com", "www.example.com", "example.org", "example.net"})


def sync_site_from_settings(sender=None, using="default", **kwargs):
    """post_migrate handler: make Site(SITE_ID) carry SITE_DOMAIN / SITE_NAME. Idempotent; returns what it did."""
    domain = (getattr(settings, "SITE_DOMAIN", "") or "").strip().lower()
    name = getattr(settings, "SITE_NAME", "") or domain
    if not domain:
        logger.info("SITE_DOMAIN is not configured (BASE_URL unset); the sites record was left unchanged.")
        return "skipped_unconfigured"
    if domain in PLACEHOLDER_DOMAINS:
        logger.warning("SITE_DOMAIN %r is a placeholder; the sites record was left unchanged.", domain)
        return "skipped_placeholder"

    from django.contrib.sites.models import Site

    site_id = getattr(settings, "SITE_ID", 1)
    site = Site.objects.using(using).filter(pk=site_id).first()
    if site is None:
        Site.objects.using(using).create(pk=site_id, domain=domain, name=name)
        result = "created"
    elif site.domain == domain and site.name == name:
        return "unchanged"
    else:
        previous = site.domain
        site.domain, site.name = domain, name
        site.save(using=using, update_fields=["domain", "name"])
        logger.info("Sites record %s: domain %r -> %r.", site_id, previous, domain)
        result = "updated"
    Site.objects.clear_cache()
    return result
