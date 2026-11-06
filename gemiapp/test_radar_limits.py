"""Automatic pause of Radars when the allowance shrinks (gemiapp/radar_limits.py)."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import CustomerRadar, StripeWebhookEvent
from .superadmin.services import grant_complimentary_access, revoke_complimentary_access
from .test_lifecycle import STRIPE
from .tests import _post_webhook, _subscription_event


@override_settings(**STRIPE)
class AutoPauseTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("biz@example.gr", "biz@example.gr", "StrongPass123")
        sub = self.user.subscription
        sub.tier, sub.status, sub.stripe_subscription_id = "business", "active", "sub_123"
        sub.save()
        base = timezone.now() - timedelta(days=30)
        self.radars = []
        for i in range(8):
            radar = CustomerRadar.objects.create(user=self.user, name=f"R{i}")
            CustomerRadar.objects.filter(pk=radar.pk).update(created_at=base + timedelta(days=i))
            self.radars.append(radar.pk)

    def active_ids(self):
        return list(CustomerRadar.objects.filter(user=self.user, is_active=True).order_by("pk")
                    .values_list("pk", flat=True))

    def webhook(self, price_id, event_id):
        with self.captureOnCommitCallbacks(execute=True):
            return _post_webhook(self.client, _subscription_event(
                "customer.subscription.updated", price_id=price_id, event_id=event_id))

    def test_downgrade_keeps_the_oldest_and_pauses_the_newest(self):
        response = self.webhook("price_pro", "evt_dn")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.active_ids(), self.radars[:5])
        paused = CustomerRadar.objects.get(pk=self.radars[-1])
        self.assertFalse(paused.is_active)
        self.assertIsNone(paused.deleted_at)                    # paused, not deleted: criteria stay

    def test_upgrade_and_renewal_pause_nothing_and_never_resume(self):
        self.webhook("price_pro", "evt_dn")
        self.webhook("price_pro", "evt_renew")                   # renewal: same limit
        self.assertEqual(len(self.active_ids()), 5)
        self.webhook("price_biz", "evt_up")                      # upgrade: nothing is resumed for the user
        self.assertEqual(len(self.active_ids()), 5)

    def test_losing_entitlement_pauses_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            _post_webhook(self.client, _subscription_event("customer.subscription.deleted", status="canceled",
                                                           event_id="evt_del"))
        self.assertEqual(len(self.active_ids()), 8)              # kept for when a subscription returns

    def test_within_the_new_limit_nothing_changes(self):
        CustomerRadar.objects.filter(pk__in=self.radars[4:]).update(is_active=False)
        self.webhook("price_pro", "evt_dn")
        self.assertEqual(self.active_ids(), self.radars[:4])

    def test_deleted_radars_are_not_counted(self):
        CustomerRadar.objects.filter(pk__in=self.radars[:3]).update(is_active=False, deleted_at=timezone.now())
        self.webhook("price_pro", "evt_dn")
        self.assertEqual(self.active_ids(), self.radars[3:])

    @override_settings(RADAR_AUTO_PAUSE_ON_DOWNGRADE=False)
    def test_switch_off_restores_the_old_behaviour(self):
        self.webhook("price_pro", "evt_dn")
        self.assertEqual(len(self.active_ids()), 8)

    def test_a_failing_pause_never_breaks_the_webhook(self):
        with patch("gemiapp.legacy_radar_sync.mirror_legacy_radar", side_effect=RuntimeError("boom")):
            response = self.webhook("price_pro", "evt_dn")
        self.assertEqual(response.status_code, 200)
        self.user.subscription.refresh_from_db()
        self.assertEqual(self.user.subscription.tier, "pro")     # the entitlement write stands
        self.assertEqual(len(self.active_ids()), 8)              # each failed pause rolled back on its own
        self.assertEqual(StripeWebhookEvent.objects.get(stripe_event_id="evt_dn").status, "processed")

    def test_a_lower_complimentary_tier_pauses_and_a_revoke_does_not(self):
        sub = self.user.subscription
        sub.status, sub.stripe_subscription_id = "canceled", ""      # complimentary access only
        sub.save()
        admin = User.objects.create_superuser("admin@example.gr", "admin@example.gr", "x")
        grant_complimentary_access(admin, self.user, "enterprise")
        self.assertEqual(len(self.active_ids()), 8)              # a raise pauses nothing
        grant_complimentary_access(admin, self.user, "pro")       # 15 -> 5
        self.assertEqual(self.active_ids(), self.radars[:5])
        revoke_complimentary_access(admin, self.user)             # -> no access: limit 0, nothing more paused
        self.assertEqual(self.active_ids(), self.radars[:5])
