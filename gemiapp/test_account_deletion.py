"""PR 2: self-service account deletion, the Superadmin action, E15 and the payload scrub."""

import io
from unittest.mock import patch

import stripe
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from .account_deletion import REDACTED, scrub_webhook_payloads
from .models import (
    AdminAuditLog, CustomerRadar, EmailEngagementEvent, ExitSurvey, LifecycleEmail, Organization,
    StripeWebhookEvent, SubscriptionPeriod, SupportMessage, UserCompanyLead, UserSubscription,
)
from .organizations import add_organization_member, create_organization
from .subscription_history import sync_stripe_period
from .test_lifecycle import STRIPE
from .tests import _post_webhook, _subscription_event

ON = dict(LIFECYCLE_EMAILS_ENABLED=True, BASE_URL="https://gemileads.gr")


def checkout_payload(customer="cus_del", email="del@example.gr"):
    return {"id": "evt_cs", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_1", "customer": customer, "subscription": "sub_del", "amount_total": 1900, "currency": "eur",
        "customer_email": email,
        "customer_details": {"email": email, "name": "Μαρία Π.", "phone": "+30 210 0000000",
                             "address": {"city": "Αθήνα", "line1": "Οδός 1", "postal_code": "10682"}},
    }}}


def run_task(func, *args, **kwargs):
    from .account_deletion import send_account_deleted_email
    assert kwargs.get("save") is False          # the address must not be kept in django-q history
    return send_account_deleted_email(*args)


class DeletionTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("del@example.gr", "del@example.gr", "StrongPass123", first_name="Μαρία")
        sub = self.user.subscription
        sub.tier, sub.status, sub.stripe_customer_id, sub.stripe_subscription_id = "pro", "active", "cus_del", "sub_del"
        sub.save()
        sync_stripe_period(sub)
        CustomerRadar.objects.create(user=self.user, name="R")
        StripeWebhookEvent.objects.create(stripe_event_id="evt_cs", event_type="checkout.session.completed",
                                          payload=checkout_payload(), status="processed")
        StripeWebhookEvent.objects.create(stripe_event_id="evt_other", event_type="checkout.session.completed",
                                          payload=checkout_payload("cus_other", "other@example.gr"), status="processed")
        EmailEngagementEvent.objects.create(event_type="opened", email="del@example.gr", tag="x", payload={})

    def delete(self, password="StrongPass123", **data):
        self.client.force_login(self.user)
        with patch("stripe.Subscription.cancel") as cancel, \
                patch("django_q.tasks.async_task", side_effect=run_task), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("account_delete"), {"confirm": password, **data})
        return response, cancel


@override_settings(**STRIPE, **ON)
class SelfServiceDeletionTests(DeletionTestCase):
    def test_settings_links_to_the_confirmation_page_which_explains_everything(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("settings")), reverse("account_delete"))
        page = self.client.get(reverse("account_delete"))
        for text in ("ΤΙ ΔΙΑΓΡΑΦΕΤΑΙ", "ΤΙ ΚΡΑΤΑΜΕ", "Για επιστροφή εντός 14 ημερών γράψε μας πριν τη διαγραφή",
                     "ΓΙΑΤΙ ΦΕΥΓΕΙΣ", 'type="password"'):
            self.assertContains(page, text)

    def test_wrong_password_deletes_nothing(self):
        response, cancel = self.delete(password="wrong")
        self.assertContains(response, "Ο κωδικός δεν είναι σωστός")
        cancel.assert_not_called()
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_full_flow_in_order(self):
        ref = str(self.user.lifecycle_profile.ref)
        pk = self.user.pk
        response, cancel = self.delete(reasons=["price"], text="ακριβό")
        cancel.assert_called_once_with("sub_del", prorate=False, invoice_now=False)
        self.assertContains(response, "Ο λογαριασμός σου διαγράφηκε")
        self.assertFalse(User.objects.filter(pk=pk).exists())
        self.assertNotIn("_auth_user_id", self.client.session)                                  # logged out
        period = SubscriptionPeriod.objects.get(user_ref=ref)
        self.assertEqual((period.user, period.end_reason), (None, "account_deleted"))
        survey = ExitSurvey.objects.get(user_ref=ref)
        self.assertEqual((survey.user, survey.context, survey.reasons), (None, "delete", ["price"]))
        for model in (UserSubscription, CustomerRadar, UserCompanyLead, SupportMessage):
            self.assertFalse(model.objects.filter(user_id=pk).exists(), model)
        self.assertFalse(EmailEngagementEvent.objects.filter(email="del@example.gr").exists())
        payload = StripeWebhookEvent.objects.get(stripe_event_id="evt_cs").payload["data"]["object"]
        self.assertEqual((payload["customer_details"], payload["customer_email"]), (REDACTED, REDACTED))
        self.assertEqual((payload["customer"], payload["amount_total"], payload["id"]), ("cus_del", 1900, "cs_1"))
        other = StripeWebhookEvent.objects.get(stripe_event_id="evt_other").payload["data"]["object"]
        self.assertEqual(other["customer_email"], "other@example.gr")                         # untouched
        self.assertEqual([m.subject for m in mail.outbox], ["Ο λογαριασμός σου διαγράφηκε"])
        self.assertEqual(mail.outbox[0].to, ["del@example.gr"])
        e15 = LifecycleEmail.objects.get(kind="e15")
        self.assertEqual((e15.status, e15.user, e15.dedupe_key), ("sent", None, f"e15:{ref}"))
        self.assertNotIn("del@example.gr", str(e15.params) + e15.reason + e15.brevo_tag)

    def test_stripe_refusal_deletes_nothing(self):
        self.client.force_login(self.user)
        with patch("stripe.Subscription.cancel", side_effect=stripe.error.APIConnectionError("down")):
            response = self.client.post(reverse("account_delete"), {"confirm": "StrongPass123"})
        self.assertContains(response, "Δεν ήταν δυνατή η ακύρωση της συνδρομής")
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_social_only_account_confirms_with_its_email(self):
        self.user.set_unusable_password()
        self.user.save()
        response, _ = self.delete(password="nope@example.gr")
        self.assertContains(response, "Το email δεν ταιριάζει")
        self.delete(password="DEL@example.gr")
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    def test_sole_owner_organization_is_deleted_shared_one_blocks(self):
        org = create_organization(owner=self.user, name="Solo").organization
        other = User.objects.create_user("m@example.gr", "m@example.gr", "x")
        shared = create_organization(owner=self.user, name="Shared").organization
        add_organization_member(shared, other, "viewer")
        response, cancel = self.delete()
        self.assertContains(response, "οργανισμού με άλλα μέλη")
        cancel.assert_not_called()
        shared.delete()
        self.delete()
        self.assertFalse(Organization.objects.filter(pk=org.pk).exists())

    def test_webhooks_after_deletion_are_graceful_no_ops(self):
        self.delete()
        late_checkout = {"id": "evt_after_cs", "type": "checkout.session.completed", "data": {"object": {
            "client_reference_id": str(self.user.pk), "customer": "cus_del", "subscription": "sub_del"}}}
        late_deleted = _subscription_event("customer.subscription.deleted", subscription_id="sub_del",
                                           status="canceled", event_id="evt_after")
        for event in (late_deleted, late_checkout):
            with patch("stripe.Subscription.retrieve") as retrieve:
                self.assertEqual(_post_webhook(self.client, event).status_code, 200)
            retrieve.assert_not_called()
        self.assertEqual(set(StripeWebhookEvent.objects.filter(stripe_event_id__startswith="evt_after")
                             .values_list("status", flat=True)), {"processed"})

    def test_flag_off_deletes_but_sends_no_e15(self):
        with override_settings(LIFECYCLE_EMAILS_ENABLED=False):
            self.delete()
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertEqual(mail.outbox, [])
        self.assertFalse(LifecycleEmail.objects.filter(kind="e15").exists())


@override_settings(**STRIPE, **ON, SUPERADMIN_EMAILS=["root@example.gr"])
class SuperadminDeletionTests(DeletionTestCase):
    def test_admin_action_with_audit_and_email_confirmation(self):
        admin = User.objects.create_superuser("root@example.gr", "root@example.gr", "x")
        AdminAuditLog.objects.create(admin_user=admin, action="grant", target_type="User",
                                     target_id=str(self.user.pk), target_repr="del@example.gr")
        self.client.force_login(admin)
        url = reverse("superadmin:user_delete_account", args=[self.user.pk])
        with patch("stripe.Subscription.cancel"):
            self.client.post(url, {"confirm_email": "wrong@example.gr"})
            self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
            self.client.post(url, {"confirm_email": "del@example.gr"})
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        log = AdminAuditLog.objects.get(action="delete_account")
        self.assertTrue(log.target_repr.startswith("deleted:"))
        self.assertFalse(AdminAuditLog.objects.filter(target_repr="del@example.gr").exists())    # address redacted


class ScrubCommandTests(DeletionTestCase):
    def test_dry_run_then_apply(self):
        out = io.StringIO()
        call_command("scrub_customer_webhook_payloads", "--email", "del@example.gr", stdout=out)
        self.assertIn("would scrub 1", out.getvalue())
        self.assertEqual(StripeWebhookEvent.objects.get(stripe_event_id="evt_cs").payload["data"]["object"]
                         ["customer_email"], "del@example.gr")
        call_command("scrub_customer_webhook_payloads", "--customer", "cus_del", "--apply", stdout=io.StringIO())
        self.assertEqual(StripeWebhookEvent.objects.get(stripe_event_id="evt_cs").payload["data"]["object"]
                         ["customer_email"], REDACTED)
        self.assertEqual(scrub_webhook_payloads(customer_ids=["cus_del"]), 0)                   # idempotent
