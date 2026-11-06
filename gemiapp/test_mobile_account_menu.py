"""Phone account menu: at <=900px .product-account is hidden, so email, SUPERADMIN (staff only) and logout live
behind one compact account button in the topbar. It must exist in both navigation modes (legacy and workspace)."""

import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .organizations import create_organization


def account_menu(html):
    match = re.search(r'<details class="product-account-menu".*?</details>', html, re.S)
    return match.group(0) if match else ""


class MobileAccountMenuTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("member@example.gr", "member@example.gr", "StrongPass123")

    def page(self, user, url=None):
        self.client.force_login(user)
        return self.client.get(url or reverse("dashboard")).content.decode()

    def test_regular_user_sees_email_and_logout_but_not_superadmin(self):
        menu = account_menu(self.page(self.user))
        self.assertTrue(menu)
        self.assertIn("member@example.gr", menu)
        self.assertIn(f'action="{reverse("logout")}"', menu)
        self.assertIn('method="post"', menu)
        self.assertIn("csrfmiddlewaretoken", menu)
        self.assertNotIn("SUPERADMIN", menu)
        self.assertNotIn("/superadmin/", menu)

    def test_staff_and_superuser_see_superadmin(self):
        staff = User.objects.create_user("staff@example.gr", "staff@example.gr", "x", is_staff=True)
        root = User.objects.create_superuser("root@example.gr", "root@example.gr", "x")
        for user in (staff, root):
            menu = account_menu(self.page(user))
            self.assertIn(reverse("superadmin:overview"), menu)
            self.assertIn("SUPERADMIN", menu)

    def test_menu_exists_in_workspace_navigation_too(self):
        org = create_organization(owner=self.user, name="Org").organization
        html = self.page(self.user, reverse("settings"))
        self.assertIn("product-rail--workspace", html)
        self.assertIn("member@example.gr", account_menu(html))
        self.assertIn(reverse("logout"), account_menu(html))
        self.assertTrue(org.pk)

    def test_anonymous_has_no_account_menu(self):
        html = self.client.get(reverse("home")).content.decode()
        self.assertEqual(account_menu(html), "")

    def test_logout_from_the_menu_ends_the_session(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("logout"))
        self.assertIn(response.status_code, (302, 303))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_css_shows_the_menu_only_on_phones(self):
        css = (Path(settings.BASE_DIR) / "static" / "css" / "product-ui.css").read_text(encoding="utf-8")
        base_rule = css.split(".product-account-menu {", 1)[1].split("}", 1)[0]
        self.assertIn("display: none", base_rule)
        phone = css.split("@media (max-width: 900px) {", 1)[1]
        self.assertIn(".product-account-menu {\n    display: block;", phone.split("@media", 1)[0])
