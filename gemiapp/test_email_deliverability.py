"""Email deliverability suppression: an address Brevo reported as hard-bounced or blocked is never
attempted again by any product send path, and a digest SMTP had accepted is reconciled when the
bounce arrives later.

The production bug these pin: an account whose verification email bounced was activated by hand,
then received every daily and intraday digest -- each blocked by Brevo, each recorded as "sent".
"""

import json
from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .email_deliverability import (
    DELIVERY_SUPPRESSED_REASON,
    clear_email_delivery_suppression,
    is_email_delivery_suppressed,
    parse_digest_tag,
    reconcile_digest_delivery,
    record_email_delivery_suppression,
)
from .models import (
    Company,
    DigestDelivery,
    EmailDeliverySuppression,
    EmailEngagementEvent,
    OutreachSuppression,
    UserSubscription,
)
from .services import (
    digest_email_tag,
    send_digests,
    send_user_yesterday_digest,
    send_verification_email_now,
)

TOKEN = "test-token-123"


def _post_webhook(client, payload):
    return client.post(
        reverse("brevo_webhook", kwargs={"token": TOKEN}),
        data=json.dumps(payload).encode(), content_type="application/json",
    )


@override_settings(BREVO_WEBHOOK_TOKEN=TOKEN)
class WebhookSuppressionTests(TestCase):

    def test_hard_bounce_creates_delivery_suppression(self):  # A
        _post_webhook(self.client, {"event": "hardBounce", "email": " Dead@Example.GR "})
        row = EmailDeliverySuppression.objects.get()
        self.assertEqual((row.email, row.reason, row.active), ("dead@example.gr", "hard_bounce", True))
        self.assertTrue(is_email_delivery_suppressed("DEAD@example.gr"))

    def test_snake_case_hard_bounce_creates_it(self):
        _post_webhook(self.client, {"event": "hard_bounce", "email": "dead@example.gr"})
        self.assertEqual(EmailDeliverySuppression.objects.get().reason, "hard_bounce")

    def test_blocked_creates_delivery_suppression(self):  # B
        _post_webhook(self.client, {"event": "blocked", "email": "blocked@example.gr"})
        row = EmailDeliverySuppression.objects.get()
        self.assertEqual((row.email, row.reason), ("blocked@example.gr", "blocked"))

    def test_soft_bounce_does_not_create_it(self):  # C
        for event in ("softBounce", "soft_bounce", "delivered", "opened", "unsubscribed", "spam"):
            _post_webhook(self.client, {"event": event, "email": "live@example.gr"})
        self.assertFalse(EmailDeliverySuppression.objects.exists())
        self.assertFalse(is_email_delivery_suppressed("live@example.gr"))

    def test_outreach_suppression_behaviour_is_kept(self):
        _post_webhook(self.client, {"event": "hardBounce", "email": "dead@example.gr"})
        self.assertTrue(OutreachSuppression.is_suppressed("dead@example.gr"))

    def test_repeated_event_keeps_one_row_and_advances_last_seen(self):
        _post_webhook(self.client, {"event": "hardBounce", "email": "dead@example.gr"})
        first = EmailDeliverySuppression.objects.get()
        _post_webhook(self.client, {"event": "blocked", "email": "dead@example.gr"})
        row = EmailDeliverySuppression.objects.get()
        self.assertEqual(row.first_seen_at, first.first_seen_at)
        self.assertGreaterEqual(row.last_seen_at, first.last_seen_at)
        self.assertEqual(EmailEngagementEvent.objects.count(), 2)

    def test_event_is_still_logged_when_the_suppression_write_fails(self):
        from unittest.mock import patch

        with patch("gemiapp.email_tracking.record_email_delivery_suppression", side_effect=Exception("db")):
            response = _post_webhook(self.client, {"event": "hardBounce", "email": "dead@example.gr"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(EmailEngagementEvent.objects.count(), 1)
        self.assertTrue(OutreachSuppression.is_suppressed("dead@example.gr"))


class OutreachOptOutIsNotDeliverabilityTests(TestCase):

    def test_outreach_unsubscribe_alone_does_not_block_transactional_email(self):  # D
        OutreachSuppression.objects.create(email="optout@example.gr")
        self.assertFalse(is_email_delivery_suppressed("optout@example.gr"))

        user = User.objects.create_user("optout@example.gr", "optout@example.gr", "StrongPass123", is_active=False)
        self.assertTrue(send_verification_email_now(user.pk))
        self.assertEqual(mail.outbox[0].to, ["optout@example.gr"])


class DigestSuppressionTests(TestCase):

    def setUp(self):
        self.today = timezone.localdate()
        Company.objects.create(gemi_number="920000000001", name="ΝΕΑ ΙΚΕ", incorporation_date=self.today)
        self.user = User.objects.create_user("dead@example.gr", "dead@example.gr", "StrongPass123", is_active=True)
        record_email_delivery_suppression("dead@example.gr", "blocked")
        mail.outbox = []

    def _entitle_enterprise(self):
        UserSubscription.objects.filter(user=self.user).update(complimentary_tier="enterprise")
        self.user = User.objects.get(pk=self.user.pk)

    def test_daily_digest_skips_suppressed_email(self):  # E
        sent, skipped = send_digests(self.today, frequency="daily")
        self.assertEqual((sent, skipped, mail.outbox), (0, 1, []))
        row = DigestDelivery.objects.get(user=self.user, digest_date=self.today, frequency="daily")
        self.assertEqual((row.status, row.error_message), ("skipped", DELIVERY_SUPPRESSED_REASON))

    def test_preference_account_and_subscription_are_untouched(self):
        self._entitle_enterprise()
        send_digests(self.today, frequency="daily")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.user.digest_preference.frequency, "daily")
        self.assertEqual(self.user.subscription.complimentary_tier, "enterprise")

    def test_intraday_digest_skips_suppressed_email(self):  # F
        self._entitle_enterprise()
        marker = UserSubscription.objects.get(user=self.user).last_sent_company_id
        sent, skipped = send_digests(self.today, frequency="intraday")
        self.assertEqual((sent, skipped, mail.outbox), (0, 1, []))
        row = DigestDelivery.objects.get(user=self.user, frequency="intraday")
        self.assertEqual((row.status, row.error_message), ("skipped", DELIVERY_SUPPRESSED_REASON))
        self.assertEqual(UserSubscription.objects.get(user=self.user).last_sent_company_id, marker,
                         "nothing was delivered, so the intraday marker must not advance")

    def test_skip_never_overwrites_an_existing_record_of_the_day(self):
        """The one intraday row covers every slot: an earlier real send must stay recorded."""
        self._entitle_enterprise()
        DigestDelivery.objects.create(user=self.user, digest_date=self.today, frequency="intraday",
                                      status="sent", company_count=3)
        send_digests(self.today, frequency="intraday")
        row = DigestDelivery.objects.get(user=self.user, frequency="intraday")
        self.assertEqual((row.status, row.company_count), ("sent", 3))

    def test_manual_yesterday_digest_refuses_suppressed_email(self):
        with self.assertRaisesMessage(ValueError, DELIVERY_SUPPRESSED_REASON):
            send_user_yesterday_digest(self.user)
        self.assertEqual(mail.outbox, [])

    def test_unsuppressed_user_in_the_same_run_still_receives(self):
        User.objects.create_user("live@example.gr", "live@example.gr", "StrongPass123", is_active=True)
        sent, skipped = send_digests(self.today, frequency="daily")
        self.assertEqual((sent, skipped), (1, 1))
        self.assertEqual([m.to for m in mail.outbox], [["live@example.gr"]])

    def test_changing_to_an_unsuppressed_address_allows_delivery_again(self):  # L
        self.user.email = "fixed@example.gr"
        self.user.save()
        sent, _ = send_digests(self.today, frequency="daily")
        self.assertEqual(sent, 1)
        self.assertEqual(mail.outbox[0].to, ["fixed@example.gr"])
        self.assertTrue(is_email_delivery_suppressed("dead@example.gr"), "the old address stays suppressed")

    def test_operator_clear_allows_delivery_and_a_new_bounce_resuppresses(self):
        out = StringIO()
        call_command("clear_email_delivery_suppression", "--email", "DEAD@example.gr", stdout=out)
        self.assertIn("Cleared", out.getvalue())
        row = EmailDeliverySuppression.objects.get()
        self.assertEqual((row.active, row.cleared_at is not None), (False, True))
        self.assertEqual(send_digests(self.today, frequency="daily")[0], 1)

        self.assertEqual(record_email_delivery_suppression("dead@example.gr", "hardBounce"), "reactivated")
        self.assertTrue(is_email_delivery_suppressed("dead@example.gr"))

    def test_clear_dry_run_changes_nothing(self):
        call_command("clear_email_delivery_suppression", "--email", "dead@example.gr", "--dry-run", stdout=StringIO())
        self.assertTrue(is_email_delivery_suppressed("dead@example.gr"))

    def test_login_does_not_clear_suppression(self):
        self.client.force_login(self.user)
        self.client.get(reverse("dashboard"))
        self.assertTrue(is_email_delivery_suppressed("dead@example.gr"))


class VerificationAndPasswordResetTests(TestCase):

    def setUp(self):
        cache.clear()  # the reset / resend views are IP rate limited
        record_email_delivery_suppression("dead@example.gr", "hardBounce")
        mail.outbox = []

    def test_verification_send_skips_suppressed_email(self):  # G
        user = User.objects.create_user("dead@example.gr", "dead@example.gr", "StrongPass123", is_active=False)
        self.assertFalse(send_verification_email_now(user.pk))
        self.assertEqual(mail.outbox, [])

    def test_resend_verification_view_skips_suppressed_email_with_the_same_answer(self):  # G
        from unittest.mock import patch

        User.objects.create_user("dead@example.gr", "dead@example.gr", "StrongPass123", is_active=False)
        with patch("django_q.tasks.async_task", side_effect=Exception("broker down")):  # inline send
            response = self.client.post(reverse("resend_verification"), {"email": "dead@example.gr"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mail.outbox, [])

    def test_resend_verification_command_does_not_count_a_suppressed_send(self):
        User.objects.create_user("dead@example.gr", "dead@example.gr", "StrongPass123", is_active=False)
        out = StringIO()
        call_command("resend_verification_emails", "--email", "dead@example.gr", stdout=out)
        self.assertEqual(mail.outbox, [])
        self.assertIn("Εστάλησαν 0 από 1", out.getvalue())

    def test_password_reset_does_not_send_to_suppressed_email(self):  # H
        User.objects.create_user("dead@example.gr", "dead@example.gr", "StrongPass123", is_active=True)
        response = self.client.post(reverse("password_reset"), {"email": "dead@example.gr"})
        self.assertRedirects(response, reverse("password_reset_done"), fetch_redirect_response=False)
        self.assertEqual(mail.outbox, [])

    def test_password_reset_still_sends_to_a_deliverable_address(self):
        User.objects.create_user("live@example.gr", "live@example.gr", "StrongPass123", is_active=True)
        self.client.post(reverse("password_reset"), {"email": "live@example.gr"})
        self.assertEqual([m.to for m in mail.outbox], [["live@example.gr"]])

    def test_password_reset_still_sends_to_an_outreach_opt_out(self):
        OutreachSuppression.objects.create(email="optout@example.gr")
        User.objects.create_user("optout@example.gr", "optout@example.gr", "StrongPass123", is_active=True)
        self.client.post(reverse("password_reset"), {"email": "optout@example.gr"})
        self.assertEqual(len(mail.outbox), 1)

    def test_allauth_mail_skips_suppressed_email(self):
        from gemiapp.adapters import NoLocalSignupAdapter

        NoLocalSignupAdapter().send_mail("account/email/password_reset_key", "dead@example.gr", {})
        self.assertEqual(mail.outbox, [])


@override_settings(BREVO_WEBHOOK_TOKEN=TOKEN)
class DigestReconciliationTests(TestCase):

    def setUp(self):
        self.today = timezone.localdate()
        self.user = User.objects.create_user("dead@example.gr", "dead@example.gr", "StrongPass123")
        self.other = User.objects.create_user("other@example.gr", "other@example.gr", "StrongPass123")
        self.delivery = DigestDelivery.objects.create(
            user=self.user, digest_date=self.today, frequency="daily", status="sent", company_count=4,
        )
        self.other_delivery = DigestDelivery.objects.create(
            user=self.other, digest_date=self.today, frequency="daily", status="sent", company_count=4,
        )

    def test_bounced_digest_reconciles_an_earlier_sent_delivery(self):  # I
        tag = digest_email_tag(self.user.id, self.today, "daily")
        _post_webhook(self.client, {"event": "blocked", "email": "dead@example.gr", "tag": f'["{tag}"]'})
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "failed")
        self.assertEqual(self.delivery.error_message, "Brevo reported blocked after SMTP acceptance")
        self.other_delivery.refresh_from_db()
        self.assertEqual(self.other_delivery.status, "sent")
        self.assertEqual(EmailEngagementEvent.objects.get().tag, tag)

    def test_tag_naming_another_users_delivery_changes_nothing(self):  # J
        tag = digest_email_tag(self.other.id, self.today, "daily")
        _post_webhook(self.client, {"event": "hardBounce", "email": "dead@example.gr", "tag": tag})
        self.other_delivery.refresh_from_db()
        self.assertEqual(self.other_delivery.status, "sent")

    def test_malformed_tags_do_not_crash_or_update(self):  # J
        malformed = [
            "digest", "digest:", "digest:abc:2026-09-28:daily", f"digest:{self.user.id}:2026-13-45:daily",
            f"digest:{self.user.id}:{self.today}:hourly", f"digest:{self.user.id}:{self.today}:daily:extra",
            f"digest:-{self.user.id}:{self.today}:daily", f"digest:0:{self.today}:daily",
            f"outreach:{self.user.id}", f"verification:{self.user.id}", '["broken', "",
        ]
        for tag in malformed:
            response = _post_webhook(self.client, {"event": "hardBounce", "email": "dead@example.gr", "tag": tag})
            self.assertEqual(response.status_code, 200, tag)
        self.assertEqual(parse_digest_tag(None), None)
        self.assertFalse(reconcile_digest_delivery(["not", "a", "string"], "dead@example.gr", "hardBounce"))
        for delivery in (self.delivery, self.other_delivery):
            delivery.refresh_from_db()
            self.assertEqual(delivery.status, "sent")
        self.assertEqual(EmailEngagementEvent.objects.count(), len(malformed))

    def test_soft_bounce_does_not_reconcile(self):
        tag = digest_email_tag(self.user.id, self.today, "daily")
        _post_webhook(self.client, {"event": "softBounce", "email": "dead@example.gr", "tag": tag})
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "sent")

    def test_only_a_sent_row_is_reconciled(self):
        self.delivery.status = "skipped"
        self.delivery.save()
        tag = digest_email_tag(self.user.id, self.today, "daily")
        self.assertFalse(reconcile_digest_delivery(tag, "dead@example.gr", "hardBounce"))
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "skipped")

    def test_manual_yesterday_tag_is_reconciled(self):
        yesterday = self.today - timedelta(days=1)
        manual = DigestDelivery.objects.create(
            user=self.user, digest_date=yesterday, frequency="manual_yesterday", status="sent",
        )
        tag = digest_email_tag(self.user.id, yesterday, "manual_yesterday")
        self.assertTrue(reconcile_digest_delivery(tag, "dead@example.gr", "hard_bounce"))
        manual.refresh_from_db()
        self.assertEqual(manual.status, "failed")


class BackfillTests(TestCase):

    def setUp(self):
        base = timezone.now() - timedelta(days=3)
        for offset, (event, email) in enumerate([
            ("hardBounce", "Dead@Example.gr"), ("blocked", "dead@example.gr"),
            ("hard_bounce", "gone@example.gr"), ("softBounce", "full@example.gr"),
            ("delivered", "live@example.gr"), ("blocked", ""),
        ]):
            event = EmailEngagementEvent.objects.create(event_type=event, email=email, payload={})
            # explicit, distinct times: the Windows clock can stamp consecutive rows identically
            EmailEngagementEvent.objects.filter(pk=event.pk).update(received_at=base + timedelta(minutes=offset))

    def _run(self, *args):
        out = StringIO()
        call_command("backfill_email_delivery_suppressions", *args, stdout=out)
        return dict(line.split("=", 1) for line in out.getvalue().splitlines() if "=" in line)

    def test_dry_run_writes_nothing_and_reports_what_apply_would_do(self):
        result = self._run()
        self.assertIn("DRY RUN", result["mode"])
        self.assertEqual((result["events_examined"], result["created"]), ("3", "2"))
        self.assertEqual(result["active_suppressions_after"], "2")
        self.assertFalse(EmailDeliverySuppression.objects.exists())

    def test_backfill_is_idempotent(self):  # K
        first = self._run("--apply")
        self.assertEqual((first["created"], first["active_suppressions_after"]), ("2", "2"))
        emails = set(EmailDeliverySuppression.objects.values_list("email", flat=True))
        self.assertEqual(emails, {"dead@example.gr", "gone@example.gr"})
        snapshot = list(EmailDeliverySuppression.objects.order_by("email").values())

        second = self._run("--apply")
        self.assertEqual((second["created"], second["updated"], second["unchanged"]), ("0", "0", "3"))
        self.assertEqual(list(EmailDeliverySuppression.objects.order_by("email").values()), snapshot)

    def test_backfill_uses_event_times_and_latest_reason(self):
        self._run("--apply")
        row = EmailDeliverySuppression.objects.get(email="dead@example.gr")
        times = sorted(EmailEngagementEvent.objects.filter(email__iexact="dead@example.gr")
                       .values_list("received_at", flat=True))
        self.assertEqual((row.first_seen_at, row.last_seen_at), (times[0], times[-1]))
        self.assertEqual(row.reason, "blocked")

    def test_backfill_does_not_undo_an_operator_clear(self):
        self._run("--apply")
        clear_email_delivery_suppression("dead@example.gr")
        result = self._run("--apply")
        self.assertEqual(result["stale"], "2")
        self.assertFalse(is_email_delivery_suppressed("dead@example.gr"))

    def test_backfill_does_not_touch_outreach_suppression(self):
        self._run("--apply")
        self.assertFalse(OutreachSuppression.objects.exists())
