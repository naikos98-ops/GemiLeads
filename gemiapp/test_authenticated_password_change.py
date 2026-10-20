from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RATELIMIT_ENABLE=False,
    ACCOUNT_RATE_LIMITS={"change_password": None},
)
class AuthenticatedPasswordChangeTests(TestCase):
    old_password = "InitialPass123!"
    new_password = "SaferReplacement456!"

    def setUp(self):
        self.user = User.objects.create_user(
            username="password-user@example.com",
            email="password-user@example.com",
            password=self.old_password,
        )

    def login(self):
        self.client.force_login(self.user)

    def change_payload(self, **overrides):
        data = {
            "oldpassword": self.old_password,
            "password1": self.new_password,
            "password2": self.new_password,
        }
        data.update(overrides)
        return data

    def test_unauthenticated_user_cannot_open_change_password(self):
        response = self.client.get(reverse("account_change_password"))
        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('account_change_password')}",
        )

    def test_authenticated_password_user_can_open_greek_form(self):
        self.login()
        response = self.client.get(reverse("account_change_password"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Τρέχων κωδικός")
        self.assertContains(response, "Νέος κωδικός")
        self.assertContains(response, "Επιβεβαίωση νέου κωδικού")
        self.assertContains(response, 'autocomplete="current-password"')
        self.assertEqual(response.content.decode().count('autocomplete="new-password"'), 2)

    def test_settings_links_password_user_to_change_flow(self):
        self.login()
        response = self.client.get(reverse("settings"))
        self.assertContains(response, reverse("account_change_password"))
        self.assertContains(response, "Αλλαγή κωδικού")

    def test_wrong_current_password_is_rejected(self):
        self.login()
        response = self.client.post(
            reverse("account_change_password"),
            self.change_payload(oldpassword="WrongCurrent123!"),
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old_password))
        self.assertTrue(response.context["form"].errors["oldpassword"])

    def test_confirmation_mismatch_is_rejected(self):
        self.login()
        response = self.client.post(
            reverse("account_change_password"),
            self.change_payload(password2="DifferentReplacement789!"),
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old_password))
        self.assertTrue(response.context["form"].errors["password2"])

    def test_django_password_validators_are_enforced(self):
        self.login()
        response = self.client.post(
            reverse("account_change_password"),
            self.change_payload(password1="password", password2="password"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors["password1"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old_password))

    def test_valid_change_redirects_to_settings_and_shows_success(self):
        self.login()
        response = self.client.post(reverse("account_change_password"), self.change_payload(), follow=True)
        self.assertRedirects(response, reverse("settings"))
        self.assertContains(response, "Ο κωδικός σου άλλαξε επιτυχώς.")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.new_password))

    def test_old_password_stops_authenticating_after_change(self):
        self.login()
        self.client.post(reverse("account_change_password"), self.change_payload())
        self.assertIsNone(authenticate(username=self.user.username, password=self.old_password))

    def test_new_password_authenticates_after_change(self):
        self.login()
        self.client.post(reverse("account_change_password"), self.change_payload())
        self.assertEqual(authenticate(username=self.user.username, password=self.new_password), self.user)

    def test_current_session_remains_authenticated_after_change(self):
        self.login()
        self.client.post(reverse("account_change_password"), self.change_payload())
        response = self.client.get(reverse("settings"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.user.pk, self.user.pk)

    def test_get_request_never_changes_password(self):
        self.login()
        self.client.get(reverse("account_change_password"), self.change_payload())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old_password))

    def test_post_without_csrf_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse("account_change_password"), self.change_payload())
        self.assertEqual(response.status_code, 403)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old_password))

    def test_invalid_form_does_not_echo_password_values(self):
        self.login()
        response = self.client.post(
            reverse("account_change_password"), self.change_payload(oldpassword="WrongCurrent123!"),
        )
        self.assertNotContains(response, 'value="WrongCurrent123!"')
        self.assertNotContains(response, f'value="{self.new_password}"')


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RATELIMIT_ENABLE=False,
    ACCOUNT_RATE_LIMITS={"change_password": None},
)
class SocialOnlyPasswordSetTests(TestCase):
    new_password = "SocialPassword456!"

    def setUp(self):
        self.user = User.objects.create_user(
            username="social-only@example.com",
            email="social-only@example.com",
        )
        self.user.set_unusable_password()
        self.user.save(update_fields=["password"])
        self.social_account = SocialAccount.objects.create(
            user=self.user,
            provider="google",
            uid="google-social-only-123",
            extra_data={"email": self.user.email},
        )
        self.client.force_login(self.user)

    def test_settings_offers_set_password_without_current_password(self):
        response = self.client.get(reverse("settings"))
        self.assertContains(response, "Δεν έχεις ορίσει κωδικό πρόσβασης.")
        self.assertContains(response, reverse("account_set_password"))
        self.assertContains(response, "Ορισμός κωδικού")

    def test_change_route_redirects_social_only_user_to_set_route(self):
        response = self.client.get(reverse("account_change_password"))
        self.assertRedirects(response, reverse("account_set_password"))

    def test_social_only_set_form_has_no_current_password(self):
        response = self.client.get(reverse("account_set_password"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("oldpassword", response.context["form"].fields)
        self.assertContains(response, "Ορισμός κωδικού")
        self.assertEqual(response.content.decode().count('autocomplete="new-password"'), 2)

    def test_social_only_user_can_set_password_and_keep_session_and_link(self):
        response = self.client.post(
            reverse("account_set_password"),
            {"password1": self.new_password, "password2": self.new_password},
            follow=True,
        )
        self.assertRedirects(response, reverse("settings"))
        self.assertContains(response, "Ο κωδικός σου ορίστηκε επιτυχώς.")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.new_password))
        self.assertEqual(response.wsgi_request.user.pk, self.user.pk)
        self.assertTrue(
            SocialAccount.objects.filter(
                pk=self.social_account.pk,
                user=self.user,
                provider="google",
                uid="google-social-only-123",
            ).exists()
        )

    def test_password_user_cannot_use_set_password_without_current_password(self):
        self.user.set_password("NowHasPassword123!")
        self.user.save(update_fields=["password"])
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("account_set_password"),
            {"password1": self.new_password, "password2": self.new_password},
        )
        self.assertRedirects(response, reverse("account_change_password"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NowHasPassword123!"))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", RATELIMIT_ENABLE=False)
class ExistingPasswordResetRegressionTests(TestCase):
    def test_existing_forgot_password_flow_still_sends_its_original_reset_link(self):
        User.objects.create_user("reset-regression@example.com", "reset-regression@example.com", "StrongPass123!")
        response = self.client.post(reverse("password_reset"), {"email": "reset-regression@example.com"})
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertRegex(mail.outbox[0].body, r"/reset/[^\s]+/")
        self.assertNotIn("/accounts/password/change/", mail.outbox[0].body)
