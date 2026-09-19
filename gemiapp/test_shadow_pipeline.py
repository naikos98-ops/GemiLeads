"""Tests for G2: the shadow opportunity pipeline (``gemiapp.opportunity_pipeline``).

A saved SHADOW CompanySignal runs through the existing C5 matching, C6 scoring, C7 breakdown and C8 materialisation,
for active Radars of entitled organizations only; it is idempotent, reports what it did and is wired after the
signal commits. SHADOW-backed opportunities never reach a customer surface, nothing is sent, assigned or tasked,
legacy and billing state is untouched, and LIVE is not enabled.
"""

import io
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.management import CommandError, call_command
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import opportunity_pipeline as g2
from .company_signals import DISCOVERY, LIVE, SHADOW, record_company_signal
from .models import (
    CompanySignal, CustomerRadar, DigestDelivery, DigestPreference, Opportunity, OpportunityScoreComponent,
    OpportunitySignal, OpportunityTask, OrganizationAuditEvent, OrganizationNotification, OrganizationRadar, RadarMatch,
    StripeWebhookEvent, UserCompanyLead, UserSubscription,
)
from .opportunities import get_opportunity_score_breakdown
from .opportunity_score_breakdown import explain_opportunity_scores
from .organization_access import (
    get_authorized_workspace_dashboard, get_authorized_workspace_opportunities, get_authorized_workspace_tasks,
)
from .test_customer_workspace import WorkspaceTestCase
from .test_gemi_company_activities import entitled_user, radar_for
from .test_opportunity_scoring import FRESH
from .test_organization_radar_matching import T0, make_company, new_company_signal, snapshot

_numbers = iter(range(700_000, 800_000))


class PipelineTestCase(WorkspaceTestCase):
    """Tenant A and B (both paying) with LIVE opportunities for ``self.company`` from the workspace fixture; every
    shadow test uses fresh companies so it never touches those LIVE rows unless it means to. The inherited feed
    fixtures' extra Radars are switched off, so each tenant has exactly one matching Radar: A's «Radar Α» and B's
    «Radar Β του B»."""

    def setUp(self):
        super().setUp()
        OrganizationRadar.objects.filter(pk__in=[self.radar_a.pk, self.radar_b.pk, self.radar_b_foreign.pk]).update(
            active=False)

    def fresh_company(self, with_snapshot=True):
        company = make_company(str(next(_numbers)))
        if with_snapshot:
            snapshot(company, T0)
        return company

    def shadow_signal(self, company=None, at=T0):
        return new_company_signal(company or self.fresh_company(), at, mode=SHADOW)

    def process(self, signal, **kwargs):
        return g2.process_company_signal(signal, as_of=kwargs.pop("as_of", FRESH), **kwargs)

    def opportunities_for(self, company):
        return Opportunity.objects.filter(company=company)

    def lapse(self, org):
        UserSubscription.objects.filter(user=org.members.get(role="owner").user).update(tier="free", status="inactive")


# --- the pipeline -------------------------------------------------------------------------------------------

class PipelineTests(PipelineTestCase):
    def test_a_shadow_signal_and_a_matching_active_radar_make_one_shadow_backed_opportunity_per_radar(self):
        company = self.fresh_company()
        signal = self.shadow_signal(company)
        run = self.process(signal)
        self.assertTrue(run.processed)
        rows = self.opportunities_for(company)
        self.assertEqual(sorted(rows.values_list("organization_id", "radar_id")),
                         sorted([(self.org.pk, self.row.radar_id), (self.org_b.pk, self.radar_b_full.pk)]))
        self.assertEqual(run.created, 2)
        for row in rows.select_related("latest_signal"):
            self.assertEqual((row.latest_signal_id, row.latest_signal.mode, row.first_signal_id, row.status,
                              row.assigned_to_id), (signal.pk, SHADOW, signal.pk, "new", None))
        self.assertEqual(CompanySignal.objects.get(pk=signal.pk).mode, SHADOW)  # never promoted

    def test_score_and_frozen_breakdown_are_the_c6_c7_values(self):
        company = self.fresh_company()
        signal = self.shadow_signal(company)
        self.process(signal)
        expected = {b.radar_id: b for b in explain_opportunity_scores(signal, as_of=FRESH)}
        for row in self.opportunities_for(company):
            stored = get_opportunity_score_breakdown(row)
            self.assertEqual(stored, expected[row.radar_id])
            self.assertEqual((row.score, row.scored_as_of), (expected[row.radar_id].score, FRESH))
            self.assertEqual(OpportunityScoreComponent.objects.filter(opportunity=row).count(), 5)

    def test_rerunning_the_same_signal_duplicates_nothing(self):
        company = self.fresh_company()
        signal = self.shadow_signal(company)
        self.process(signal)
        world = (list(Opportunity.objects.values()), list(OpportunitySignal.objects.values()),
                 list(OpportunityScoreComponent.objects.values()))
        again = self.process(signal, as_of=FRESH + timedelta(days=20))  # even a much later replay changes nothing
        self.assertEqual((again.created, again.updated, again.unchanged), (0, 0, 2))
        self.assertEqual((list(Opportunity.objects.values()), list(OpportunitySignal.objects.values()),
                          list(OpportunityScoreComponent.objects.values())), world)

    def test_non_matching_and_inactive_radars_create_nothing(self):
        other_kad = self.full_radar("Άλλος ΚΑΔ", kads=(self.r.kad_other,))
        idle = self.full_radar("Ανενεργό")
        idle.active = False
        idle.save(update_fields=["active"])
        company = self.fresh_company()
        run = self.process(self.shadow_signal(company))
        radars = set(self.opportunities_for(company).values_list("radar_id", flat=True))
        self.assertNotIn(other_kad.pk, radars)
        self.assertNotIn(idle.pk, radars)
        # C5's pre-filter already drops both (the KAD is refuted, the Radar inactive): only A's and B's are considered.
        self.assertEqual((run.considered, run.matched, run.created), (2, 2, 2))

    def test_an_unpaid_organization_gets_no_new_opportunity_and_keeps_its_history(self):
        self.lapse(self.org_b)
        history = list(Opportunity.objects.filter(organization=self.org_b).values())
        company = self.fresh_company()
        run = self.process(self.shadow_signal(company))
        self.assertEqual(list(self.opportunities_for(company).values_list("organization_id", flat=True)), [self.org.pk])
        self.assertGreaterEqual(run.skipped_not_entitled, 1)
        self.assertEqual(list(Opportunity.objects.filter(organization=self.org_b).values()), history)

    def test_a_replay_does_only_the_missing_work(self):
        self.lapse(self.org)
        company = self.fresh_company()
        signal = self.shadow_signal(company)
        self.assertEqual(self.process(signal).created, 1)                    # B only
        b_row = list(self.opportunities_for(company).values())
        UserSubscription.objects.filter(user=self.owner).update(tier="business", status="active")
        replay = self.process(signal, as_of=FRESH + timedelta(days=2))
        self.assertEqual((replay.created, replay.unchanged), (1, 1))         # A's missing one; B's left as it was
        self.assertIn(b_row[0], list(self.opportunities_for(company).values()))

    def test_each_opportunity_belongs_to_its_radars_organization(self):
        company = self.fresh_company()
        self.process(self.shadow_signal(company))
        for row in self.opportunities_for(company).select_related("radar"):
            self.assertEqual(row.organization_id, row.radar.organization_id)
        self.assertEqual(self.opportunities_for(company).filter(organization=self.org).count(), 1)
        self.assertEqual(self.opportunities_for(company).filter(organization=self.org_b).count(), 1)

    def test_a_shadow_signal_never_recaptures_a_live_backed_opportunity(self):
        # self.company already has LIVE-backed opportunities in A and B (the workspace fixture).
        before = list(Opportunity.objects.filter(company=self.company).values())
        run = self.process(self.shadow_signal(self.company))
        self.assertEqual(run.skipped_live_backed, 2)
        self.assertEqual(list(Opportunity.objects.filter(company=self.company).values()), before)
        self.assertEqual(self.status_of(self.owner, "organization_opportunities"), 200)
        self.assertIn(self.company.pk, [r.company_id for r in
                                        get_authorized_workspace_opportunities(self.owner, self.org.pk).rows])

    def test_live_is_not_enabled(self):
        company = self.fresh_company()
        live = new_company_signal(company, T0, mode=LIVE)
        run = self.process(live)
        self.assertEqual((run.processed, run.skipped_reason), (False, g2.LIVE_NOT_ENABLED))
        self.assertFalse(self.opportunities_for(company).exists())
        self.assertEqual(g2.PIPELINE_MODES, (SHADOW,))
        self.assertNotIn("promote_company_signal", open("gemiapp/opportunity_pipeline.py", encoding="utf-8").read())

    def test_missing_state_is_reported_and_signal_type_only_radars_still_match(self):
        watch_all = self.radar(name="Όλες οι νέες", signal_types=("new_company",))
        company = self.fresh_company(with_snapshot=False)
        run = self.process(self.shadow_signal(company))
        self.assertEqual(run.context_status, "no_snapshot")
        self.assertGreaterEqual(run.insufficient_state, 1)          # criteria Radars cannot be decided
        self.assertIn(watch_all.pk, self.opportunities_for(company).values_list("radar_id", flat=True))

    def test_observability(self):
        company = self.fresh_company()
        with self.assertLogs("gemiapp.opportunity_pipeline", "INFO") as logs:
            run = self.process(self.shadow_signal(company))
        self.assertEqual((run.entitled_considered, run.matched, run.created, run.errors), (run.considered, 2, 2, []))
        self.assertIn(f"signal={run.signal_id} mode=shadow", logs.output[-1])
        self.assertIn("created=2", logs.output[-1])

    def test_one_failing_radar_is_recorded_and_the_others_proceed(self):
        company = self.fresh_company()
        real = g2.materialize_opportunity

        def flaky(*, signal, radar, score, breakdown):
            if radar.organization_id == self.org_b.pk:
                raise RuntimeError("boom")
            return real(signal=signal, radar=radar, score=score, breakdown=breakdown)

        with patch.object(g2, "materialize_opportunity", flaky), self.assertLogs("gemiapp.opportunity_pipeline", "ERROR"):
            run = self.process(self.shadow_signal(company))
        self.assertEqual((run.created, run.errors), (1, [(self.radar_b_full.pk, "RuntimeError")]))

    def test_invalid_input_is_refused(self):
        for bad in (None, CompanySignal(), "1"):
            with self.assertRaises(g2.PipelineError):
                g2.process_company_signal(bad)


# --- wiring ----------------------------------------------------------------------------------------------------

class WiringTests(PipelineTestCase):
    def record(self, company, key):
        return record_company_signal(company=company, signal_type="new_company", source_type=DISCOVERY,
                                     event_key={"wiring": key}, detected_at=T0, mode=SHADOW)

    def test_a_newly_recorded_shadow_signal_is_processed_after_commit(self):
        company = self.fresh_company()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            signal, created = self.record(company, 1)
        self.assertTrue(created)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(set(self.opportunities_for(company).values_list("latest_signal_id", flat=True)), {signal.pk})

    def test_a_replayed_signal_registers_nothing_and_duplicates_nothing(self):
        company = self.fresh_company()
        with self.captureOnCommitCallbacks(execute=True):
            self.record(company, 2)
        count = self.opportunities_for(company).count()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            _, created = self.record(company, 2)
        self.assertEqual((created, len(callbacks), self.opportunities_for(company).count()), (False, 0, count))

    def test_a_rolled_back_producer_processes_nothing(self):
        company = self.fresh_company()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    self.record(company, 3)
                    raise RuntimeError("producer failed after recording")
            except RuntimeError:
                pass
        self.assertEqual((len(callbacks), CompanySignal.objects.filter(company=company).count()), (0, 0))
        self.assertFalse(self.opportunities_for(company).exists())

    def test_a_pipeline_failure_never_reaches_the_producer_or_the_signal(self):
        company = self.fresh_company()
        with patch.object(g2, "process_company_signal", side_effect=RuntimeError("pipeline down")), \
                self.assertLogs("gemiapp.opportunity_pipeline", "ERROR"), \
                self.captureOnCommitCallbacks(execute=True):
            signal, created = self.record(company, 4)
        self.assertTrue(created)
        self.assertEqual(CompanySignal.objects.get(pk=signal.pk).mode, SHADOW)
        self.assertFalse(self.opportunities_for(company).exists())
        run = self.process(signal)                       # the replay recovers it
        self.assertEqual(run.created, 2)


# --- customer surfaces, side effects, legacy ------------------------------------------------------------------

class CustomerSafetyTests(PipelineTestCase):
    def test_a_shadow_backed_opportunity_is_invisible_to_every_customer_surface(self):
        before = get_authorized_workspace_dashboard(self.owner, self.org.pk)
        unread = before.unread_notifications
        company = self.fresh_company()
        self.process(self.shadow_signal(company))
        shadow = Opportunity.objects.get(organization=self.org, company=company)
        Opportunity.objects.filter(pk=shadow.pk).update(status="saved")      # even when saved
        after = get_authorized_workspace_dashboard(self.owner, self.org.pk)
        self.assertEqual((after.active_opportunities, after.new_opportunities, after.saved_opportunities,
                          after.unread_notifications),
                         (before.active_opportunities, before.new_opportunities, before.saved_opportunities, unread))
        for view in ("active", "saved", "all"):
            rows = get_authorized_workspace_opportunities(self.owner, self.org.pk, view=view).rows
            self.assertNotIn(company.pk, [row.company_id for row in rows], view)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("organization_company_opportunity",
                                                 args=[self.org.pk, company.pk])).status_code, 404)
        html = "".join(self.html(self.owner, name, **query) for name, query in (
            ("organization_dashboard", {}), ("organization_opportunities", {"status": "all"}),
            ("organization_opportunities", {"status": "saved"})))
        self.assertNotIn(company.gemi_number, html)

    def test_nothing_is_sent_assigned_tasked_notified_or_audited(self):
        mail.outbox = []
        counts = (OrganizationNotification.objects.count(), OpportunityTask.objects.count(),
                  OrganizationAuditEvent.objects.count())
        company = self.fresh_company()
        self.process(self.shadow_signal(company))
        self.assertEqual((OrganizationNotification.objects.count(), OpportunityTask.objects.count(),
                          OrganizationAuditEvent.objects.count()), counts)
        self.assertEqual(mail.outbox, [])
        self.assertFalse(self.opportunities_for(company).exclude(assigned_to=None).exists())
        self.assertEqual(get_authorized_workspace_tasks(self.owner, self.org.pk).tasks, ())

    def test_legacy_billing_and_digest_state_is_untouched(self):
        legacy_user = entitled_user("legacy-g2@example.com")
        radar_for(legacy_user, "Παλιό Radar", prefectures=["ΑΤΤΙΚΗΣ"])

        def legacy():
            return tuple(list(model.objects.values()) for model in (
                CustomerRadar, UserCompanyLead, RadarMatch, DigestPreference, DigestDelivery, UserSubscription,
                StripeWebhookEvent))

        before = legacy()
        for _ in range(2):
            self.process(self.shadow_signal())
        self.assertEqual(legacy(), before)


# --- the replay command ----------------------------------------------------------------------------------------

class ReplayCommandTests(PipelineTestCase):
    def replay(self, *args):
        out = io.StringIO()
        call_command("process_shadow_signals", *args, stdout=out)
        return out.getvalue()

    def recent(self, mode=SHADOW, hours=1):
        return new_company_signal(self.fresh_company(), timezone.now() - timedelta(hours=hours), mode=mode)

    def test_only_recent_shadow_signals_in_id_order_and_bounded(self):
        first, second = self.recent(), self.recent()
        live = self.recent(mode=LIVE)
        old = self.recent(hours=48)
        output = self.replay("--limit", "1")
        self.assertIn("shadow signals selected=1 processed=1", output)
        self.assertIn(f"last id {first.pk}", output)
        self.assertIn(f"rerun with --after-id {first.pk}", output)
        self.assertTrue(self.opportunities_for(first.company).exists())
        self.assertFalse(self.opportunities_for(second.company).exists())
        self.replay("--after-id", str(first.pk))
        self.assertTrue(self.opportunities_for(second.company).exists())
        for untouched in (live, old):
            self.assertFalse(self.opportunities_for(untouched.company).exists())
        self.assertEqual(CompanySignal.objects.get(pk=live.pk).mode, LIVE)
        self.assertEqual(set(CompanySignal.objects.exclude(pk=live.pk).filter(
            pk__in=[first.pk, second.pk, old.pk]).values_list("mode", flat=True)), {SHADOW})

    def test_it_is_idempotent_and_the_dry_run_writes_nothing(self):
        signal = self.recent()
        dry = self.replay("--dry-run")
        self.assertIn("[dry-run] opportunities created=2", dry)
        self.assertFalse(self.opportunities_for(signal.company).exists())
        self.replay()
        world = (list(Opportunity.objects.values()), list(OpportunitySignal.objects.values()))
        output = self.replay()
        self.assertIn("created=0", output)
        self.assertEqual((list(Opportunity.objects.values()), list(OpportunitySignal.objects.values())), world)

    def test_bounds_are_enforced(self):
        for args in (("--limit", "0"), ("--limit", "1001"), ("--since-hours", "0")):
            with self.assertRaises(CommandError):
                self.replay(*args)
