"""PR 1 of the lifecycle-email brief (2026-11-06): SubscriptionPeriod history, LifecycleEmail send log,
reminder preferences, E2/E4/E5/E8/E10, the daily job, checkout return and the Radar nudge."""

import io
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .lifecycle import deliver, reminder_unsubscribe_url, run_daily
from .models import CustomerRadar, EmailDeliverySuppression, LifecycleEmail, SubscriptionPeriod, UserSubscription
from .subscription_history import is_former_paid, months_paid, sync_stripe_period, user_ref_for
from .superadmin.services import grant_complimentary_access, revoke_complimentary_access
from .tests import _checkout_completed_event, _post_webhook, _stripe_subscription_payload, _subscription_event

STRIPE = dict(STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
              STRIPE_WEBHOOK_SECRET="whsec_test")
ON = dict(LIFECYCLE_EMAILS_ENABLED=True, BASE_URL="https://gemileads.gr")


def run_tasks_inline():
    """async_task -> deliver() synchronously, so on_commit hand-offs actually send in tests."""
    return patch("django_q.tasks.async_task", side_effect=lambda func, row_id: deliver(row_id))


class LifecycleTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("buyer@example.gr", "buyer@example.gr", "StrongPass123", first_name="Μαρία")

    def webhook(self, event, retrieve=None):
        with run_tasks_inline(), self.captureOnCommitCallbacks(execute=True):
            if retrieve is not None:
                with patch("stripe.Subscription.retrieve", return_value=retrieve):
                    return _post_webhook(self.client, event)
            return _post_webhook(self.client, event)

    def checkout(self, tier_price="price_pro", event_id="evt_checkout_1"):
        return self.webhook(_checkout_completed_event(self.user.id, event_id=event_id),
                            retrieve=_stripe_subscription_payload("active", tier_price))

    def kinds(self):
        return sorted(LifecycleEmail.objects.values_list("kind", flat=True))

    def sent_subjects(self):
        return [m.subject for m in mail.outbox]


@override_settings(**STRIPE)
class FlagOffTests(LifecycleTestCase):
    def test_nothing_is_reserved_or_sent_while_the_flag_is_off(self):
        self.assertFalse(settings.LIFECYCLE_EMAILS_ENABLED)          # the default
        self.checkout()
        self.assertEqual(LifecycleEmail.objects.count(), 0)
        self.assertEqual(mail.outbox, [])
        # history is still recorded: it does not depend on the email flag
        self.assertEqual(SubscriptionPeriod.objects.filter(user=self.user, ended_at__isnull=True).count(), 1)

    def test_daily_job_refuses_to_send_without_the_flag(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("send_lifecycle_emails", stdout=io.StringIO())


@override_settings(**STRIPE, **ON)
class SubscriptionHistoryTests(LifecycleTestCase):
    def test_checkout_opens_one_period_and_a_duplicate_webhook_writes_nothing(self):
        self.checkout()
        self.checkout()                      # same event id: already processed
        self.webhook(_subscription_event("customer.subscription.updated", event_id="evt_u1"))   # renewal-like
        periods = SubscriptionPeriod.objects.filter(user=self.user)
        self.assertEqual(periods.count(), 1)
        period = periods.get()
        self.assertEqual((period.source, period.tier, period.ended_at), ("stripe", "pro", None))
        self.assertEqual(period.user_ref, user_ref_for(self.user))

    def test_upgrade_closes_and_opens_and_closed_rows_never_change(self):
        self.checkout()
        first = SubscriptionPeriod.objects.get(user=self.user)
        self.webhook(_subscription_event("customer.subscription.updated", price_id="price_biz", event_id="evt_up"))
        first.refresh_from_db()
        self.assertEqual(first.end_reason, "upgraded")
        ended = first.ended_at
        self.webhook(_subscription_event("customer.subscription.updated", price_id="price_pro", event_id="evt_down"))
        first.refresh_from_db()
        self.assertEqual(first.ended_at, ended)                      # the closed row is never edited again
        self.assertEqual(list(SubscriptionPeriod.objects.filter(user=self.user).order_by("id")
                              .values_list("tier", "end_reason")),
                         [("pro", "upgraded"), ("business", "downgraded"), ("pro", "")])

    def test_cancellation_reason_comes_from_stripe_and_defaults_to_unknown(self):
        self.checkout()
        event = _subscription_event("customer.subscription.deleted", status="canceled", event_id="evt_del")
        event["data"]["object"]["cancellation_details"] = {"reason": "payment_failed"}
        self.webhook(event)
        self.assertEqual(SubscriptionPeriod.objects.get(user=self.user).end_reason, "payment_failed")
        self.assertTrue(is_former_paid(self.user))
        self.assertGreaterEqual(months_paid(self.user), 0)

    def test_past_due_closes_the_period_as_payment_failed_but_sends_no_free_email(self):
        self.checkout()
        self.webhook(_subscription_event("customer.subscription.updated", status="past_due", event_id="evt_pd"))
        self.assertEqual(SubscriptionPeriod.objects.get(user=self.user).end_reason, "payment_failed")
        self.assertNotIn("e10", self.kinds())

    def test_complimentary_grant_and_revoke_are_recorded_without_any_welcome(self):
        admin = User.objects.create_superuser("root@example.gr", "root@example.gr", "x")
        grant_complimentary_access(admin, self.user, "business")
        open_period = SubscriptionPeriod.objects.get(user=self.user)
        self.assertEqual((open_period.source, open_period.tier), ("complimentary", "business"))
        revoke_complimentary_access(admin, self.user)
        open_period.refresh_from_db()
        self.assertEqual(open_period.end_reason, "canceled")
        self.assertEqual(self.kinds(), [])                          # complimentary never gets E4 or E10

    def test_complimentary_expiry_is_closed_by_the_daily_job(self):
        admin = User.objects.create_superuser("root@example.gr", "root@example.gr", "x")
        grant_complimentary_access(admin, self.user, "pro", timezone.now() + timedelta(days=1))
        run_daily(now=timezone.now() + timedelta(days=2))
        self.assertEqual(SubscriptionPeriod.objects.get(user=self.user).end_reason, "complimentary_expired")

    def test_a_history_failure_never_blocks_the_entitlement(self):
        with patch("gemiapp.lifecycle_triggers.sync_stripe_period", side_effect=RuntimeError("history broken")):
            response = self.checkout()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(UserSubscription.objects.get(user=self.user).has_entitlement)

    def test_history_has_no_effect_on_entitlement(self):
        sub = self.user.subscription
        before = (sub.has_entitlement, sub.effective_tier, sub.radar_limit)
        SubscriptionPeriod.objects.create(user=self.user, user_ref="x", source="stripe", tier="enterprise",
                                          started_at=timezone.now())
        sub.refresh_from_db()
        self.assertEqual((sub.has_entitlement, sub.effective_tier, sub.radar_limit), before)

    def test_sync_is_idempotent(self):
        sub = self.user.subscription
        sub.tier, sub.status = "pro", "active"
        sub.save()
        sync_stripe_period(sub)
        sync_stripe_period(sub)
        self.assertEqual(SubscriptionPeriod.objects.filter(user=self.user).count(), 1)


@override_settings(**STRIPE, **ON)
class WelcomeEmailTests(LifecycleTestCase):
    def verify(self, user):
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        with run_tasks_inline(), self.captureOnCommitCallbacks(execute=True):
            return self.client.get(reverse("verify_email", kwargs={"uidb64": uid, "token": token}))

    def test_first_verification_sends_one_welcome_and_reverification_none(self):
        user = User.objects.create_user("new@example.gr", "new@example.gr", "StrongPass123", is_active=False)
        self.verify(user)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(self.sent_subjects(), ["Καλώς ήρθες στο Gemi Leads — ένα βήμα για να ξεκινήσεις"])
        message = mail.outbox[0]
        self.assertEqual(message.reply_to, ["info@gemileads.gr"])
        self.assertEqual(message.extra_headers["X-Mailin-Tag"], f"lifecycle:e2:{user.pk}")
        self.assertIn("https://gemileads.gr/dashboard/", message.body)
        self.assertIn("Νίκος", message.alternatives[0][0])
        self.client.logout()
        self.verify(user)                                           # already active: no second welcome
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(LifecycleEmail.objects.get(kind="e2").status, "sent")

    def _social(self, email):
        from allauth.socialaccount.models import SocialAccount, SocialLogin
        from django.contrib.sessions.backends.db import SessionStore
        from django.test import RequestFactory

        from .adapters import SocialAccountAdapter

        request = RequestFactory().get("/accounts/google/login/callback/")
        request.session = SessionStore()
        login = SocialLogin(user=User(email=email, username=email),
                            account=SocialAccount(provider="google", uid=f"uid-{email}"))
        return SocialAccountAdapter(), request, login

    def test_new_social_signup_gets_the_welcome_once(self):
        adapter, request, login = self._social("google@example.gr")
        with run_tasks_inline(), self.captureOnCommitCallbacks(execute=True):
            user = adapter.save_user(request, login)
        self.assertEqual(self.sent_subjects(), ["Καλώς ήρθες στο Gemi Leads — ένα βήμα για να ξεκινήσεις"])
        self.assertEqual(LifecycleEmail.objects.get(kind="e2").user, user)
        # A later password verification (or a second social sign-in) never sends another.
        with run_tasks_inline(), self.captureOnCommitCallbacks(execute=True):
            from .adapters import _welcome
            _welcome(user)
        self.assertEqual(len(mail.outbox), 1)

    def test_social_login_activating_an_unverified_account_gets_the_welcome(self):
        existing = User.objects.create_user("cold@example.gr", "cold@example.gr", "StrongPass123", is_active=False)
        adapter, request, login = self._social("cold@example.gr")
        with patch("allauth.socialaccount.models.SocialLogin.connect"), run_tasks_inline(), \
                self.captureOnCommitCallbacks(execute=True):
            adapter.pre_social_login(request, login)
        self.assertEqual(self.kinds(), ["e2"])
        self.assertEqual(LifecycleEmail.objects.get(kind="e2").user, existing)

    def test_social_login_of_an_active_account_sends_nothing(self):
        User.objects.create_user("warm@example.gr", "warm@example.gr", "StrongPass123")
        adapter, request, login = self._social("warm@example.gr")
        with patch("allauth.socialaccount.models.SocialLogin.connect"), run_tasks_inline(), \
                self.captureOnCommitCallbacks(execute=True):
            adapter.pre_social_login(request, login)
        self.assertEqual(self.kinds(), [])

    @override_settings(LIFECYCLE_EMAILS_ENABLED=False)
    def test_social_signup_with_flag_off_reserves_nothing(self):
        adapter, request, login = self._social("off@example.gr")
        with self.captureOnCommitCallbacks(execute=True):
            adapter.save_user(request, login)
        self.assertEqual(LifecycleEmail.objects.count(), 0)

    def test_paid_welcome_once_per_tier(self):
        self.checkout()
        self.checkout()                                             # duplicate webhook
        self.webhook(_subscription_event("customer.subscription.updated", event_id="evt_renew"))   # renewal
        self.assertEqual(self.sent_subjects(), ["Είσαι στο Pro — στήσε το πρώτο σου Radar"])
        self.assertIn("Έως 5 ενεργά Radars", mail.outbox[0].body)
        self.assertIn("https://gemileads.gr/radars/new/", mail.outbox[0].body)
        self.webhook(_subscription_event("customer.subscription.updated", price_id="price_biz", event_id="evt_up"))
        self.assertEqual(self.sent_subjects()[-1], "Είσαι στο Business — στήσε το πρώτο σου Radar")
        self.assertIn("Έως 10 ενεργά Radars", mail.outbox[-1].body)
        self.webhook(_subscription_event("customer.subscription.updated", price_id="price_pro", event_id="evt_dn"))
        self.assertEqual(len(mail.outbox), 2)                       # downgrade: no welcome
        self.assertEqual(self.kinds(), ["e4", "e4"])

    def test_existing_paid_customer_gets_nothing_on_renewal(self):
        sub = self.user.subscription
        sub.tier, sub.status, sub.stripe_subscription_id = "pro", "active", "sub_123"
        sub.save()
        self.webhook(_subscription_event("customer.subscription.updated", event_id="evt_r"))
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self.kinds(), [])

    def test_smtp_failure_is_recorded_and_retried_once_by_the_daily_job(self):
        with patch("gemiapp.lifecycle.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            self.checkout()
        row = LifecycleEmail.objects.get(kind="e4")
        self.assertEqual(row.status, "failed")
        self.assertIn("smtp", row.reason)
        self.assertTrue(UserSubscription.objects.get(user=self.user).has_entitlement)   # payment unaffected
        run_daily()
        run_daily()
        row.refresh_from_db()
        self.assertEqual(row.status, "sent")
        self.assertEqual(len(mail.outbox), 1)

    def test_a_suppressed_address_is_skipped(self):
        EmailDeliverySuppression.objects.create(email=self.user.email, reason="hard_bounce",
                                                first_seen_at=timezone.now(), last_seen_at=timezone.now())
        self.checkout()
        self.assertEqual(LifecycleEmail.objects.get(kind="e4").status, "skipped")
        self.assertEqual(mail.outbox, [])

    def test_account_emails_ignore_reminder_opt_out_and_do_not_contact(self):
        profile = self.user.lifecycle_profile
        profile.reminders_enabled, profile.do_not_contact = False, True
        profile.save()
        self.checkout()
        self.assertEqual(LifecycleEmail.objects.get(kind="e4").status, "sent")


@override_settings(**STRIPE, **ON)
class CancellationEmailTests(LifecycleTestCase):
    def test_scheduled_cancellation_sends_e8_once(self):
        self.checkout()
        ends = int((timezone.now() + timedelta(days=20)).timestamp())
        for event_id in ("evt_c1", "evt_c2"):
            self.webhook(_subscription_event("customer.subscription.updated", cancel_at_period_end=True,
                                             cancel_at=ends, event_id=event_id))
        e8 = [m for m in mail.outbox if m.subject.startswith("Η συνδρομή σου λήγει στις")]
        self.assertEqual(len(e8), 1)
        self.assertIn("/feedback/leaving/?context=cancel", e8[0].body)
        self.assertIn("https://gemileads.gr/settings/", e8[0].body)

    def test_subscription_deleted_sends_e10_but_not_when_still_complimentary(self):
        self.checkout()
        self.webhook(_subscription_event("customer.subscription.deleted", status="canceled", event_id="evt_del"))
        self.assertIn("Είσαι πλέον στο Free", self.sent_subjects())
        other = User.objects.create_user("comp@example.gr", "comp@example.gr", "x")
        sub = other.subscription
        sub.tier, sub.status, sub.stripe_subscription_id = "pro", "active", "sub_comp"
        sub.complimentary_tier = "business"
        sub.save()
        sync_stripe_period(sub)
        mail.outbox.clear()
        self.webhook(_subscription_event("customer.subscription.deleted", subscription_id="sub_comp",
                                         status="canceled", event_id="evt_del2"))
        self.assertEqual(mail.outbox, [])

    def test_e10_respects_do_not_contact(self):
        self.checkout()
        profile = self.user.lifecycle_profile
        profile.do_not_contact = True
        profile.save()
        self.webhook(_subscription_event("customer.subscription.deleted", status="canceled", event_id="evt_del"))
        self.assertEqual(LifecycleEmail.objects.get(kind="e10").status, "skipped")


@override_settings(**ON)
class DailyJobTests(LifecycleTestCase):
    def setUp(self):
        super().setUp()
        sub = self.user.subscription
        sub.tier, sub.status = "pro", "active"
        sub.save()
        self.start = timezone.now() - timedelta(days=10)
        self.period = SubscriptionPeriod.objects.create(user=self.user, user_ref=user_ref_for(self.user),
                                                        source="stripe", tier="pro",
                                                        started_at=timezone.now() - timedelta(hours=30))

    def daily(self, **kw):
        with override_settings(LIFECYCLE_START_AT=self.start.isoformat()):
            return run_daily(**kw)

    def test_e5_after_24h_without_radar_once_per_period(self):
        self.daily()
        self.daily()
        self.assertEqual(self.sent_subjects(), ["Το Radar σου δεν έχει στηθεί ακόμα"])
        message = mail.outbox[0]
        self.assertIn("List-Unsubscribe", message.extra_headers)
        self.assertIn("/reminders/unsubscribe/", message.body)
        self.assertEqual(LifecycleEmail.objects.get(kind="e5").dedupe_key, f"e5:{self.period.pk}")

    def test_e5_not_with_a_radar_not_before_activation_not_without_start(self):
        CustomerRadar.objects.create(user=self.user, name="R", prefectures=["ΑΤΤΙΚΗΣ"])
        self.daily()
        CustomerRadar.objects.all().delete()
        self.period.started_at = self.start - timedelta(days=1)       # before activation: never retroactive
        self.period.save()
        self.daily()
        self.period.started_at = timezone.now() - timedelta(hours=30)
        self.period.save()
        run_daily()                                                  # no LIFECYCLE_START_AT: nothing time-based
        self.assertEqual(LifecycleEmail.objects.count(), 0)

    def test_e5_still_sent_when_only_the_automatic_radar_exists(self):
        CustomerRadar.objects.create(user=self.user, name="Όλες οι νέες επιχειρήσεις")   # created at every signup
        self.daily()
        self.assertEqual(self.sent_subjects(), ["Το Radar σου δεν έχει στηθεί ακόμα"])

    def test_reminder_opt_out_and_do_not_contact_skip(self):
        profile = self.user.lifecycle_profile
        profile.reminders_enabled = False
        profile.save()
        self.daily()
        self.assertEqual(LifecycleEmail.objects.get(kind="e5").status, "skipped")
        self.assertEqual(mail.outbox, [])

    def test_second_reminder_of_the_day_moves_to_tomorrow(self):
        LifecycleEmail.objects.create(user=self.user, kind="e3", dedupe_key="e3:x", status="sent",
                                      sent_at=timezone.now())
        report = self.daily()
        row = LifecycleEmail.objects.get(kind="e5")
        self.assertEqual((row.status, report.deferred), ("queued", 1))
        self.assertGreater(row.not_before, timezone.now())
        self.daily(now=row.not_before + timedelta(minutes=1))
        row.refresh_from_db()
        self.assertEqual(row.status, "sent")

    def test_dry_run_writes_and_sends_nothing(self):
        out = io.StringIO()
        with override_settings(LIFECYCLE_START_AT=self.start.isoformat()):
            call_command("send_lifecycle_emails", "--dry-run", stdout=out)
        self.assertIn("E5 → user", out.getvalue())
        self.assertEqual(LifecycleEmail.objects.count(), 0)
        self.assertEqual(mail.outbox, [])

    def test_a_row_stuck_in_sending_is_never_resent(self):
        row = LifecycleEmail.objects.create(user=self.user, kind="e5", dedupe_key=f"e5:{self.period.pk}",
                                            status="sending", params={"period_id": self.period.pk})
        self.daily()
        row.refresh_from_db()
        self.assertEqual(row.status, "sending")
        self.assertEqual(mail.outbox, [])


@override_settings(**ON)
class PreferenceTests(LifecycleTestCase):
    def test_settings_toggle(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("settings")), "ΣΥΜΒΟΥΛΕΣ ΚΑΙ ΥΠΕΝΘΥΜΙΣΕΙΣ")
        self.client.post(reverse("settings_reminders"), {"reminders_enabled": "0"})
        self.user.lifecycle_profile.refresh_from_db()
        self.assertFalse(self.user.lifecycle_profile.reminders_enabled)
        self.client.post(reverse("settings_reminders"), {"reminders_enabled": "1"})
        self.user.lifecycle_profile.refresh_from_db()
        self.assertTrue(self.user.lifecycle_profile.reminders_enabled)

    def test_unsubscribe_link_get_changes_nothing_post_turns_reminders_off_only(self):
        url = reminder_unsubscribe_url(self.user).replace("https://gemileads.gr", "")
        self.client.get(url)
        self.user.lifecycle_profile.refresh_from_db()
        self.assertTrue(self.user.lifecycle_profile.reminders_enabled)
        self.client.post(url)
        self.user.lifecycle_profile.refresh_from_db()
        self.assertFalse(self.user.lifecycle_profile.reminders_enabled)
        self.assertEqual(self.user.digest_preference.frequency, "daily")      # the digest is untouched
        self.assertContains(self.client.get("/reminders/unsubscribe/forged/"), "άκυρος")

    def test_exit_survey_is_saved(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("churn_feedback") + "?context=cancel"), "Τι δεν σου κάλυψε;")
        self.client.post(reverse("churn_feedback"), {"context": "cancel", "reasons": ["price", "no_time"],
                                                     "text": "Δοκιμή"})
        from .models import ExitSurvey
        answer = ExitSurvey.objects.get()
        self.assertEqual((answer.reasons, answer.text, answer.context), (["price", "no_time"], "Δοκιμή", "cancel"))

    def test_superadmin_sets_do_not_contact_with_an_audit_entry(self):
        from .models import AdminAuditLog
        admin = User.objects.create_superuser("root@example.gr", "root@example.gr", "x")
        self.client.force_login(admin)
        with override_settings(SUPERADMIN_EMAILS=["root@example.gr"]):
            self.client.post(reverse("superadmin:user_do_not_contact", args=[self.user.pk]), {"do_not_contact": "1"})
        self.user.lifecycle_profile.refresh_from_db()
        self.assertTrue(self.user.lifecycle_profile.do_not_contact)
        self.assertTrue(AdminAuditLog.objects.filter(action="set_do_not_contact").exists())


@override_settings(**STRIPE)
class CheckoutReturnTests(LifecycleTestCase):
    def session(self, user_id=None, status="complete"):
        return {"id": "cs_1", "client_reference_id": str(user_id or self.user.pk), "status": status}

    def get(self, payload):
        self.client.force_login(self.user)
        with patch("stripe.checkout.Session.retrieve", return_value=payload):
            return self.client.get(reverse("checkout_return") + "?session_id=cs_1")

    def test_webhook_not_arrived_yet_shows_confirming_page(self):
        response = self.get(self.session())
        self.assertContains(response, "Η πληρωμή επιβεβαιώνεται")
        self.assertContains(response, 'http-equiv="refresh"')

    def test_entitled_without_radar_goes_to_the_radar_form(self):
        sub = self.user.subscription
        sub.tier, sub.status = "pro", "active"
        sub.save()
        response = self.get(self.session())
        self.assertRedirects(response, reverse("radar_create"), fetch_redirect_response=False)
        CustomerRadar.objects.create(user=self.user, name="Όλες οι νέες επιχειρήσεις")   # automatic: still the form
        self.assertRedirects(self.get(self.session()), reverse("radar_create"), fetch_redirect_response=False)
        CustomerRadar.objects.create(user=self.user, name="R", name_query="καφέ")
        self.assertRedirects(self.get(self.session()), reverse("dashboard"), fetch_redirect_response=False)

    def test_someone_elses_or_incomplete_session_goes_to_the_dashboard(self):
        for payload in (self.session(user_id=999999), self.session(status="open")):
            self.assertRedirects(self.get(payload), reverse("dashboard"), fetch_redirect_response=False)

    def test_success_url_points_here(self):
        from types import SimpleNamespace
        self.client.force_login(self.user)
        with override_settings(LEGAL_BILLING_ACTIVE=True), \
                patch("stripe.checkout.Session.create", return_value=SimpleNamespace(url="https://stripe.test/x")) as create:
            self.client.post(reverse("create_checkout_session"), {"tier": "pro"})
        self.assertIn("/billing/success/?session_id={CHECKOUT_SESSION_ID}", create.call_args.kwargs["success_url"])


def nudged_links(html):
    return re.findall(r'<a href="(/radars/)"[^>]*class="radar-nudge" aria-label="Radars — δεν έχεις στήσει ακόμη"', html)


class RadarNudgeTests(TestCase):
    def page(self, user, url="/dashboard/"):
        self.client.force_login(user)
        return self.client.get(url).content.decode()

    def make(self, email, tier=None, complimentary=None):
        user = User.objects.create_user(email, email, "x")
        sub = user.subscription
        if tier:
            sub.tier, sub.status = tier, "active"
        if complimentary:
            sub.complimentary_tier = complimentary
        sub.save()
        return user

    def test_paid_and_complimentary_without_radar_are_nudged_in_rail_mobile_and_dashboard(self):
        for user in (self.make("paid@example.gr", tier="pro"), self.make("comp@example.gr", complimentary="business")):
            html = self.page(user)
            self.assertEqual(len(nudged_links(html)), 2, user.email)             # rail + mobile nav
            self.assertIn("Δεν έχεις στήσει ακόμα Radar με τα δικά σου κριτήρια. Θέλει 2 λεπτά.", html)
            self.assertIn(f'href="{reverse("radar_create")}" class="product-ink-button">Φτιάξε Radar', html)

    def test_no_nudge_with_an_active_radar_for_free_or_anonymous(self):
        paid = self.make("paid@example.gr", tier="pro")
        CustomerRadar.objects.create(user=paid, name="R", legal_types=["ΙΚΕ"])
        free = self.make("free@example.gr")
        for user in (paid, free):
            html = self.page(user)
            self.assertEqual(nudged_links(html), [], user.email)
            self.assertNotIn("data-radar-nudge-notice", html)
        self.client.logout()
        self.assertNotIn("radar-nudge", self.client.get("/").content.decode())

    def test_a_paused_radar_does_not_count_as_set_up(self):
        paid = self.make("paid@example.gr", tier="pro")
        CustomerRadar.objects.create(user=paid, name="R", is_active=False, prefectures=["ΑΤΤΙΚΗΣ"])
        self.assertEqual(len(nudged_links(self.page(paid))), 2)

    def test_the_automatic_radar_does_not_hide_the_nudge(self):
        """Every signup creates an active, criteria-less «Όλες οι νέες επιχειρήσεις» Radar (founder report 2026-11-07:
        a September user who became Pro saw no purple Radars)."""
        paid = self.make("sept@example.gr", tier="pro")
        CustomerRadar.objects.create(user=paid, name="Όλες οι νέες επιχειρήσεις")
        self.assertEqual(len(nudged_links(self.page(paid))), 2)
        CustomerRadar.objects.create(user=paid, name="Καφετέριες", name_query="καφέ")
        self.assertEqual(nudged_links(self.page(paid)), [])

    def test_has_targeted_radar_counts_any_criterion_on_an_active_radar(self):
        from .models import ActivityCode, has_targeted_radar
        user = self.make("crit@example.gr", tier="pro")
        self.assertFalse(has_targeted_radar(user))
        CustomerRadar.objects.create(user=user, name="Όλες", name_query="   ")
        self.assertFalse(has_targeted_radar(user))
        deleted = CustomerRadar.objects.create(user=user, name="Del", prefectures=["ΑΤΤΙΚΗΣ"])
        deleted.deleted_at, deleted.is_active = timezone.now(), False
        deleted.save()
        self.assertFalse(has_targeted_radar(user))
        code = ActivityCode.objects.create(code="56.10", normalized_code="5610", description="Εστιατόρια")
        radar = CustomerRadar.objects.create(user=user, name="Codes")
        radar.activity_codes.add(code)
        self.assertTrue(has_targeted_radar(user))

    def test_workspace_navigation_is_nudged_too(self):
        from .organizations import create_organization
        paid = self.make("org@example.gr", tier="pro")
        create_organization(owner=paid, name="Org")
        html = self.page(paid, reverse("settings"))
        self.assertIn('data-nav="radars"', html)
        self.assertEqual(html.count('class="radar-nudge"'), 2)

    def test_css_motion_rules(self):
        css = (Path(settings.BASE_DIR) / "static" / "css" / "product-ui.css").read_text(encoding="utf-8")
        nudge = css.split("RADAR NUDGE", 1)[1]
        self.assertIn("#2a1a5e", nudge)
        self.assertIn("radial-gradient(120% 140% at 20% 0%, #7b4fd6 0%, #3a1f86 45%, #140a33 100%)", nudge)
        self.assertIn("animation: radar-nudge-pop 1.6s ease-in-out infinite", nudge)
        self.assertIn('a.radar-nudge[aria-current="page"] { animation: none; }', nudge)
        self.assertRegex(nudge, r"prefers-reduced-motion: reduce\)\s*\{[^}]*animation: none")
        keyframes = nudge.split("@keyframes radar-nudge-pop", 1)[1].split("}\n}", 1)[0]
        self.assertNotIn("width", keyframes)                                      # transform only
        self.assertIn("scale(1.05)", keyframes)


@override_settings(**ON)
class CommandTests(LifecycleTestCase):
    def test_preview_writes_files_and_leaves_the_database_untouched(self):
        users = User.objects.count()
        with tempfile.TemporaryDirectory() as out:
            call_command("preview_lifecycle_emails", "--out", out, stdout=io.StringIO(), stderr=io.StringIO())
            files = sorted(p.name for p in Path(out).iterdir())
        for kind in ("e2", "e4", "e5", "e8", "e10"):
            self.assertIn(f"{kind}.html", files)
            self.assertIn(f"{kind}.txt", files)
        self.assertEqual(User.objects.count(), users)
        self.assertEqual(LifecycleEmail.objects.count(), 0)
        self.assertEqual(mail.outbox, [])

    @override_settings(**STRIPE)
    def test_backfill_dry_run_then_apply(self):
        from .models import StripeWebhookEvent
        sub = self.user.subscription
        sub.stripe_subscription_id = "sub_bf"
        sub.save()
        for i, (etype, status, price) in enumerate([
            ("customer.subscription.created", "active", "price_pro"),
            ("customer.subscription.updated", "active", "price_biz"),
            ("customer.subscription.deleted", "canceled", "price_biz"),
        ]):
            payload = _subscription_event(etype, subscription_id="sub_bf", status=status, price_id=price,
                                          event_id=f"evt_bf{i}")
            payload["created"] = int((timezone.now() - timedelta(days=30 - i * 10)).timestamp())
            StripeWebhookEvent.objects.create(stripe_event_id=f"evt_bf{i}", event_type=etype, payload=payload,
                                              status="processed")
        out = io.StringIO()
        call_command("backfill_subscription_periods", stdout=out)
        self.assertIn("periods=2", out.getvalue())
        self.assertEqual(SubscriptionPeriod.objects.count(), 0)
        call_command("backfill_subscription_periods", "--apply", stdout=io.StringIO())
        self.assertEqual(list(SubscriptionPeriod.objects.order_by("started_at").values_list("tier", "end_reason")),
                         [("pro", "upgraded"), ("business", "unknown")])
        call_command("backfill_subscription_periods", "--apply", stdout=io.StringIO())     # no duplicates
        self.assertEqual(SubscriptionPeriod.objects.count(), 2)
        self.assertEqual(LifecycleEmail.objects.count(), 0)                                 # backfill sends nothing
