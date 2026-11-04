"""Contact visibility, Reply-To on every user email, and the in-app «Βοήθεια» support form."""

import time as real_time
import types
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import caches
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from gemiapp.models import AdminAuditLog, DigestPreference, SupportMessage, UserSubscription
from gemiapp.services import _send_digest_email, send_verification_email_now
from gemiapp.support import (
    MAX_NOTIFY_ATTEMPTS,
    notification_subject,
    plan_label,
    schedule_retry,
    send_support_notification,
)
from gemiapp.tasks import send_support_notification_task

REPLY_TO = "info@gemileads.gr"
VALID = {"category": "billing", "message": "Πώς αλλάζω την κάρτα μου;", "page_url": "/settings/?tab=1"}


def _frozen_limiter_clock():
    """The limiter counts in fixed wall-clock windows; pin only its clock so a slow run cannot straddle one."""
    frozen = real_time.time()
    clock = types.SimpleNamespace(**{n: getattr(real_time, n) for n in dir(real_time) if not n.startswith("_")})
    clock.time = lambda: frozen
    return patch("django_ratelimit.core.time", clock)


@override_settings(EMAIL_REPLY_TO=REPLY_TO)
class ReplyToTests(TestCase):
    """A reply to any email we send a user reaches info@, not the unread notifications@ sender."""

    def setUp(self):
        caches["shared"].clear()
        mail.outbox = []

    def test_digest_email_has_reply_to(self):
        user = User.objects.create_user("d@example.gr", "d@example.gr", "StrongPass123")
        _send_digest_email(user, "Subject", "text", "<p>html</p>", "digest:1:2026-11-04:daily")
        self.assertEqual(mail.outbox[0].reply_to, [REPLY_TO])

    def test_daily_digest_send_path_has_reply_to(self):
        from datetime import date

        from gemiapp.models import Company
        from gemiapp.services import send_digests

        Company.objects.create(gemi_number="1", name="DIGEST TEST", incorporation_date=date(2026, 11, 4))
        user = User.objects.create_user("free@example.gr", "free@example.gr", "StrongPass123", is_active=True)
        DigestPreference.objects.filter(user=user).update(frequency="daily")
        send_digests(date(2026, 11, 4), "daily")
        self.assertTrue(mail.outbox)
        self.assertTrue(all(m.reply_to == [REPLY_TO] for m in mail.outbox))

    def test_verification_email_has_reply_to(self):
        user = User.objects.create_user("v@example.gr", "v@example.gr", "StrongPass123", is_active=False)
        self.assertTrue(send_verification_email_now(user.pk))
        self.assertEqual(mail.outbox[0].reply_to, [REPLY_TO])

    def test_password_reset_email_has_reply_to(self):
        User.objects.create_user("r@example.gr", "r@example.gr", "StrongPass123", is_active=True)
        self.client.post(reverse("password_reset"), {"email": "r@example.gr"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].reply_to, [REPLY_TO])
        self.assertTrue(mail.outbox[0].alternatives)  # the HTML part survives the override

    def test_allauth_email_has_reply_to(self):
        from gemiapp.adapters import NoLocalSignupAdapter

        message = NoLocalSignupAdapter().render_mail("account/email/password_reset_key", "a@example.gr", {
            "user": User(email="a@example.gr"), "password_reset_url": "https://x/", "request": None,
        })
        self.assertEqual(message.reply_to, [REPLY_TO])

    @override_settings(EMAIL_REPLY_TO="")
    def test_no_reply_to_when_unset(self):
        user = User.objects.create_user("d@example.gr", "d@example.gr", "StrongPass123")
        _send_digest_email(user, "Subject", "text", "<p>html</p>", "digest:1:2026-11-04:daily")
        self.assertEqual(mail.outbox[0].reply_to, [])


@override_settings(EMAIL_REPLY_TO=REPLY_TO)
class ContactVisibilityTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("c@example.gr", "c@example.gr", "StrongPass123", is_active=True)

    def test_signed_in_layout_shows_contact_and_help_button(self):
        self.client.force_login(self.user)
        for name in ("dashboard", "settings", "lead_list", "radar_list"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn(f'href="mailto:{REPLY_TO}"', html, name)
            self.assertIn("data-support-open", html, name)
            self.assertIn('id="support-dialog"', html, name)
            self.assertIn("Θα σου απαντήσουμε στο <b>c@example.gr</b>", html, name)

    def test_settings_page_states_the_contact_address(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("settings")).content.decode()
        self.assertIn("ΕΠΙΚΟΙΝΩΝΙΑ", html)
        self.assertIn(f"Επικοινωνία: <a href=\"mailto:{REPLY_TO}\">{REPLY_TO}</a>", html)

    def test_anonymous_pages_have_no_help_widget(self):
        html = self.client.get(reverse("home")).content.decode()
        self.assertNotIn('id="support-dialog"', html)
        self.assertNotIn("data-support-open", html)

    def test_dialog_is_accessible(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('aria-labelledby="support-title"', html)
        self.assertIn('aria-label="Κλείσιμο"', html)
        self.assertIn('aria-haspopup="dialog"', html)
        self.assertIn('role="alert"', html)
        for value in ("question", "problem", "billing", "suggestion"):
            self.assertIn(f'value="{value}"', html)


@override_settings(EMAIL_REPLY_TO=REPLY_TO, SUPPORT_INBOX="support-inbox@example.gr")
class SupportMessageTests(TestCase):

    def setUp(self):
        caches["shared"].clear()
        mail.outbox = []
        self.user = User.objects.create_user("p@example.gr", "p@example.gr", "StrongPass123", is_active=True)
        UserSubscription.objects.filter(user=self.user).update(tier="pro", status="active")
        self.client.force_login(self.user)

    def _post(self, data=VALID, fetch=True, **extra):
        headers = {"HTTP_X_REQUESTED_WITH": "fetch"} if fetch else {}
        with patch("django_q.tasks.async_task") as queued:
            response = self.client.post(reverse("support_message_create"), data,
                                        HTTP_USER_AGENT="TestAgent/1.0", **headers, **extra)
        return response, queued

    def test_post_stores_message_with_automatic_fields_and_queues_notification(self):
        response, queued = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        support = SupportMessage.objects.get()
        self.assertEqual((support.user, support.email, support.plan), (self.user, "p@example.gr", "Pro"))
        self.assertEqual(support.category, "billing")
        self.assertEqual(support.message, VALID["message"])
        self.assertEqual(support.page_url, "/settings/?tab=1")
        self.assertEqual(support.user_agent, "TestAgent/1.0")
        self.assertEqual(support.status, SupportMessage.STATUS_NEW)
        self.assertIsNotNone(support.created_at)
        queued.assert_called_once_with("gemiapp.tasks.send_support_notification_task", support.pk)
        self.assertEqual(mail.outbox, [])  # never sent inline from the request

    def test_message_length_bounds(self):
        for text, ok in (("x" * 9, False), ("x" * 10, True), ("x" * 2000, True), ("x" * 2001, False), ("   ", False)):
            caches["shared"].clear()
            response, _ = self._post({**VALID, "message": text})
            self.assertEqual(response.status_code == 200, ok, len(text))
        self.assertEqual(SupportMessage.objects.count(), 2)

    def test_unknown_category_is_refused(self):
        response, _ = self._post({**VALID, "category": "spam"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("category", response.json()["errors"])
        self.assertFalse(SupportMessage.objects.exists())

    def test_foreign_page_url_is_not_stored(self):
        for url in ("https://evil.example/x", "//evil.example/x", "javascript:alert(1)"):
            caches["shared"].clear()
            self._post({**VALID, "page_url": url})
        self.assertEqual(set(SupportMessage.objects.values_list("page_url", flat=True)), {""})

    def test_non_js_post_redirects_back_with_a_message(self):
        response, _ = self._post(fetch=False)
        self.assertRedirects(response, "/settings/?tab=1", fetch_redirect_response=False)
        self.assertEqual(SupportMessage.objects.count(), 1)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(reverse("support_message_create")).status_code, 405)

    def test_anonymous_user_is_redirected_to_login_and_nothing_is_stored(self):
        self.client.logout()
        response, queued = self._post()
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.assertFalse(SupportMessage.objects.exists())
        queued.assert_not_called()

    def test_csrf_is_enforced(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse("support_message_create"), VALID, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SupportMessage.objects.exists())

    def test_rate_limit_is_five_per_hour_per_user(self):
        with _frozen_limiter_clock():
            codes = [self._post()[0].status_code for _ in range(6)]
        self.assertEqual(codes, [200] * 5 + [429])
        self.assertEqual(SupportMessage.objects.count(), 5)

    def test_rate_limit_is_per_user(self):
        other = User.objects.create_user("o@example.gr", "o@example.gr", "StrongPass123", is_active=True)
        with _frozen_limiter_clock():
            for _ in range(5):
                self._post()
            self.client.force_login(other)
            self.assertEqual(self._post()[0].status_code, 200)

    def test_queue_failure_still_keeps_the_message(self):
        with patch("django_q.tasks.async_task", side_effect=Exception("broker down")),                 self.assertLogs("gemiapp.support", "ERROR"):
            response = self.client.post(reverse("support_message_create"), VALID, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SupportMessage.objects.count(), 1)


@override_settings(EMAIL_REPLY_TO=REPLY_TO, SUPPORT_INBOX="support-inbox@example.gr")
class SupportNotificationTests(TestCase):

    def setUp(self):
        mail.outbox = []
        self.user = User.objects.create_user("n@example.gr", "n@example.gr", "StrongPass123", is_active=True)
        self.support = SupportMessage.objects.create(
            user=self.user, email="n@example.gr", plan="Pro", category="problem",
            message="Δεν βλέπω τα leads μου σήμερα.", page_url="/leads/", user_agent="UA",
        )

    def test_notification_goes_to_inbox_with_user_reply_to_and_subject(self):
        self.assertTrue(send_support_notification_task(self.support.pk))
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ["support-inbox@example.gr"])
        self.assertEqual(email.reply_to, ["n@example.gr"])
        self.assertEqual(email.subject, "[Support][Pro] Πρόβλημα — n@example.gr")
        for fragment in ("Δεν βλέπω τα leads μου σήμερα.", "/leads/", "UA", f"id {self.user.pk}", "Pro"):
            self.assertIn(fragment, email.body)
        self.support.refresh_from_db()
        self.assertIsNotNone(self.support.notified_at)
        self.assertEqual(self.support.notify_attempts, 1)

    def test_notification_is_never_sent_twice(self):
        send_support_notification(self.support.pk)
        send_support_notification(self.support.pk)
        self.assertEqual(len(mail.outbox), 1)

    def test_smtp_failure_keeps_the_message_records_it_and_schedules_a_retry(self):
        from django_q.models import Schedule

        with patch("django.core.mail.EmailMessage.send", side_effect=ConnectionRefusedError("relay down")):
            with self.assertRaises(ConnectionRefusedError), self.assertLogs("gemiapp.support", "ERROR"):
                send_support_notification_task(self.support.pk)
        self.support.refresh_from_db()
        self.assertIsNone(self.support.notified_at)
        self.assertEqual(self.support.notify_attempts, 1)
        self.assertEqual(self.support.last_notify_error, "ConnectionRefusedError")
        retry = Schedule.objects.get(func="gemiapp.tasks.send_support_notification_task")
        self.assertEqual((retry.args, retry.schedule_type), (str(self.support.pk), Schedule.ONCE))
        self.assertGreater(retry.next_run, timezone.now())

        # The retry then succeeds and clears the error.
        self.assertTrue(send_support_notification_task(self.support.pk))
        self.support.refresh_from_db()
        self.assertIsNotNone(self.support.notified_at)
        self.assertEqual(self.support.last_notify_error, "")

    def test_retries_are_bounded(self):
        from django_q.models import Schedule

        with self.assertLogs("gemiapp.support", "ERROR"):
            self.assertFalse(schedule_retry(self.support.pk, MAX_NOTIFY_ATTEMPTS))
        self.assertFalse(Schedule.objects.filter(func="gemiapp.tasks.send_support_notification_task").exists())

    def test_resend_command_sends_only_pending(self):
        SupportMessage.objects.create(user=self.user, email="n@example.gr", plan="Pro", category="question",
                                      message="ήδη ειδοποιημένο", notified_at=timezone.now())
        out = StringIO()
        call_command("resend_support_notifications", stdout=out)
        self.assertIn("pending=1 sent=1 failed=0", out.getvalue())
        self.assertEqual(len(mail.outbox), 1)

    def test_plan_label(self):
        UserSubscription.objects.filter(user=self.user).update(tier="business", status="active")
        self.user.refresh_from_db()
        self.assertEqual(plan_label(self.user), "Business")
        free = User.objects.create_user("f@example.gr", "f@example.gr", "StrongPass123")
        self.assertEqual(plan_label(free), "Free")

    def test_subject_format(self):
        self.assertEqual(notification_subject(self.support), "[Support][Pro] Πρόβλημα — n@example.gr")


@override_settings(SUPERADMIN_EMAILS=["admin@example.gr"])
class SuperadminSupportTests(TestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser("admin@example.gr", "admin@example.gr", "StrongPass123")
        self.user = User.objects.create_user("u@example.gr", "u@example.gr", "StrongPass123", is_active=True)
        self.support = SupportMessage.objects.create(user=self.user, email="u@example.gr", plan="Free",
                                                     category="question", message="Πώς φτιάχνω Ραντάρ;")

    def test_superadmin_lists_messages_and_marks_answered(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("superadmin:support_list")).content.decode()
        self.assertIn("Πώς φτιάχνω Ραντάρ;", html)
        self.assertIn("Σήμανση ως απαντημένο", html)
        response = self.client.post(reverse("superadmin:support_mark_answered", args=[self.support.pk]))
        self.assertEqual(response.status_code, 302)
        self.support.refresh_from_db()
        self.assertEqual(self.support.status, SupportMessage.STATUS_ANSWERED)
        self.assertEqual(self.support.answered_by, self.admin)
        self.assertIsNotNone(self.support.answered_at)
        self.assertTrue(AdminAuditLog.objects.filter(action="SUPPORT_MARK_ANSWERED").exists())
        self.assertNotIn("Πώς φτιάχνω Ραντάρ;", self.client.get(reverse("superadmin:support_list")).content.decode())

    def test_regular_user_cannot_see_or_mark(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("superadmin:support_list")).status_code, 403)
        self.assertEqual(self.client.post(reverse("superadmin:support_mark_answered", args=[self.support.pk])).status_code, 403)
        self.support.refresh_from_db()
        self.assertEqual(self.support.status, SupportMessage.STATUS_NEW)

    def test_mark_answered_requires_post(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("superadmin:support_mark_answered", args=[self.support.pk])).status_code, 405)


class SupportPrivacyPolicyTests(TestCase):
    def test_privacy_policy_describes_support_messages(self):
        html = self.client.get(reverse("privacy")).content.decode()
        self.assertIn("Μηνύματα υποστήριξης", html)
        self.assertIn("Απάντηση σε αιτήματα υποστήριξης", html)
        self.assertIn("διαγράφονται μαζί με αυτόν", html)
