"""The dormant LIVE cutover (gemiapp.live_cutover): persistent LIVE periods, the per-cycle signal mode, the pipeline's
mode rules, the separate customer-visibility gate, rollback / re-enable and the read-only readiness report.

Unlike the D29-D37 modules, nothing here turns visibility on for the whole module: the dormant default is the point.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_q.models import Schedule

from . import ingestion_cycle, live_cutover, new_company_signals
from . import opportunity_pipeline as g2
from .company_signals import LIVE, SHADOW
from .g4_shadow_cycle import preflight
from .ingestion_cycle import LIVE_CUTOVER_PHASE, IngestionCycleRefused
from .legacy_radar_sync import mirror_enabled
from .live_cutover import (
    CLOSED, OPENED, UNCHANGED, cutover_state, record_cycle_signal_mode, unauthorized_live_signals,
)
from .live_cutover_readiness import BLOCKED, EXPECTED_CRON, INGESTION_TASK, READY, build_readiness_report
from .models import (
    Company, CompanySignal, GemiDiscoveryRun, LiveSignalPeriod, Opportunity, OpportunityNote, OpportunitySignal,
    OpportunityTask, OrganizationNotification,
)
from .notifications import generate_due_task_notifications
from .organization_access import (
    OrganizationAccessDenied, add_authorized_opportunity_note, apply_authorized_company_do_not_contact,
    assign_authorized_opportunity, complete_authorized_opportunity_task, create_authorized_opportunity_task,
    get_authorized_company_opportunity_page, get_authorized_notifications, get_authorized_workspace_dashboard,
    get_authorized_workspace_opportunities, get_authorized_workspace_tasks, save_authorized_opportunity,
    set_authorized_opportunity_status,
)
from .organization_radars import RadarDefinition, create_organization_radar
from .organizations import create_organization
from .test_customer_workspace import WorkspaceTestCase
from .test_g4_shadow_cycle import FRONTIER, NEW
from .test_gemi_discovery import item, ready_cursor
from .test_ingestion_cycle import IngestionCycleTestCase
from .test_legacy_radar_sync import SyncTestCase
from .test_new_company_signals import discovery_run, make_company, observation
from .test_organization_radar_matching import T0, new_company_signal, snapshot

LIVE_ON = override_settings(GEMI_LIVE_SIGNALS_ENABLED=True)
VISIBLE = override_settings(GEMI_LIVE_OPPORTUNITIES_VISIBLE=True)
FLAG_COMBINATIONS = ((False, False), (True, False), (False, True), (True, True))


def open_period(at=None):
    """An authorized LIVE period as a cycle would have opened it."""
    at = at or timezone.now() - timedelta(hours=1)
    return LiveSignalPeriod.objects.create(opened_at=at, opened_by_cycle_at=at, is_open=True)


def live_world():
    return (sorted(CompanySignal.objects.filter(mode=LIVE).values_list("pk", "mode", "detected_at")),
            list(Opportunity.objects.order_by("pk").values()))


# --- defaults, model and transitions ---------------------------------------------------------------------------

class DefaultsTests(TestCase):
    def test_both_flags_default_off_and_the_legacy_transition_stays_on(self):
        self.assertFalse(settings.GEMI_LIVE_SIGNALS_ENABLED)
        self.assertFalse(settings.GEMI_LIVE_OPPORTUNITIES_VISIBLE)
        self.assertTrue(settings.GEMI_TRANSITION_LEGACY_ACCESS)
        source = open("config/settings.py", encoding="utf-8").read()
        for flag in ("GEMI_LIVE_SIGNALS_ENABLED", "GEMI_LIVE_OPPORTUNITIES_VISIBLE"):
            self.assertIn(f'os.environ.get("{flag}", "0") == "1"', source)
            self.assertIn(f"{flag}=0", open(".env.example", encoding="utf-8").read())
        self.assertEqual((live_cutover.current_signal_mode(), live_cutover.supported_pipeline_modes()),
                         (SHADOW, (SHADOW,)))
        self.assertIsNone(live_cutover.customer_visible_signal_mode())

    def test_the_mode_resolver_and_the_pipeline_modes_follow_the_flag(self):
        with LIVE_ON:
            self.assertEqual((live_cutover.current_signal_mode(), live_cutover.supported_pipeline_modes()),
                             (LIVE, (SHADOW, LIVE)))
        self.assertEqual(g2.PIPELINE_MODES, (SHADOW,))       # the unconditional part never grows

    def test_visibility_never_depends_on_the_persistent_state(self):
        open_period()
        with LIVE_ON:
            self.assertIsNone(live_cutover.customer_visible_signal_mode())
        with VISIBLE:
            LiveSignalPeriod.objects.update(is_open=False, closed_at=timezone.now(), closed_by_cycle_at=timezone.now())
            self.assertEqual(live_cutover.customer_visible_signal_mode(), LIVE)


class PeriodTests(TestCase):
    def test_transitions_are_idempotent_and_auditable(self):
        t1 = timezone.now()
        self.assertEqual(record_cycle_signal_mode(SHADOW, cycle_started_at=t1), UNCHANGED)
        self.assertFalse(LiveSignalPeriod.objects.exists())                       # never activated
        self.assertEqual(record_cycle_signal_mode(LIVE, cycle_started_at=t1, now=t1), OPENED)
        self.assertEqual(record_cycle_signal_mode(LIVE, cycle_started_at=t1 + timedelta(minutes=30)), UNCHANGED)
        t2 = t1 + timedelta(hours=2)
        self.assertEqual(record_cycle_signal_mode(SHADOW, cycle_started_at=t2, now=t2), CLOSED)
        self.assertEqual(record_cycle_signal_mode(SHADOW, cycle_started_at=t2), UNCHANGED)
        t3 = t2 + timedelta(hours=2)
        self.assertEqual(record_cycle_signal_mode(LIVE, cycle_started_at=t3, now=t3), OPENED)   # re-enable: new row
        rows = list(LiveSignalPeriod.objects.order_by("pk").values_list("opened_at", "closed_at", "is_open"))
        self.assertEqual(rows, [(t1, t2, False), (t3, None, True)])
        state = cutover_state()
        self.assertEqual((state.ever_activated, state.periods, state.first_opened_at, state.open_period_opened_at,
                          state.last_closed_at), (True, 2, t1, t3, t2))
        with self.assertRaises(ValueError):
            record_cycle_signal_mode("bogus", cycle_started_at=t3)

    def test_at_most_one_open_period_and_periods_are_never_deleted(self):
        open_period()
        with self.assertRaises(IntegrityError), transaction.atomic():
            open_period()
        with self.assertRaises(RuntimeError):
            LiveSignalPeriod.objects.get().delete()
        with self.assertRaises(IntegrityError), transaction.atomic():   # open rows carry no close time
            LiveSignalPeriod.objects.create(opened_at=timezone.now(), opened_by_cycle_at=timezone.now(),
                                            is_open=False)

    def test_authorized_and_unauthorized_live_signals(self):
        company = make_company("7001")
        before = new_company_signal(company, T0, mode=LIVE)               # no period yet: unauthorized
        period_start = timezone.now()
        open_period(period_start)
        during = new_company_signal(make_company("7002"), T0, mode=LIVE)
        record_cycle_signal_mode(SHADOW, cycle_started_at=timezone.now())
        after = new_company_signal(make_company("7003"), T0, mode=LIVE)   # period closed: unauthorized again
        CompanySignal.objects.filter(pk=before.pk).update(created_at=period_start - timedelta(minutes=5))
        self.assertEqual(set(unauthorized_live_signals().values_list("pk", flat=True)), {before.pk, after.pk})
        self.assertTrue(live_cutover.signal_is_authorized_live(during))
        self.assertFalse(live_cutover.signal_is_authorized_live(after))
        self.assertEqual((cutover_state().live_signals, cutover_state().unauthorized_live_signals), (3, 2))


# --- the ingestion cycle -----------------------------------------------------------------------------------------

class CycleModeTests(IngestionCycleTestCase):
    def numbers(self, start):
        return iter(range(start, start + 100_000, 1000))

    def test_flag_off_is_shadow_exactly_as_today(self):
        report, _ = self.run_cycle(item(NEW, self.today))
        self.assertEqual((report.signal_mode, report.live_period_transition), (SHADOW, UNCHANGED))
        self.assertEqual(set(CompanySignal.objects.values_list("mode", flat=True)), {SHADOW})
        self.assertFalse(LiveSignalPeriod.objects.exists())
        self.assertEqual(set(Opportunity.objects.values_list("latest_signal__mode", flat=True)), {SHADOW})
        self.assertEqual(GemiDiscoveryRun.objects.get(pk=report.discovery.run_id).policy["signal_mode"], SHADOW)
        self.assertEqual(report.summary()["signal_mode"], SHADOW)

    def test_flag_on_creates_live_new_company_signals_inside_a_recorded_period(self):
        with LIVE_ON:
            report, _ = self.run_cycle(item(NEW, self.today))
        self.assertEqual((report.status, report.signal_mode, report.live_period_transition), ("ok", LIVE, OPENED))
        signal = CompanySignal.objects.get(company__gemi_number=str(NEW))
        self.assertEqual(signal.mode, LIVE)
        self.assertTrue(live_cutover.signal_is_authorized_live(signal))
        period = LiveSignalPeriod.objects.get()
        self.assertEqual((period.opened_by_cycle_at, period.is_open), (report.started_at, True))
        run = GemiDiscoveryRun.objects.get(pk=report.discovery.run_id)
        self.assertEqual(run.policy["signal_mode"], LIVE)
        # the first LIVE cycle is queryable
        self.assertEqual(GemiDiscoveryRun.objects.filter(policy__signal_mode=LIVE).order_by("started_at").first(), run)
        self.assertEqual(Opportunity.objects.get(company=signal.company).latest_signal_id, signal.pk)
        self.assertEqual(mail.outbox, [])                       # no emitter, no email
        self.assertFalse(OrganizationNotification.objects.exists())
        # dark LIVE: the opportunity exists, the customer still sees nothing
        self.assertEqual(get_authorized_workspace_opportunities(self.owner, self.org.pk, view="all").rows, ())

    def test_the_mode_is_fixed_once_for_the_whole_cycle(self):
        first, second = NEW, NEW + 1000
        real = new_company_signals.produce_new_company_signal
        calls = []

        def flip_after_first(observation, **kwargs):
            calls.append(kwargs["mode"])
            result = real(observation, **kwargs)
            settings.GEMI_LIVE_SIGNALS_ENABLED = False     # a change mid-cycle must not split the cycle
            return result

        with LIVE_ON, patch.object(ingestion_cycle, "current_signal_mode",
                                   wraps=live_cutover.current_signal_mode) as resolver, \
                patch.object(new_company_signals, "produce_new_company_signal", side_effect=flip_after_first):
            report, _ = self.run_cycle(item(second, self.today), item(first, self.today))
        resolver.assert_called_once()
        self.assertEqual(calls, [LIVE, LIVE])
        self.assertEqual(set(CompanySignal.objects.values_list("mode", flat=True)), {LIVE})
        self.assertEqual(report.signal_mode, LIVE)

    def test_snapshot_change_producers_stay_shadow(self):
        from . import snapshot_change_signals

        source = open(snapshot_change_signals.__file__, encoding="utf-8").read()
        self.assertNotIn("current_signal_mode", source)
        self.assertIn("mode=SHADOW", source)

    def test_enable_rollback_shadow_cycles_and_re_enable(self):
        numbers = self.numbers(NEW)
        with LIVE_ON:
            self.run_cycle(item(next(numbers), self.today))
        live_before = live_world()
        live_ids = {pk for pk, _, _ in live_before[0]}
        self.assertEqual(len(live_ids), 1)

        # R2: the flag goes off -- ingestion keeps running, history stays LIVE and legitimate
        for _ in range(2):
            report, transport = self.run_cycle(item(next(numbers), self.today))
            self.assertEqual((report.status, report.signal_mode), ("ok", SHADOW))
            self.assertTrue(transport.calls)
        self.assertEqual(LiveSignalPeriod.objects.filter(is_open=True).count(), 0)
        self.assertEqual(set(CompanySignal.objects.filter(mode=LIVE).values_list("pk", flat=True)), live_ids)
        self.assertEqual(unauthorized_live_signals().count(), 0)
        self.assertEqual(preflight(certification=False).refusals, [])
        before_rows = {row["pk"] if "pk" in row else row["id"]: row for row in live_before[1]}
        for row in Opportunity.objects.filter(pk__in=before_rows).values():
            self.assertEqual(row, before_rows[row["id"]])                    # no demotion, no rewrite
        self.assertEqual(CompanySignal.objects.filter(mode=SHADOW).count(), 2)

        # re-enable: a new period, new LIVE signals, nothing old rewritten
        with LIVE_ON:
            report, _ = self.run_cycle(item(next(numbers), self.today))
        self.assertEqual(report.live_period_transition, OPENED)
        self.assertEqual(LiveSignalPeriod.objects.count(), 2)
        self.assertEqual(CompanySignal.objects.filter(mode=LIVE).count(), 2)
        self.assertEqual(CompanySignal.objects.filter(mode=SHADOW).count(), 2)
        self.assertEqual(unauthorized_live_signals().count(), 0)

    def test_authorized_history_never_stops_ingestion_but_an_unauthorized_signal_does(self):
        with LIVE_ON:
            self.run_cycle(item(NEW, self.today))
        report, _ = self.run_cycle(item(NEW + 1000, self.today))                  # flag off, history LIVE
        self.assertEqual(report.status, "ok")
        rogue = new_company_signal(Company.objects.get(gemi_number=str(FRONTIER)), T0, mode=LIVE)
        self.assertIn(rogue.pk, unauthorized_live_signals().values_list("pk", flat=True))
        with self.assertRaises(IngestionCycleRefused) as refused:
            self.run_cycle(item(NEW + 2000, self.today))
        self.assertIn("no unauthorized LIVE signal", str(refused.exception))

    def test_a_failure_to_record_the_period_stops_the_cycle_before_any_request_or_write(self):
        with LIVE_ON, patch.object(ingestion_cycle, "record_cycle_signal_mode", side_effect=RuntimeError("db")),                 self.assertLogs("gemiapp.ingestion_cycle", "ERROR"):
            report, transport = self.run_cycle(item(NEW, self.today))
        self.assertEqual(report.failed_phases[0][0], LIVE_CUTOVER_PHASE)
        self.assertEqual((transport.calls, CompanySignal.objects.count()), ([], 0))
        self.assertFalse(Company.objects.filter(gemi_number=str(NEW)).exists())

    def test_a_dry_run_records_no_period(self):
        with LIVE_ON:
            report, _ = self.run_cycle(item(NEW, self.today), dry_run=True)
        self.assertEqual((report.signal_mode, report.live_period_transition), (LIVE, ""))
        self.assertFalse(LiveSignalPeriod.objects.exists())
        self.assertFalse(CompanySignal.objects.exists())

    def test_the_g4_certification_cycle_refuses_while_live_processing_is_on(self):
        with LIVE_ON:
            names = [name for name, _ in preflight().refusals]
            self.assertIn("LIVE processing is off (SHADOW certification cycle)", names)
            self.assertNotIn("LIVE processing is off (SHADOW certification cycle)",
                             [name for name, _ in preflight(certification=False).refusals])
        self.assertEqual(preflight().refusals, [])


# --- the pipeline ------------------------------------------------------------------------------------------------

class PipelineModeTests(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        self.company_x = make_company("880011")
        snapshot(self.company_x, T0)
        self.shadow_row = self.live_opportunity(self.row.radar, company=self.company_x, mode=SHADOW)
        Opportunity.objects.filter(pk=self.shadow_row.pk).update(status="do_not_contact", assigned_to=self.maria)
        self.note = OpportunityNote.objects.create(organization=self.org, opportunity=self.shadow_row, body="σημείωση")
        self.task = OpportunityTask.objects.create(organization=self.org, opportunity=self.shadow_row, title="κλήση",
                                                   due_on=timezone.localdate())

    def test_a_live_signal_recaptures_the_shadow_backed_row_in_place(self):
        open_period()
        live = new_company_signal(self.company_x, T0 + timedelta(hours=1), mode=LIVE)
        before = Opportunity.objects.get(pk=self.shadow_row.pk)
        with LIVE_ON:
            run = g2.process_company_signal(live)
            again = g2.process_company_signal(live)
        self.assertTrue(run.processed)
        self.assertEqual(Opportunity.objects.filter(company=self.company_x, radar=self.row.radar).count(), 1)
        row = Opportunity.objects.get(pk=self.shadow_row.pk)
        self.assertEqual((row.latest_signal_id, row.first_signal_id), (live.pk, before.first_signal_id))
        self.assertEqual((row.status, row.assigned_to_id), ("do_not_contact", self.maria.pk))
        self.assertEqual((row.notes.get(), row.tasks.get()), (self.note, self.task))
        self.assertEqual(OpportunitySignal.objects.filter(opportunity=row, signal=live).count(), 1)
        self.assertEqual((again.created, again.updated, again.unchanged >= 1), (0, 0, True))

    def test_a_shadow_signal_never_recaptures_a_live_backed_row(self):
        open_period()
        live = new_company_signal(self.company_x, T0 + timedelta(hours=1), mode=LIVE)
        with LIVE_ON:
            g2.process_company_signal(live)
        row = list(Opportunity.objects.filter(pk=self.shadow_row.pk).values())
        run = g2.process_company_signal(new_company_signal(self.company_x, T0 + timedelta(hours=2)))
        self.assertGreaterEqual(run.skipped_live_backed, 1)
        self.assertEqual(list(Opportunity.objects.filter(pk=self.shadow_row.pk).values()), row)

    def test_live_signals_are_skipped_without_the_flag_or_outside_a_period(self):
        live = new_company_signal(self.company_x, T0 + timedelta(hours=1), mode=LIVE)
        before = list(Opportunity.objects.order_by("pk").values())
        self.assertEqual(g2.process_company_signal(live).skipped_reason, g2.LIVE_NOT_ENABLED)
        with LIVE_ON:
            self.assertEqual(g2.process_company_signal(live).skipped_reason, g2.LIVE_NOT_AUTHORIZED)
        self.assertEqual(list(Opportunity.objects.order_by("pk").values()), before)

    def test_a_non_entitled_organization_receives_no_live_opportunity(self):
        from django.contrib.auth.models import User

        free_owner = User.objects.create_user("free-live@example.com", "free-live@example.com", "x")
        free_org = create_organization(owner=free_owner, name="Free org").organization
        create_organization_radar(free_org, RadarDefinition(name="Όλες", active=True, signal_types=("new_company",)))
        open_period()
        company = make_company("880022")
        snapshot(company, T0)
        with LIVE_ON:
            run = g2.process_company_signal(new_company_signal(company, T0, mode=LIVE))
        self.assertGreaterEqual(run.skipped_not_entitled, 1)
        self.assertFalse(Opportunity.objects.filter(organization=free_org).exists())

    def test_the_shadow_replay_never_selects_a_live_signal_and_the_live_replay_is_gated(self):
        new_company_signal(self.company_x, timezone.now() - timedelta(hours=1), mode=LIVE)
        out = StringIO()
        call_command("process_shadow_signals", "--since-hours", "24", stdout=out)
        self.assertIn("shadow signals selected=0", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("process_live_signals", stdout=StringIO())
        open_period(timezone.now() - timedelta(days=1))
        with LIVE_ON:
            out = StringIO()
            call_command("process_live_signals", "--dry-run", stdout=out)
        self.assertIn("live signals selected=1", out.getvalue())


# --- customer visibility -----------------------------------------------------------------------------------------

class VisibilityTests(WorkspaceTestCase):
    """The workspace fixture holds LIVE-backed opportunities, a task and the full role matrix."""

    def setUp(self):
        super().setUp()
        with VISIBLE:
            assign_authorized_opportunity(self.owner, self.org.pk, self.row.pk, self.maria.pk)
            self.task = create_authorized_opportunity_task(self.owner, self.org.pk, self.row.pk, "Κλήση",
                                                           timezone.localdate().isoformat(),
                                                           assignee_membership_id=self.maria.pk)
            self.task = OpportunityTask.objects.get(pk=self.task.task_id)

    def surfaces(self):
        dashboard = get_authorized_workspace_dashboard(self.owner, self.org.pk)
        return {
            "dashboard": (dashboard.active_opportunities, dashboard.open_tasks, len(dashboard.top_opportunities),
                          len(dashboard.attention_tasks)),
            "opportunities": len(get_authorized_workspace_opportunities(self.owner, self.org.pk, view="all").rows),
            "tasks": len(get_authorized_workspace_tasks(self.owner, self.org.pk).tasks),
            "notifications": [entry.available for entry in get_authorized_notifications(self.maria_user, self.org.pk)],
        }

    def test_with_visibility_off_every_surface_and_mutation_hides_live_rows(self):
        self.assertTrue(CompanySignal.objects.filter(mode=LIVE).exists())
        surfaces = self.surfaces()
        self.assertEqual(surfaces["dashboard"], (0, 0, 0, 0))
        self.assertEqual((surfaces["opportunities"], surfaces["tasks"]), (0, 0))
        self.assertTrue(surfaces["notifications"])                       # the ASSIGNMENT notification exists...
        self.assertFalse(any(surfaces["notifications"]))                 # ...but shows no opportunity
        self.assertEqual(get_authorized_workspace_opportunities(self.owner, self.org.pk, view="saved").rows, ())
        for name in ("organization_dashboard", "organization_opportunities", "organization_tasks"):
            self.assertEqual(self.status_of(self.owner, name), 200)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("organization_company_opportunity",
                                                 args=[self.org.pk, self.company.pk])).status_code, 404)
        before = list(Opportunity.objects.order_by("pk").values())
        for call in (
            lambda: get_authorized_company_opportunity_page(self.owner, self.org.pk, self.company.pk),
            lambda: save_authorized_opportunity(self.owner, self.org.pk, self.row.pk),
            lambda: assign_authorized_opportunity(self.owner, self.org.pk, self.row.pk, self.maria.pk),
            lambda: set_authorized_opportunity_status(self.owner, self.org.pk, self.row.pk, "contacted"),
            lambda: add_authorized_opportunity_note(self.owner, self.org.pk, self.row.pk, "κείμενο"),
            lambda: create_authorized_opportunity_task(self.owner, self.org.pk, self.row.pk, "Νέα",
                                                       timezone.localdate().isoformat()),
            lambda: complete_authorized_opportunity_task(self.owner, self.org.pk, self.row.pk, self.task.pk),
            lambda: apply_authorized_company_do_not_contact(self.owner, self.org.pk, self.company.pk, "manual",
                                                            True),
        ):
            with self.assertRaises(OrganizationAccessDenied):
                call()
        self.assertEqual(list(Opportunity.objects.order_by("pk").values()), before)
        self.assertIsNone(OpportunityTask.objects.get(pk=self.task.pk).completed_at)

    def test_task_due_stays_silent_while_visibility_is_off(self):
        self.assertEqual(generate_due_task_notifications().created, 0)
        self.assertFalse(OrganizationNotification.objects.filter(notification_type="task_due").exists())
        with VISIBLE:
            self.assertEqual(generate_due_task_notifications().created, 1)

    def test_with_visibility_on_every_surface_shows_them_consistently(self):
        with VISIBLE:
            surfaces = self.surfaces()
            self.assertEqual(surfaces["dashboard"][0], 1)
            self.assertEqual(surfaces["dashboard"][1], 1)
            self.assertEqual((surfaces["opportunities"], surfaces["tasks"]), (1, 1))
            self.assertTrue(all(surfaces["notifications"]))
            self.assertTrue(get_authorized_company_opportunity_page(self.owner, self.org.pk, self.company.pk))
            set_authorized_opportunity_status(self.owner, self.org.pk, self.row.pk, "contacted")
            add_authorized_opportunity_note(self.owner, self.org.pk, self.row.pk, "ορατή")
            self.client.force_login(self.owner)
            self.assertEqual(self.client.get(reverse("organization_company_opportunity",
                                                     args=[self.org.pk, self.company.pk])).status_code, 200)
        self.assertEqual(Opportunity.objects.get(pk=self.row.pk).status, "contacted")

    def test_shadow_rows_stay_invisible_even_with_visibility_on(self):
        company, shadow_row = self.new_company(mode=SHADOW)
        with VISIBLE:
            with self.assertRaises(OrganizationAccessDenied):
                get_authorized_company_opportunity_page(self.owner, self.org.pk, company.pk)
            ids = [row.company_id for row in
                   get_authorized_workspace_opportunities(self.owner, self.org.pk, view="all").rows]
        self.assertNotIn(company.pk, ids)

    def test_staff_and_superusers_get_no_bypass_with_visibility_on(self):
        from django.contrib.auth.models import User

        root = User.objects.create_superuser("root-live", "root-live@example.com", "StrongPass123")
        with VISIBLE:
            self.assertEqual(self.status_of(root, "organization_dashboard"), 404)
            with self.assertRaises(OrganizationAccessDenied):
                get_authorized_company_opportunity_page(root, self.org.pk, self.company.pk)


class LegacyCoexistenceTests(WorkspaceTestCase):
    def test_the_legacy_navigation_stays_and_only_its_copy_follows_visibility(self):
        html = self.html(self.owner, "organization_dashboard")
        self.assertIn("until opportunities go live", html)
        self.assertNotIn("PREVIOUS SERVICE", html)
        with VISIBLE:
            html = self.html(self.owner, "organization_dashboard")
            empty = self.html(self.owner, "organization_opportunities", status="won")
        self.assertIn("PREVIOUS SERVICE", html)
        self.assertNotIn("until opportunities go live", html)
        for link in (reverse("dashboard"), reverse("lead_list")):
            self.assertIn(f'href="{link}"', html)                       # Signals and Leads remain reachable
        self.assertNotIn("data-transition-pointer", empty)

    def test_a_free_owner_keeps_the_paywall_and_the_signals_landing_under_every_flag(self):
        from django.contrib.auth.models import User

        free_owner = User.objects.create_user("free-ws@example.com", "free-ws@example.com", "x")
        free_org = create_organization(owner=free_owner, name="Free WS").organization
        for signals, visible in FLAG_COMBINATIONS:
            with override_settings(GEMI_LIVE_SIGNALS_ENABLED=signals, GEMI_LIVE_OPPORTUNITIES_VISIBLE=visible):
                self.client.force_login(free_owner)
                self.assertRedirects(self.client.get(reverse("product_home")), reverse("dashboard"),
                                     fetch_redirect_response=False)
                response = self.client.get(reverse("organization_dashboard", args=[free_org.pk]))
                self.assertRedirects(response, reverse("pricing"), fetch_redirect_response=False)


class RadarMirrorTests(SyncTestCase):
    def test_the_customer_radar_mirror_is_unchanged_under_every_flag_combination(self):
        for index, (signals, visible) in enumerate(FLAG_COMBINATIONS):
            with override_settings(GEMI_LIVE_SIGNALS_ENABLED=signals, GEMI_LIVE_OPPORTUNITIES_VISIBLE=visible):
                self.assertTrue(mirror_enabled())
                radar = self.create_via_ui(name=f"Radar {index}")
                mirror = self.mirror_of(radar)
                self.assertEqual((mirror.name, mirror.active), (f"Radar {index}", True))
                self.edit_via_ui(radar, name=f"Radar {index} v2", activity_codes=["47110000"])
                self.assertEqual(self.criteria(mirror)["name"], f"Radar {index} v2")
                self.assertEqual(self.criteria(mirror)["kads"], {self.retail.pk})


# --- readiness ---------------------------------------------------------------------------------------------------

@override_settings(GEMI_DISCOVERY_V2_ENABLED=True, GEMI_COLLECTOR_ENABLED=True)
class ReadinessTests(IngestionCycleTestCase):
    def setUp(self):
        super().setUp()
        # post_migrate already registers it while ingest is enabled; make sure it is exactly the expected one
        Schedule.objects.filter(func=INGESTION_TASK).delete()
        Schedule.objects.create(func=INGESTION_TASK, cron=EXPECTED_CRON, schedule_type=Schedule.CRON)

    def blocking(self, report):
        return [name for status, name, _ in report.checks if status == BLOCKED]

    def counts(self):
        from django.apps import apps as django_apps

        return {model.__name__: model.objects.count() for model in django_apps.get_app_config("gemiapp").get_models()}

    def test_a_clean_shadow_world_is_ready_and_nothing_is_written(self):
        self.run_cycle(item(NEW, self.today))
        before = self.counts()
        report = build_readiness_report()
        self.assertEqual(report.verdict, READY, self.blocking(report))
        self.assertEqual(self.counts(), before)
        names = [name for _, name, _ in report.checks]
        for expected in ("signals by mode/type", "opportunities by latest_signal mode/status",
                         "organizations entitled / total", "duplicate Opportunity (organization, radar, company)",
                         "duplicate OpportunitySignal links", "Radar / Organization mismatches",
                         "Company / latest_signal mismatches", "Company / first_signal mismatches",
                         "latest_signal missing from OpportunitySignal history",
                         "opportunities without exactly 5 score components", "opportunities of inactive Radars",
                         "Radar migration tombstones", "opportunities of organizations not entitled now",
                         "cutover state", "GEMI_LIVE_SIGNALS_ENABLED", "GEMI_LIVE_OPPORTUNITIES_VISIBLE",
                         "ingestion schedule", "GEMI_TRANSITION_LEGACY_ACCESS (legacy navigation + Radar mirror)",
                         "NEW_COMPANY signals_would_create (all eligible discovery evidence)",
                         "SHADOW replay dry run (168h)", "unauthorized LIVE signals"):
            self.assertIn(expected, names)

    def test_an_unauthorized_live_signal_blocks(self):
        new_company_signal(Company.objects.get(gemi_number=str(FRONTIER)), T0, mode=LIVE)
        self.assertIn("unauthorized LIVE signals", self.blocking(build_readiness_report()))

    def test_authorized_live_history_does_not_block(self):
        with override_settings(GEMI_LIVE_SIGNALS_ENABLED=True):
            self.run_cycle(item(NEW, self.today))
        report = build_readiness_report()
        self.assertNotIn("unauthorized LIVE signals", self.blocking(report))

    def test_old_eligible_evidence_without_a_signal_blocks_until_it_is_materialised(self):
        make_company("990001")
        observation(discovery_run(), gemi_number="990001")
        self.assertIn("NEW_COMPANY signals_would_create (all eligible discovery evidence)",
                      self.blocking(build_readiness_report()))
        self.assertFalse(CompanySignal.objects.filter(company__gemi_number="990001").exists())   # dry run only
        call_command("materialize_new_company_signals", stdout=StringIO())
        self.assertEqual(CompanySignal.objects.get(company__gemi_number="990001").mode, SHADOW)
        self.assertNotIn("NEW_COMPANY signals_would_create (all eligible discovery evidence)",
                         self.blocking(build_readiness_report()))

    def test_a_shadow_replay_backlog_blocks_until_it_is_replayed(self):
        report, _ = self.run_cycle(item(NEW, self.today))
        Opportunity.objects.all().delete()          # simulate opportunity work a previous cycle lost
        self.assertIn("SHADOW replay dry run (168h)", self.blocking(build_readiness_report()))
        call_command("process_shadow_signals", "--since-hours", "168", stdout=StringIO())
        self.assertNotIn("SHADOW replay dry run (168h)", self.blocking(build_readiness_report()))

    def test_schedule_and_legacy_flag_are_required(self):
        Schedule.objects.all().delete()
        with override_settings(GEMI_TRANSITION_LEGACY_ACCESS=False):
            blocking = self.blocking(build_readiness_report())
        self.assertIn("ingestion schedule", blocking)
        self.assertIn("GEMI_TRANSITION_LEGACY_ACCESS (legacy navigation + Radar mirror)", blocking)

    def test_the_command_prints_the_verdict_and_exits_non_zero_when_blocked(self):
        self.run_cycle(item(NEW, self.today))
        out = StringIO()
        try:
            call_command("report_live_cutover_readiness", stdout=out)
        except CommandError:
            self.fail(out.getvalue())
        self.assertIn("READINESS: READY", out.getvalue())
        new_company_signal(Company.objects.get(gemi_number=str(FRONTIER)), T0, mode=LIVE)
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("report_live_cutover_readiness", stdout=out)
        self.assertIn("READINESS: BLOCKED", out.getvalue())
        self.assertIn("[BLOCKED] unauthorized LIVE signals", out.getvalue())


class SourceGuardTests(TestCase):
    def test_every_customer_scope_goes_through_the_one_visibility_helper(self):
        source = open("gemiapp/organization_access.py", encoding="utf-8").read()
        self.assertNotIn("latest_signal__mode=LIVE", source)
        self.assertNotIn("latest_signal_mode=LIVE", source)
        self.assertNotIn("latest_signal__mode", open("gemiapp/notifications.py", encoding="utf-8").read())
        self.assertNotIn("GEMI_LIVE_OPPORTUNITIES_VISIBLE", source)
        for path in ("gemiapp/organization_access.py", "gemiapp/notifications.py", "gemiapp/organization_views.py"):
            self.assertNotIn("settings.GEMI_LIVE", open(path, encoding="utf-8").read(), path)

    def test_nothing_promotes_or_demotes_a_signal(self):
        for path in ("gemiapp/live_cutover.py", "gemiapp/ingestion_cycle.py", "gemiapp/opportunity_pipeline.py",
                     "gemiapp/live_cutover_readiness.py", "gemiapp/signal_replay.py", "gemiapp/new_company_signals.py"):
            source = open(path, encoding="utf-8").read()
            self.assertNotIn("promote_company_signal(", source, path)
            self.assertNotIn(".update(mode", source, path)
