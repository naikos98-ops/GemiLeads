"""The django.contrib.sites record follows BASE_URL, so nothing public is built on example.com."""

import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from config.settings import site_domain_from_base_url

from .seo import StaticViewSitemap
from .site_identity import PLACEHOLDER_DOMAINS, sync_site_from_settings

PLACEHOLDER = re.compile(r"example\.(com|org|net)|localhost|127\.0\.0\.1")


class SiteDomainSettingTests(TestCase):
    def test_host_is_taken_from_an_explicit_base_url(self):
        self.assertEqual(site_domain_from_base_url("https://gemileads.gr"), "gemileads.gr")
        self.assertEqual(site_domain_from_base_url("https://GemiLeads.gr/"), "gemileads.gr")
        self.assertEqual(site_domain_from_base_url("http://localhost:8000"), "localhost:8000")

    def test_unset_or_malformed_base_url_yields_nothing(self):
        for value in ("", None, "gemileads.gr", "ftp://gemileads.gr", "https://gemileads.gr/app"):
            self.assertEqual(site_domain_from_base_url(value), "", value)


class SiteSyncTests(TestCase):
    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "example.com", "name": "example.com"})
        Site.objects.clear_cache()

    @override_settings(SITE_DOMAIN="gemileads.gr", SITE_NAME="Gemi Leads")
    def test_placeholder_record_is_replaced_and_keeps_its_id(self):
        self.assertEqual(sync_site_from_settings(), "updated")
        site = Site.objects.get_current()
        self.assertEqual((site.pk, site.domain, site.name), (settings.SITE_ID, "gemileads.gr", "Gemi Leads"))
        self.assertEqual(Site.objects.count(), 1)

    @override_settings(SITE_DOMAIN="gemileads.gr", SITE_NAME="Gemi Leads")
    def test_second_run_is_a_no_op(self):
        sync_site_from_settings()
        self.assertEqual(sync_site_from_settings(), "unchanged")

    @override_settings(SITE_DOMAIN="")
    def test_unconfigured_environment_leaves_the_record_alone(self):
        Site.objects.filter(pk=settings.SITE_ID).update(domain="gemileads.gr")
        self.assertEqual(sync_site_from_settings(), "skipped_unconfigured")
        self.assertEqual(Site.objects.get(pk=settings.SITE_ID).domain, "gemileads.gr")

    def test_placeholder_setting_is_refused(self):
        for placeholder in PLACEHOLDER_DOMAINS:
            with override_settings(SITE_DOMAIN=placeholder), self.assertLogs("gemiapp.site_identity", "WARNING"):
                self.assertEqual(sync_site_from_settings(), "skipped_placeholder")
        self.assertEqual(Site.objects.get(pk=settings.SITE_ID).domain, "example.com")

    @override_settings(SITE_DOMAIN="staging.gemileads.gr")
    def test_each_environment_names_itself(self):
        sync_site_from_settings()
        self.assertEqual(Site.objects.get_current().domain, "staging.gemileads.gr")

    @override_settings(SITE_DOMAIN="gemileads.gr")
    def test_missing_record_is_created(self):
        Site.objects.all().delete()
        Site.objects.clear_cache()
        self.assertEqual(sync_site_from_settings(), "created")
        self.assertEqual(Site.objects.get(pk=settings.SITE_ID).domain, "gemileads.gr")


@override_settings(SITE_DOMAIN="gemileads.gr", SITE_NAME="Gemi Leads")
class ProductionUrlTests(TestCase):
    """Once synced, the public URLs built from the Site record are canonical https://gemileads.gr/ URLs."""

    def setUp(self):
        Site.objects.update_or_create(pk=settings.SITE_ID, defaults={"domain": "example.com", "name": "example.com"})
        Site.objects.clear_cache()
        sync_site_from_settings()

    def test_sitemap_locations_are_canonical_and_never_placeholders(self):
        body = self.client.get("/sitemap.xml").content.decode()
        locations = re.findall(r"<loc>([^<]+)</loc>", body)
        self.assertEqual(len(locations), len(StaticViewSitemap().items()))
        for loc in locations:
            self.assertTrue(loc.startswith("https://gemileads.gr/"), loc)
            self.assertIsNone(PLACEHOLDER.search(loc), loc)

    def test_sitemap_keeps_every_entry_and_its_lastmod(self):
        body = self.client.get("/sitemap.xml").content.decode()
        pairs = dict(re.findall(r"<loc>https://gemileads\.gr([^<]+)</loc><lastmod>([^<]+)</lastmod>", body))
        expected = {reverse(name): day.isoformat() for name, day in StaticViewSitemap.LAST_MODIFIED.items()}
        self.assertEqual(pairs, expected)

    def test_password_reset_link_points_at_the_real_host(self):
        User.objects.create_user("reset@example.test", "reset@example.test", "Abcdef12345!")
        self.client.post(reverse("password_reset"), {"email": "reset@example.test"}, secure=True)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        bodies = [message.body] + [content for content, _ in message.alternatives]
        for body in bodies:
            self.assertIn("https://gemileads.gr/reset/", body)
            self.assertNotIn("example.com/reset", body)

    @override_settings(ALLOWED_HOSTS=["gemileads.gr"])
    def test_robots_points_at_the_sitemap_on_the_request_host(self):
        body = self.client.get("/robots.txt", HTTP_HOST="gemileads.gr", secure=True).content.decode()
        self.assertIn("Sitemap: https://gemileads.gr/sitemap.xml", body)


class ProductionConfigTests(TestCase):
    def test_render_sets_the_canonical_base_url(self):
        """The sync is only as good as the value it copies: production must declare https://gemileads.gr."""
        render = (Path(settings.BASE_DIR) / "render.yaml").read_text(encoding="utf-8")
        match = re.search(r"key: BASE_URL\s+value: (\S+)", render)
        self.assertIsNotNone(match)
        self.assertEqual(site_domain_from_base_url(match.group(1)), "gemileads.gr")
        hosts = re.search(r"key: DJANGO_ALLOWED_HOSTS\s+value: (\S+)", render).group(1).split(",")
        self.assertIn("gemileads.gr", hosts)
