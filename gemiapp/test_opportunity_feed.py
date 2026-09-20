"""Tests for the opportunity feed read model (C9, gemiapp.opportunity_feed).

The feed reads persisted C8 opportunities only: one card per (organization, company), frozen captures, filters
applied to underlying opportunities before aggregation, keyset pagination. It writes nothing.
"""

import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.signing import TimestampSigner
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from . import opportunity_feed as c9
from .company_contact import extract_company_contact_phones
from .company_signals import DISCOVERY, SNAPSHOT_DIFF, record_company_signal
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    Company, CompanyMonitoring, CompanySignal, CompanySnapshot, CustomerRadar, DigestDelivery, Opportunity,
    OpportunityScoreComponent, OpportunityScoreEvidence, OpportunitySignal, OrganizationRadar, RadarMatch,
    UserCompanyLead, UserSubscription,
)
from .opportunity_feed import (
    DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, FeedCursor, FeedError, FeedFilters, FeedRadarOpportunity,
    OpportunityFeedCard, OpportunityFeedPage, get_opportunity_feed,
)
from .opportunity_scoring import OPPORTUNITY_SCORE_RULE_VERSION, score_class_for
from .organization_radar_matching import ORGANIZATION_RADAR_MATCH_RULE_VERSION
from .organization_radars import RadarDefinition, create_organization_radar, replace_organization_radar
from .services import company_matches_radar, eligible_radars, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_opportunities import OpportunityTestCase
from .test_organization_radar_matching import (
    ADDRESS_SENTINEL, PRIVATE, T0, make_company, new_company_signal, organization, snapshot,
)

_events = iter(range(1, 10_000_000))


class FeedTestCase(OpportunityTestCase):
    def setUp(self):
        super().setUp()
        self.radar_a = self.radar(name="Radar A", signal_types=("new_company",))
        self.radar_b = self.radar(name="Radar B", kads=(self.r.kad_2026,))

    def signal(self, company, at, signal_type="new_company"):
        source = DISCOVERY if signal_type == "new_company" else SNAPSHOT_DIFF
        signal, _ = record_company_signal(company=company, signal_type=signal_type, source_type=source,
                                          event_key={"feed": next(_events)}, detected_at=at)
        return signal

    def opportunity(self, radar, company, *, score, at=T0, signals=None, status="new", kads=(), regions=(),
                    reason="explicit_signal_type_match"):
        """A persisted C8 row with a chosen frozen capture: the feed only ever reads such rows."""
        signals = signals or [self.signal(company, at)]
        latest = max(signals, key=lambda s: (s.detected_at, s.pk))
        row = Opportunity.objects.create(
            organization=radar.organization, radar=radar, company=company, first_signal=signals[0],
            latest_signal=latest, score=score, score_class=score_class_for(score),
            score_rule_version=OPPORTUNITY_SCORE_RULE_VERSION, match_rule_version=ORGANIZATION_RADAR_MATCH_RULE_VERSION,
            scored_as_of=latest.detected_at + timedelta(hours=1), primary_reason_code=reason, status=status,
        )
        for signal in signals:
            OpportunitySignal.objects.create(opportunity=row, signal=signal, score=score,
                                             scored_as_of=row.scored_as_of)
        for code, kind, values in (("industry_fit", "kad", kads), ("geographic_fit", "region", regions)):
            if values:
                component = OpportunityScoreComponent.objects.create(
                    opportunity=row, code=code, awarded_points=1, max_points=30, reason_code="x",
                    position=0 if code == "industry_fit" else 2)
                for index, value in enumerate(values):
                    fields = ({"kad_code": value[0], "kad_version": value[1]} if kind == "kad"
                              else {"region_level": value[0], "region_source_id": value[1]})
                    OpportunityScoreEvidence.objects.create(component=component, kind=kind, position=index, **fields)
        return row

    def feed(self, filters=None, org=None, **kwargs):
        return get_opportunity_feed(org or self.org, filters, **kwargs)

    def companies(self, page):
        return [card.company_id for card in page.cards]


# --- contract -------------------------------------------------------------------------------------

class ContractTests(TestCase):
    def test_no_model_no_migration_and_immutable_results(self):
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("gemiapp").get_models()}
        self.assertFalse([n for n in names if "Feed" in n])
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(max(name for app, name in loader.disk_migrations if app == "gemiapp"),
                         "0055_gemi_request_attempt")  # G6 owns 0055 (GemiRequestAttempt); any newer migration must update this pin deliberately
        for cls in (FeedFilters, FeedCursor, FeedRadarOpportunity, OpportunityFeedCard, OpportunityFeedPage):
            self.assertTrue(dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen, cls)
        self.assertEqual((DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE), (50, 200))

    def test_the_read_model_writes_nothing_and_never_matches_scores_or_explains(self):
        code = inspect.getsource(c9).split('"""', 2)[2]
        for forbidden in (".save(", ".create(", ".update(", ".delete(", "bulk_", "get_or_create", "set_opportunity_status",
                          "organization_radar_matching", "opportunity_scoring", "opportunity_score_breakdown",
                          "build_opportunity_score_breakdown", "materialize", "timezone.now(", "date.today(",
                          "request.user", "session", "raw_data", "gemi_phones", "phone", "email", "person",
                          "vat_number", "address", "CustomerRadar", "RadarMatch", "UserCompanyLead", "get_gemi_client",
                          "stripe", "send_mail", "urllib", "requests"):
            self.assertNotIn(forbidden, code, forbidden)

    def test_card_fields_carry_no_private_or_owner_data(self):
        names = {f.name for cls in (OpportunityFeedCard, FeedRadarOpportunity) for f in dataclasses.fields(cls)}
        for forbidden in ("company_name", "phone", "email", "person", "address", "vat", "raw", "user", "owner",
                          "subscription", "breakdown", "components"):
            self.assertFalse([n for n in names if forbidden in n], forbidden)

    def test_nothing_in_the_product_calls_the_feed(self):
        for path in ("gemiapp/urls.py", "gemiapp/views.py", "config/urls.py", "gemiapp/tasks.py", "gemiapp/apps.py",
                     "gemiapp/services.py", "gemiapp/billing.py", "gemiapp/forms.py", "config/settings.py",
                     "render.yaml"):
            self.assertNotIn("opportunity_feed", open(path, encoding="utf-8").read(), path)


# --- aggregation and ordering ----------------------------------------------------------------------

class AggregationTests(FeedTestCase):
    def test_01_one_opportunity_is_one_card(self):
        row = self.opportunity(self.radar_a, self.company, score=80)
        page = self.feed()
        self.assertEqual(len(page.cards), 1)
        card = page.cards[0]
        self.assertEqual((card.company_id, card.primary_opportunity_id, card.score, card.score_class, card.status),
                         (self.company.pk, row.pk, 80, "high", "new"))
        self.assertEqual((card.opportunity_count, card.relevant_signal_count), (1, 1))
        self.assertEqual(card.company_gemi_number, self.company.gemi_number)
        self.assertIsNone(page.next_cursor)

    def test_02_two_radars_on_one_company_make_one_card_with_both_children(self):
        a = self.opportunity(self.radar_a, self.company, score=75)
        b = self.opportunity(self.radar_b, self.company, score=90)
        card, = self.feed().cards
        self.assertEqual(card.opportunity_count, 2)
        self.assertEqual([c.opportunity_id for c in card.opportunities], [b.pk, a.pk])
        self.assertEqual({c.radar_name for c in card.opportunities}, {"Radar A", "Radar B"})
        self.assertEqual(Opportunity.objects.count(), 2)  # rows are never merged

    def test_03_two_companies_make_two_cards(self):
        other = make_company("500100")
        self.opportunity(self.radar_a, self.company, score=60)
        self.opportunity(self.radar_a, other, score=70)
        self.assertEqual(self.companies(self.feed()), [other.pk, self.company.pk])

    def test_04_a_signal_shared_by_two_radars_counts_once(self):
        shared = self.signal(self.company, T0)
        self.opportunity(self.radar_a, self.company, score=60, signals=[shared])
        self.opportunity(self.radar_b, self.company, score=70, signals=[shared])
        card, = self.feed().cards
        self.assertEqual((card.opportunity_count, card.relevant_signal_count), (2, 1))

    def test_05_distinct_signals_are_counted_across_radars(self):
        first, second, third = (self.signal(self.company, T0 + timedelta(hours=h)) for h in (0, 1, 2))
        self.opportunity(self.radar_a, self.company, score=60, signals=[first, second])
        self.opportunity(self.radar_b, self.company, score=70, signals=[second, third])
        self.assertEqual(self.feed().cards[0].relevant_signal_count, 3)

    def test_06_the_highest_score_is_primary_and_every_top_level_field_comes_from_it(self):
        self.opportunity(self.radar_a, self.company, score=75, status="saved", reason="explicit_signal_type_match",
                         at=T0 + timedelta(days=3))
        high = self.opportunity(self.radar_b, self.company, score=90, status="new", reason="exact_kad_match", at=T0)
        card, = self.feed().cards
        self.assertEqual((card.primary_opportunity_id, card.primary_radar_id, card.score, card.score_class,
                          card.status, card.primary_reason_code, card.scored_as_of, card.primary_signal_detected_at),
                         (high.pk, self.radar_b.pk, 90, "priority", "new", "exact_kad_match", high.scored_as_of, T0))
        # The company's freshest event is kept distinct from the primary's.
        self.assertEqual(card.latest_company_signal_detected_at, T0 + timedelta(days=3))

    def test_07_on_equal_score_the_freshest_signal_wins(self):
        self.opportunity(self.radar_a, self.company, score=80, at=T0)
        fresher = self.opportunity(self.radar_b, self.company, score=80, at=T0 + timedelta(hours=5))
        self.assertEqual(self.feed().cards[0].primary_opportunity_id, fresher.pk)

    def test_08_on_an_exact_tie_the_lowest_id_wins(self):
        shared = self.signal(self.company, T0)
        first = self.opportunity(self.radar_a, self.company, score=80, signals=[shared])
        self.opportunity(self.radar_b, self.company, score=80, signals=[shared])
        self.assertEqual(self.feed().cards[0].primary_opportunity_id, first.pk)

    def test_09_cards_sort_by_score_then_freshness_then_id(self):
        companies = [make_company(f"5002{i:02d}") for i in range(4)]
        self.opportunity(self.radar_a, companies[0], score=70, at=T0)
        self.opportunity(self.radar_a, companies[1], score=90, at=T0)
        self.opportunity(self.radar_a, companies[2], score=70, at=T0 + timedelta(days=1))
        shared_time = T0 + timedelta(days=1)
        self.opportunity(self.radar_a, companies[3], score=70, at=shared_time)
        order = self.companies(self.feed())
        self.assertEqual(order, [companies[1].pk, companies[2].pk, companies[3].pk, companies[0].pk])


# --- frozen captures -------------------------------------------------------------------------------

class FrozenTests(FeedTestCase):
    def test_10_a_radar_edit_never_changes_a_card(self):
        radar = self.radar(name="real", **self.everything())
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        self.materialize(signal, radar)
        before = self.feed(FeedFilters(radar_ids=(radar.pk,))).cards[0]
        replace_organization_radar(self.org, radar, RadarDefinition(
            name="real", active=True, kads=(self.r.kad_other,), regions=(self.r.thessaloniki,)))
        after = self.feed(FeedFilters(radar_ids=(radar.pk,))).cards[0]
        self.assertEqual(after, before)
        self.assertEqual((after.score, after.score_class, after.primary_reason_code), (100, "priority", "exact_kad_match"))

    def test_scores_never_decay_with_the_clock(self):
        self.opportunity(self.radar_a, self.company, score=90, at=T0)
        first = self.feed()
        with patch("django.utils.timezone.now", return_value=T0 + timedelta(days=400)):
            later = self.feed()
        self.assertEqual(later, first)
        self.assertEqual(later.cards[0].score, 90)


# --- filters ---------------------------------------------------------------------------------------

class FilterTests(FeedTestCase):
    def test_11_score_bounds_are_inclusive(self):
        rows = {score: self.opportunity(self.radar_a, make_company(f"5003{score:02d}"), score=score)
                for score in (74, 75, 76)}
        page = self.feed(FeedFilters(min_score=75, max_score=75))
        self.assertEqual([c.primary_opportunity_id for c in page.cards], [rows[75].pk])
        self.assertEqual(len(self.feed(FeedFilters(min_score=75)).cards), 2)
        self.assertEqual(len(self.feed(FeedFilters(max_score=75)).cards), 2)

    def test_12_score_class_filter_keeps_only_matching_children_and_repicks_the_primary(self):
        self.opportunity(self.radar_a, self.company, score=92)          # priority
        high = self.opportunity(self.radar_b, self.company, score=80)   # high
        card, = self.feed(FeedFilters(score_classes=("high",))).cards
        self.assertEqual((card.primary_opportunity_id, card.score, card.opportunity_count), (high.pk, 80, 1))
        self.assertEqual(self.feed(FeedFilters(score_classes=("low",))).cards, ())

    def test_13_radar_filter_scopes_the_card_to_that_radar(self):
        self.opportunity(self.radar_a, self.company, score=90)
        b = self.opportunity(self.radar_b, self.company, score=60)
        card, = self.feed(FeedFilters(radar_ids=(self.radar_b.pk,))).cards
        self.assertEqual(([c.opportunity_id for c in card.opportunities], card.score), ([b.pk], 60))

    def test_14_status_filter_keeps_the_card_coherent(self):
        self.opportunity(self.radar_a, self.company, score=90, status="new")
        saved = self.opportunity(self.radar_b, self.company, score=60, status="saved")
        card, = self.feed(FeedFilters(statuses=("saved", "won"))).cards
        self.assertEqual((card.primary_opportunity_id, card.status, card.opportunity_count), (saved.pk, "saved", 1))

    def test_15_signal_type_filter_uses_only_contributing_signals(self):
        new = self.signal(self.company, T0)
        changed = self.signal(self.company, T0 + timedelta(days=1), "status_changed")
        other = make_company("500400")
        unrelated = self.signal(other, T0, "status_changed")  # history not contributing to any opportunity
        with_change = self.opportunity(self.radar_a, self.company, score=70, signals=[new, changed])
        self.opportunity(self.radar_a, other, score=80, signals=[self.signal(other, T0)])
        self.assertIsNotNone(unrelated.pk)
        page = self.feed(FeedFilters(signal_types=("status_changed",)))
        self.assertEqual([c.primary_opportunity_id for c in page.cards], [with_change.pk])

    def test_16_kad_filter_uses_exact_frozen_evidence_only(self):
        exact = self.opportunity(self.radar_b, self.company, score=70, kads=(("62010000", "kad_2026"),))
        other = make_company("500500")
        self.opportunity(self.radar_b, other, score=80, kads=(("62010000", "kad_2008"),))
        page = self.feed(FeedFilters(kads=(("62010000", "kad_2026"),)))
        self.assertEqual([c.primary_opportunity_id for c in page.cards], [exact.pk])
        self.assertEqual(self.feed(FeedFilters(kads=(("620100", "kad_2026"),))).cards, ())  # no prefix
        # The company's *current* state is irrelevant: a later snapshot with other KADs changes nothing.
        snapshot(self.company, T0 + timedelta(days=9), kads=(("47191002", "kad_2026"),))
        self.assertEqual([c.primary_opportunity_id for c in self.feed(FeedFilters(kads=(("62010000", "kad_2026"),))).cards],
                         [exact.pk])

    def test_17_region_filter_uses_exact_frozen_evidence_and_level(self):
        pref = self.opportunity(self.radar_a, self.company, score=70, regions=(("prefecture", "61190"),))
        other = make_company("500600")
        self.opportunity(self.radar_a, other, score=80, regions=(("municipality", "61190"),))
        page = self.feed(FeedFilters(regions=(("prefecture", "61190"),)))
        self.assertEqual([c.primary_opportunity_id for c in page.cards], [pref.pk])

    def test_18_date_filter_is_inclusive_on_latest_signal_detection(self):
        rows = {offset: self.opportunity(self.radar_a, make_company(f"5007{offset:02d}"), score=60,
                                         at=T0 + timedelta(hours=offset)) for offset in (0, 1, 2)}
        page = self.feed(FeedFilters(detected_from=T0 + timedelta(hours=1), detected_to=T0 + timedelta(hours=1)))
        self.assertEqual([c.primary_opportunity_id for c in page.cards], [rows[1].pk])
        self.assertEqual(len(self.feed(FeedFilters(detected_from=T0 + timedelta(hours=1))).cards), 2)

    def test_19_filters_combine_with_and_and_values_with_or(self):
        self.opportunity(self.radar_a, self.company, score=90, status="new")
        target = self.opportunity(self.radar_b, make_company("500800"), score=80, status="saved")
        self.opportunity(self.radar_b, make_company("500801"), score=50, status="saved")
        page = self.feed(FeedFilters(radar_ids=(self.radar_b.pk,), statuses=("new", "saved"), min_score=75))
        self.assertEqual([c.primary_opportunity_id for c in page.cards], [target.pk])

    def test_invalid_filters_are_refused(self):
        other_org = organization("Other feed", "other-feed@example.com")
        foreign = create_organization_radar(other_org, RadarDefinition(name="f", active=True,
                                                                       signal_types=("new_company",)))
        for bad in (FeedFilters(score_classes=("hot",)), FeedFilters(statuses=("archived",)),
                    FeedFilters(signal_types=("gossip",)), FeedFilters(min_score=-1), FeedFilters(max_score=101),
                    FeedFilters(min_score=80, max_score=70), FeedFilters(kads=(("1",),)),
                    FeedFilters(regions=(("city", "1"),)), FeedFilters(detected_from=datetime(2026, 9, 1)),
                    FeedFilters(radar_ids=(foreign.pk,)), FeedFilters(radar_ids=(999_999,)),
                    FeedFilters(score_classes=["high"])):
            with self.assertRaises(FeedError, msg=bad):
                self.feed(bad)
        for kwargs in ({"limit": 0}, {"limit": 201}, {"limit": True}, {"cursor": "not-a-cursor"}):
            with self.assertRaises(FeedError):
                self.feed(**kwargs)
        for bad_org in (None, self.radar_a, organization.__class__):
            with self.assertRaises(FeedError):
                get_opportunity_feed(bad_org)


# --- tenancy, pagination, efficiency ---------------------------------------------------------------

class TenancyAndPagingTests(FeedTestCase):
    def test_20_21_organizations_see_only_their_own_cards(self):
        other_org = organization("Other tenant", "other-tenant@example.com")
        foreign = create_organization_radar(other_org, RadarDefinition(name="theirs", active=True,
                                                                       signal_types=("new_company",)))
        mine = self.opportunity(self.radar_a, self.company, score=70)
        theirs = self.opportunity(foreign, self.company, score=95)  # same company, other organization
        self.assertEqual([c.primary_opportunity_id for c in self.feed().cards], [mine.pk])
        self.assertEqual([c.primary_opportunity_id for c in self.feed(org=other_org).cards], [theirs.pk])
        self.assertEqual(self.feed().cards[0].opportunity_count, 1)
        with CaptureQueriesContext(connection) as queries:
            self.feed()
        root = queries.captured_queries[0]["sql"]
        self.assertIn('"organization_id"', root)

    def test_22_an_empty_feed_is_clean(self):
        page = self.feed()
        self.assertEqual((page.cards, page.next_cursor, page.organization_id), ((), None, self.org.pk))

    def test_23_pages_never_duplicate_or_skip_a_card(self):
        companies = [make_company(f"5009{i:02d}") for i in range(9)]
        for index, company in enumerate(companies):
            score = (70, 90, 70, 50, 90, 70, 60, 90, 70)[index]
            at = T0 + timedelta(hours=(3, 1, 3, 2, 1, 0, 5, 4, 3)[index])
            self.opportunity(self.radar_a, company, score=score, at=at)
            if index % 3 == 0:  # some companies also have a weaker second Radar opportunity
                self.opportunity(self.radar_b, company, score=40, at=at)
        whole = self.companies(self.feed(limit=MAX_PAGE_SIZE))
        paged, cursor = [], None
        while True:
            page = self.feed(limit=2, cursor=cursor)
            paged += self.companies(page)
            cursor = page.next_cursor
            if cursor is None:
                break
        self.assertEqual(paged, whole)
        self.assertEqual(len(paged), len(set(paged)), paged)
        self.assertEqual(len(paged), 9)

    def test_query_count_is_bounded_and_independent_of_the_page(self):
        radars = [self.radar_a, self.radar_b, self.radar(name="Radar C", signal_types=("new_company",)),
                  self.radar(name="Radar D", signal_types=("new_company",))]
        for index in range(50):
            company = make_company(f"51{index:04d}")
            for offset in range(2):  # 100 opportunities over 50 companies
                radar = radars[(index + offset) % 4]
                self.opportunity(radar, company, score=40 + (index * 7 + offset * 13) % 60,
                                 signals=[self.signal(company, T0 + timedelta(minutes=index)),
                                          self.signal(company, T0 + timedelta(minutes=index, seconds=30))])
        self.assertEqual(Opportunity.objects.count(), 100)
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(len(self.feed(limit=10).cards), 10)
        with CaptureQueriesContext(connection) as large:
            self.assertEqual(len(self.feed(limit=50).cards), 50)
        self.assertEqual(len(small), 3)  # primaries page, children with their relations, distinct signal counts
        self.assertEqual(len(large), len(small))
        with CaptureQueriesContext(connection) as filtered:
            self.feed(FeedFilters(radar_ids=(radars[0].pk,)), limit=50)
        self.assertEqual(len(filtered), 4)  # plus the radar-ownership check
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT")
                            for q in small.captured_queries + large.captured_queries))


# --- no writes, privacy, parity --------------------------------------------------------------------

class SafetyTests(FeedTestCase):
    def test_24_reading_writes_nothing(self):
        self.opportunity(self.radar_a, self.company, score=80, status="new")
        self.opportunity(self.radar_b, self.company, score=70, status="saved")
        before = (list(Opportunity.objects.values()), list(OpportunitySignal.objects.values()),
                  list(OrganizationRadar.objects.values()), list(CompanySignal.objects.values()))
        with CaptureQueriesContext(connection) as queries:
            self.feed()
            self.feed(FeedFilters(statuses=("saved",)))
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries))
        self.assertEqual((list(Opportunity.objects.values()), list(OpportunitySignal.objects.values()),
                          list(OrganizationRadar.objects.values()), list(CompanySignal.objects.values())), before)

    def test_25_no_contact_person_or_payload_reaches_cards_or_the_command(self):
        self.opportunity(self.radar_a, self.company, score=80)
        page = self.feed()
        out = StringIO()
        call_command("show_opportunity_feed", organization_id=self.org.pk, stdout=out)
        text = repr(page) + out.getvalue()
        for secret in PRIVATE + (self.company.name, ADDRESS_SENTINEL, "ΑΤΤΙΚΗΣ"):
            self.assertNotIn(secret, text)
        self.assertIn(f"company=#{self.company.pk} gemi={self.company.gemi_number} score=80/100 class=high", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("show_opportunity_feed", organization_id=999_999, stdout=StringIO())

    def test_the_legacy_product_and_billing_are_untouched(self):
        user = entitled_user("legacy-feed@example.com")
        legacy = radar_for(user, "legacy", prefectures=["ΑΤΤΙΚΗΣ"])
        run_at = datetime(2026, 9, 16, 9, tzinfo=dt_timezone.utc)
        CompanyMonitoring.objects.create(
            company=self.company, state="active", priority="high", primary_reason="active_radar_match",
            policy_version=1, monitored_since=run_at - timedelta(days=3), next_check_at=run_at - timedelta(hours=1))
        policy = RefreshPolicy(max_requests_per_run=5, max_pages_per_query=2, max_direct_details_per_run=2, page_size=2)
        self.opportunity(self.radar_a, self.company, score=80)

        def world():
            mail.outbox = []
            DigestDelivery.objects.all().delete()
            sent = send_digests(date(2026, 9, 1))
            return (
                list(CustomerRadar.objects.values()), [r.pk for r in eligible_radars()],
                company_matches_radar(Company.objects.get(pk=self.company.pk), CustomerRadar.objects.get(pk=legacy.pk)),
                {pk: e.source_ids for pk, e in radar_match_evidence().items()},
                build_company_refresh_plan(run_at=run_at, policy=policy).summary(),
                list(UserSubscription.objects.values()), UserSubscription.objects.get(user=user).has_entitlement,
                list(CompanyMonitoring.objects.values()), list(RadarMatch.objects.values()),
                list(UserCompanyLead.objects.values()), (sent, [(m.subject, m.body) for m in mail.outbox]),
                list(Opportunity.objects.values()), list(CompanySnapshot.objects.values()),
            )

        with patch("django.core.signing.TimestampSigner.timestamp", return_value=TimestampSigner().timestamp()), \
                patch("gemiapp.ingestion.refresh.get_gemi_client") as client:
            before = world()
            self.assertEqual(len(self.feed().cards), 1)
            self.assertEqual(world(), before)
        client.assert_not_called()

    def test_the_phone_bridge_is_intact_and_absent_from_the_feed(self):
        self.assertEqual([p.display for p in extract_company_contact_phones(
            {"phone": "2109990001", "persons": [{"phone": "6999990002"}]})], ["2109990001"])
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])
        self.opportunity(self.radar_a, self.company, score=80)
        self.assertTrue(self.company.gemi_phones)
        self.assertNotIn("tel:", repr(self.feed()))
