"""PR 3 lifecycle emails: E1, E3, E6, E7, E9, E11, E12, E13, E14 (triggers, once-only, exclusions, texts)."""

from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .lifecycle import keep_url, run_daily
from .models import (
    Company, CustomerRadar, EmailEngagementEvent, LifecycleEmail, RadarMatch, SubscriptionPeriod, UserCompanyLead,
)
from .subscription_history import user_ref_for
from .test_lifecycle import STRIPE
from .tests import _post_webhook

START = timezone.now() - timedelta(days=90)
ON = dict(LIFECYCLE_EMAILS_ENABLED=True, BASE_URL="https://gemileads.gr", LIFECYCLE_START_AT=START.isoformat())


def subjects():
    return [m.subject for m in mail.outbox]


def make_user(email, *, active=True, tier=None, joined_days=60, login_days=None):
    user = User.objects.create_user(email, email, "x", is_active=active)
    User.objects.filter(pk=user.pk).update(
        date_joined=timezone.now() - timedelta(days=joined_days),
        last_login=(timezone.now() - timedelta(days=login_days)) if login_days is not None else timezone.now(),
    )
    user.refresh_from_db()
    if tier:
        sub = user.subscription
        sub.tier, sub.status = tier, "active"
        sub.save()
    return user


def kinds_for(user):
    return sorted(LifecycleEmail.objects.filter(user=user, status="sent").values_list("kind", flat=True))


@override_settings(**ON)
class E1Tests(TestCase):
    def test_unverified_after_24h_once_with_a_fresh_link(self):
        user = make_user("new@example.gr", active=False, joined_days=2, login_days=None)
        User.objects.filter(pk=user.pk).update(last_login=None)
        run_daily()
        run_daily()
        self.assertEqual(subjects(), ["Επιβεβαίωσε το email σου για να ξεκινήσεις"])
        self.assertIn("https://gemileads.gr/verify/", mail.outbox[0].body)

    def test_not_before_24h_not_before_activation(self):
        make_user("fresh@example.gr", active=False, joined_days=0)
        make_user("old@example.gr", active=False, joined_days=200)
        run_daily()
        self.assertEqual(mail.outbox, [])


@override_settings(**ON)
class E3Tests(TestCase):
    def setUp(self):
        self.user = make_user("free@example.gr")
        LifecycleEmail.objects.create(user=self.user, kind="e2", dedupe_key=f"e2:{self.user.pk}", status="sent",
                                      sent_at=timezone.now() - timedelta(hours=50))

    def test_free_without_filter_use_gets_e3_once(self):
        run_daily()
        run_daily()
        self.assertEqual(subjects(), ["Βρες τις καταχωρίσεις που σε αφορούν σε 2 κλικ"])
        self.assertIn("Radar (Pro)", mail.outbox[0].body)

    def test_a_dashboard_filter_marks_use_and_prevents_e3(self):
        self.client.force_login(self.user)
        self.client.get(reverse("dashboard") + "?prefecture=ATTIKH")
        self.user.lifecycle_profile.refresh_from_db()
        self.assertIsNotNone(self.user.lifecycle_profile.last_filter_used_at)
        run_daily()
        self.assertNotIn("e3", kinds_for(self.user))

    def test_paid_accounts_do_not_get_e3(self):
        sub = self.user.subscription
        sub.tier, sub.status = "pro", "active"
        sub.save()
        run_daily()
        self.assertNotIn("Βρες τις καταχωρίσεις που σε αφορούν σε 2 κλικ", subjects())


@override_settings(**ON)
class E6Tests(TestCase):
    def test_week_without_match_once_per_radar_and_not_with_a_match(self):
        user = make_user("paid@example.gr", tier="pro")
        quiet = CustomerRadar.objects.create(user=user, name="Ήσυχο", monitor_from=timezone.now() - timedelta(days=8))
        busy = CustomerRadar.objects.create(user=user, name="Με match", monitor_from=timezone.now() - timedelta(days=8))
        company = Company.objects.create(gemi_number="1", name="Α", incorporation_date=date.today())
        lead = UserCompanyLead.objects.create(user=user, company=company)
        RadarMatch.objects.create(radar=busy, company=company, lead=lead, matched_on=date.today())
        run_daily()
        run_daily()
        e6 = [m for m in mail.outbox if m.subject.startswith("Το Radar σου δεν βρήκε")]
        self.assertEqual(len(e6), 1)
        self.assertIn(f"/radars/{quiet.pk}/", e6[0].body)


@override_settings(**ON, **STRIPE)
class E7Tests(TestCase):
    def test_scheduled_downgrade_announces_the_new_limit_without_claiming_a_pause(self):
        user = make_user("biz@example.gr", tier="business")
        sub = user.subscription
        sub.stripe_subscription_id = "sub_biz"
        sub.save()
        for i in range(7):
            CustomerRadar.objects.create(user=user, name=f"R{i}")
        when = int((timezone.now() + timedelta(days=20)).timestamp())
        event = {"id": "evt_sched", "type": "subscription_schedule.updated", "data": {"object": {
            "id": "sub_sched_1", "subscription": "sub_biz",
            "phases": [{"items": [{"price": "price_biz"}]}, {"items": [{"price": "price_pro"}], "start_date": when}],
        }}}
        with patch("django_q.tasks.async_task", side_effect=lambda f, pk: __import__("gemiapp.lifecycle", fromlist=["deliver"]).deliver(pk)), \
                self.captureOnCommitCallbacks(execute=True):
            _post_webhook(self.client, event)
            _post_webhook(self.client, {**event, "id": "evt_sched_2"})
        e7 = [m for m in mail.outbox if "περνάς στο Pro" in m.subject]
        self.assertEqual(len(e7), 1)
        self.assertIn("έως 5 ενεργά Radars και τώρα έχεις 7", e7[0].body)
        self.assertNotIn("μπαίνουν σε παύση", e7[0].body)
        self.assertIn("https://gemileads.gr/settings/", e7[0].body)


@override_settings(**ON)
class E9Tests(TestCase):
    def test_three_days_before_an_announced_end(self):
        user = make_user("leaving@example.gr", tier="pro")
        sub = user.subscription
        sub.stripe_subscription_id, sub.active_until = "sub_l", timezone.now() + timedelta(days=3)
        sub.save()
        run_daily()
        self.assertEqual(mail.outbox, [])                    # no E8 was ever announced: not after activation
        LifecycleEmail.objects.create(user=user, kind="e8", status="sent",
                                      dedupe_key=f"e8:sub_l:{sub.active_until.date().isoformat()}")
        run_daily()
        run_daily()
        self.assertEqual(subjects(), ["Η συνδρομή σου λήγει σε 3 μέρες"])
        self.assertIn("Η εξαγωγή σε CSV", mail.outbox[0].body)


@override_settings(**ON)
class InactivityTests(TestCase):
    def setUp(self):
        self.user = make_user("idle@example.gr", login_days=10)
        for n in range(3):
            Company.objects.create(gemi_number=str(100 + n), name=f"Ε{n}", incorporation_date=date.today() - timedelta(days=2))

    def test_e11_with_the_week_count_once_per_inactivity_period(self):
        run_daily()
        run_daily()
        self.assertEqual(subjects(), ["3 νέες καταχωρίσεις αυτή την εβδομάδα"])

    def test_a_recent_click_counts_as_activity_but_opens_do_not(self):
        EmailEngagementEvent.objects.create(event_type="opened", email=self.user.email, payload={})
        EmailEngagementEvent.objects.create(event_type="click", email=self.user.email, payload={})
        run_daily()
        self.assertEqual(mail.outbox, [])

    def test_no_e11_when_n_is_zero(self):
        Company.objects.all().delete()
        run_daily()
        self.assertEqual(mail.outbox, [])

    def test_e12_after_30_days_then_reminders_stop_unless_kept(self):
        User.objects.filter(pk=self.user.pk).update(last_login=timezone.now() - timedelta(days=31))
        self.user.refresh_from_db()
        start = self.user.last_login.date().isoformat()
        LifecycleEmail.objects.create(user=self.user, kind="e11", status="sent",
                                      dedupe_key=f"e11:{self.user.pk}:{start}", sent_at=timezone.now() - timedelta(days=24))
        run_daily()
        self.assertEqual(subjects(), ["Να συνεχίσω να σου γράφω;"])
        self.assertIn("/reminders/keep/", mail.outbox[0].body)
        later = timezone.now() + timedelta(days=15)
        run_daily(now=later)
        self.user.lifecycle_profile.refresh_from_db()
        self.assertFalse(self.user.lifecycle_profile.reminders_enabled)
        self.assertEqual(self.user.digest_preference.frequency, "daily")             # digest untouched
        url = keep_url(self.user).replace("https://gemileads.gr", "")
        self.client.get(url)
        self.user.lifecycle_profile.refresh_from_db()
        self.assertFalse(self.user.lifecycle_profile.reminders_enabled)              # GET changes nothing
        self.client.post(url)
        self.user.lifecycle_profile.refresh_from_db()
        self.assertTrue(self.user.lifecycle_profile.reminders_enabled)
        self.assertIsNotNone(self.user.lifecycle_profile.kept_at)

    def test_reminders_respect_the_one_per_day_rule(self):
        LifecycleEmail.objects.create(user=self.user, kind="e5", dedupe_key="e5:x", status="sent",
                                      sent_at=timezone.now())
        report = run_daily()
        self.assertEqual(report.deferred, 1)
        self.assertEqual(mail.outbox, [])


@override_settings(**ON)
class WinBackTests(TestCase):
    def setUp(self):
        self.user = make_user("former@example.gr")
        self.period = SubscriptionPeriod.objects.create(
            user=self.user, user_ref=user_ref_for(self.user), source="stripe", tier="pro",
            started_at=timezone.now() - timedelta(days=60), ended_at=timezone.now() - timedelta(days=15),
            end_reason="canceled")
        CustomerRadar.objects.create(user=self.user, name="Αθήνα", prefectures=["ΑΤΤΙΚΗΣ"])
        Company.objects.create(gemi_number="900", name="Ταιριάζει", prefecture="ΑΤΤΙΚΗΣ",
                               incorporation_date=date.today() - timedelta(days=3))
        Company.objects.create(gemi_number="901", name="Όχι", prefecture="ΧΙΟΥ",
                               incorporation_date=date.today() - timedelta(days=3))
        # keep E11 out of these tests: the user is active
        User.objects.filter(pk=self.user.pk).update(last_login=timezone.now())

    def test_e13_counts_what_the_saved_radars_would_have_found(self):
        run_daily()
        run_daily()
        self.assertEqual(subjects(), ["Το Radar σου θα είχε βρει 1 νέα καταχώριση"])

    def test_e13_without_a_number_when_nothing_matches_and_never_with_dnc(self):
        Company.objects.filter(gemi_number="900").delete()
        run_daily()
        self.assertEqual(subjects(), ["Τα Radars σου είναι ακόμη αποθηκευμένα"])
        other = make_user("dnc@example.gr")
        SubscriptionPeriod.objects.create(user=other, user_ref=user_ref_for(other), source="stripe", tier="pro",
                                          started_at=START, ended_at=timezone.now() - timedelta(days=15),
                                          end_reason="canceled")
        profile = other.lifecycle_profile
        profile.do_not_contact = True
        profile.save()
        run_daily()
        self.assertFalse(LifecycleEmail.objects.filter(user=other, kind="e13").exists())

    def test_e14_only_with_founder_text_and_after_60_days(self):
        SubscriptionPeriod.objects.filter(pk=self.period.pk).update(ended_at=timezone.now() - timedelta(days=61))
        LifecycleEmail.objects.create(user=self.user, kind="e13", dedupe_key=f"e13:{self.period.pk}", status="sent",
                                      sent_at=timezone.now() - timedelta(days=40))
        run_daily()
        self.assertFalse(LifecycleEmail.objects.filter(kind="e14").exists())         # no text => never reserved
        with override_settings(LIFECYCLE_WHATS_NEW="Νέο φίλτρο νομικής μορφής | Γρηγορότερο πρωινό email"):
            run_daily()
            run_daily()
        self.assertEqual(subjects(), ["Τι νέο έχει το Gemi Leads"])
        self.assertIn("Νέο φίλτρο νομικής μορφής", mail.outbox[0].body)

    def test_no_win_back_for_someone_who_subscribed_again(self):
        SubscriptionPeriod.objects.create(user=self.user, user_ref=user_ref_for(self.user), source="stripe",
                                          tier="pro", started_at=timezone.now() - timedelta(days=1))
        run_daily()
        self.assertEqual(mail.outbox, [])
