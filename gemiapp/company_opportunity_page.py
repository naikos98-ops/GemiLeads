"""Company opportunity page read model (D29, blueprint §36): the first customer-facing Gemi Leads 2.0 surface.

§36 defines a *company* page -- «Κάθε company page» -- with a header (Επωνυμία, διακριτικός τίτλος, status, ΓΕΜΗ,
νομική μορφή), «Why this lead», «Current information» («Ό,τι διαθέτει νόμιμα/αξιόπιστα η πηγή»), a timeline, the
company's opportunities and actions (Save, Assign, Call workflow, Follow-up, Dismiss). It names no contact field and
no URL. The actions belong to later Phase D items and are not rendered here at all.

This module only *assembles* immutable page values from rows that ``organization_access`` has already authorized;
it never decides access. It reads nothing tenant-owned by itself except the frozen C8 capture of an authorized
opportunity, and the platform data (company identity, canonical snapshots, LIVE signals, GEMI reference
descriptions) that describe the company.

Where every value comes from
----------------------------
* **Company identity** -- ``Company.name``, ``trade_names`` (the διακριτικός τίτλος) and ``gemi_number``: the core
  row's identity columns. Nothing else of the row is loaded -- no address, email, VAT, website, persons or payload.
* **Status, legal form, location, activities** («current information») -- the company's **latest canonical B3
  snapshot**: status, legal type, prefecture and municipality source ids, the incorporation date only when its
  quality is VALID, and the verified-current activities with their exact code and KAD version. Never
  ``Company.is_active``, the legacy description columns or ``CompanyActivity`` rows. Without a snapshot the page
  says the canonical information is unavailable; it does not fall back to legacy fields.
* **Descriptions** of those source ids come from the GEMI reference tables when they know the id; otherwise the id
  alone is shown. A description never changes identity.
* **Why this lead** -- the primary opportunity's **frozen C8 capture**, read back unchanged: score, class, the five
  v1 components with their reason codes and canonical evidence, and ``scored_as_of``. Nothing is recalculated, so
  the explanation is historical while «current information» is today's state -- deliberately different things.
* **Timeline** -- B6 in LIVE mode only, newest first, first ``TIMELINE_LIMIT`` entries.

Only LIVE reaches a customer
----------------------------
Opportunities whose current capture rests on a SHADOW signal are removed before this module sees them, and the
timeline reads LIVE signals only, so no SHADOW signal id, type or date can reach the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from django.apps import apps

from .company_signals import LIVE, SIGNAL_TYPE_CHOICES
from .company_timeline import get_company_timeline
from .opportunities import get_opportunity_score_breakdown

TIMELINE_LIMIT = 20
SIGNAL_LABELS = dict(SIGNAL_TYPE_CHOICES)
STATUS_LABELS = {
    "new": "Νέα", "viewed": "Προβλήθηκε", "saved": "Αποθηκευμένη", "assigned": "Ανατεθειμένη",
    "contacted": "Επικοινωνία", "interested": "Ενδιαφέρον", "follow_up": "Follow-up", "won": "Κερδήθηκε",
    "lost": "Χάθηκε", "not_relevant": "Μη σχετική", "do_not_contact": "Χωρίς επικοινωνία",
}
# §31 names the classes; the labels stay the blueprint's.
SCORE_CLASS_LABELS = {"priority": "Priority", "high": "High", "medium": "Medium", "low": "Low"}
REASON_LABELS = {
    "exact_kad_match": "Ακριβές ταίριασμα ΚΑΔ με το Radar",
    "radar_has_no_kad_target": "Το Radar δεν στοχεύει ΚΑΔ",
    "explicit_signal_type_match": "Το Radar παρακολουθεί ρητά αυτό το γεγονός",
    "radar_accepts_any_signal_type": "Το Radar δέχεται κάθε τύπο γεγονότος",
    "explicit_region_match": "Ταίριασμα περιοχής με το Radar",
    "radar_has_no_region_target": "Το Radar δεν στοχεύει περιοχή",
    "explicit_legal_form_match": "Ταίριασμα νομικής μορφής με το Radar",
    "radar_has_no_legal_form_target": "Το Radar δεν στοχεύει νομική μορφή",
    "fresh_within_24h": "Εντοπίστηκε έως 24 ώρες πριν από τον υπολογισμό",
    "fresh_within_72h": "Εντοπίστηκε έως 3 ημέρες πριν από τον υπολογισμό",
    "fresh_within_7d": "Εντοπίστηκε έως 7 ημέρες πριν από τον υπολογισμό",
    "fresh_within_14d": "Εντοπίστηκε έως 14 ημέρες πριν από τον υπολογισμό",
    "fresh_within_30d": "Εντοπίστηκε έως 30 ημέρες πριν από τον υπολογισμό",
    "stale_over_30d": "Εντοπίστηκε πάνω από 30 ημέρες πριν από τον υπολογισμό",
}
ACTIVITY_TYPE_LABELS = {"primary": "Κύρια", "secondary": "Δευτερεύουσα", "auxiliary": "Βοηθητική",
                        "other": "Λοιπή", "unknown": "—"}


@dataclass(frozen=True)
class ReferenceValue:
    """A GEMI source id with its reference description when the local reference data knows it."""

    source_id: str
    description: str | None = None

    def __str__(self):
        return f"{self.description} ({self.source_id})" if self.description else self.source_id


@dataclass(frozen=True)
class PageCompany:
    company_id: int
    gemi_number: str
    name: str
    trade_names: str


@dataclass(frozen=True)
class CurrentActivity:
    code: str
    kad_version: str | None
    activity_type: str
    description: str | None


@dataclass(frozen=True)
class CurrentState:
    available: bool
    snapshot_id: int | None = None
    observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    status: ReferenceValue | None = None
    legal_form: ReferenceValue | None = None
    prefecture: ReferenceValue | None = None
    municipality: ReferenceValue | None = None
    incorporation_date: date | None = None
    activities: tuple = ()
    indeterminate_activity_count: int = 0


@dataclass(frozen=True)
class PageOpportunity:
    opportunity_id: int
    radar_id: int
    radar_name: str
    score: int
    score_class: str
    score_class_label: str
    status: str
    status_label: str
    primary_reason_code: str
    primary_reason_label: str
    latest_signal_type: str
    latest_signal_label: str
    latest_signal_detected_at: datetime
    scored_as_of: datetime
    live_signal_count: int
    is_primary: bool
    # D30: "save" (this member may Save it now), "saved" (settled), or None (no control at all).
    save_action: str | None = None
    # D31: the active assignment (a display name only, never a contact detail) and whether this member may assign it.
    assigned_membership_id: int | None = None
    assigned_display_name: str | None = None
    assigned_at: datetime | None = None
    can_assign: bool = False
    # D32: the (status, label) targets this member may move the row to; empty means no status control at all.
    status_targets: tuple = ()
    # D33: this opportunity's notes (PageNote, newest first, within the page bound) and whether this member may add one.
    notes: tuple = ()
    can_add_note: bool = False


@dataclass(frozen=True)
class PageNote:
    """One user-authored note as displayed: plain text, the author's display name (or a former-member label)."""

    note_id: int
    body: str
    created_at: datetime
    author_display_name: str


@dataclass(frozen=True)
class BreakdownLine:
    code: str
    label: str
    awarded_points: int
    max_points: int
    awarded: bool
    reason_code: str
    reason_label: str
    evidence: tuple  # short display strings built from canonical ids only


@dataclass(frozen=True)
class TimelineItem:
    signal_id: int
    signal_type: str
    label: str
    detected_at: datetime
    effective_date: date | None
    effective_at: datetime | None
    effective_precision: str
    summary: str


@dataclass(frozen=True)
class CompanyOpportunityPage:
    organization_id: int
    organization_name: str
    company: PageCompany
    primary: PageOpportunity
    opportunities: tuple            # PageOpportunity, primary first, in the C9 order
    score: int
    score_class: str
    score_class_label: str
    scored_as_of: datetime
    breakdown: tuple                # BreakdownLine, the five v1 components in order
    current: CurrentState
    timeline: tuple                 # TimelineItem, LIVE only, newest first
    timeline_truncated: bool
    assignees: tuple = ()           # D31: (membership_id, display name) of assignable sales users, or empty
    notes_truncated: bool = False   # D33: more notes exist than the page shows


def _model(name):
    return apps.get_model("gemiapp", name)


def _descriptions(model_name, ids, **extra) -> dict:
    ids = {value for value in ids if value}
    if not ids:
        return {}
    rows = _model(model_name).objects.filter(source_id__in=ids, **extra).values_list("source_id", "description")
    return {source_id: description for source_id, description in rows if description}


def _age(seconds: int) -> str:
    if seconds < 2 * 3600:
        return f"{max(seconds // 60, 0)} λεπτά"
    if seconds < 2 * 86400:
        return f"{seconds // 3600} ώρες"
    return f"{seconds // 86400} ημέρες"


def build_company_opportunity_page(*, organization, rows, live_signal_counts: dict, save_actions: dict | None = None,
                                  assign_actions: dict | None = None, assignees: tuple = (),
                                  assignments: dict | None = None, status_actions: dict | None = None,
                                  notes: dict | None = None, notes_truncated: bool = False,
                                  note_actions: dict | None = None) -> CompanyOpportunityPage:
    """Assemble the page from already-authorized, LIVE-backed opportunities of one company, primary first."""
    primary_row = rows[0]
    company = primary_row.company
    frozen = get_opportunity_score_breakdown(primary_row)
    snapshot = (_model("CompanySnapshot").objects.filter(company_id=company.pk, schema_version=1)
                .order_by("-observed_at", "-id").first())
    timeline_page = get_company_timeline(company, mode=LIVE, limit=TIMELINE_LIMIT)

    # Every GEMI source id the page will describe, looked up once per reference table.
    wanted = {"status": set(), "legal": set(), "prefecture": set(), "municipality": set(), "kad": set()}
    if snapshot is not None:
        wanted["status"].add(snapshot.status_source_id)
        wanted["legal"].add(snapshot.legal_type_source_id)
        wanted["prefecture"].add(snapshot.prefecture_source_id)
        wanted["municipality"].add(snapshot.municipality_source_id)
        wanted["kad"].update(entry.get("code") for entry in snapshot.activities_state or ())
    for component in frozen.components:
        for item in component.evidence:
            kind = type(item).__name__
            if kind == "KadEvidence":
                wanted["kad"].add(item.code)
            elif kind == "RegionEvidence":
                wanted[item.level].add(item.source_id)
            elif kind == "LegalFormEvidence":
                wanted["legal"].add(item.source_id)
    for entry in timeline_page.entries:
        if entry.subject_kind == "kad":
            wanted["kad"].add(entry.activity_code)
        elif entry.subject_kind in ("status", "legal_form", "municipality"):
            bucket = {"status": "status", "legal_form": "legal", "municipality": "municipality"}[entry.subject_kind]
            wanted[bucket].update((entry.before_source_id, entry.after_source_id))
    names = {
        "status": _descriptions("GemiCompanyStatus", wanted["status"]),
        "legal": _descriptions("GemiLegalType", wanted["legal"]),
        "prefecture": _descriptions("GemiPrefecture", wanted["prefecture"]),
        "municipality": _descriptions("GemiMunicipality", wanted["municipality"]),
    }
    kad_names = {}
    kad_codes = {value for value in wanted["kad"] if value}
    if kad_codes:
        for code, version, description in (_model("GemiKad").objects.filter(source_id__in=kad_codes)
                                           .values_list("source_id", "kad_version", "description")):
            if description:
                kad_names[(code, version)] = description

    def ref(bucket, source_id):
        return ReferenceValue(source_id, names[bucket].get(source_id)) if source_id else None

    current = CurrentState(available=False)
    if snapshot is not None:
        current = CurrentState(
            available=True, snapshot_id=snapshot.pk, observed_at=snapshot.observed_at,
            last_observed_at=snapshot.last_observed_at, status=ref("status", snapshot.status_source_id),
            legal_form=ref("legal", snapshot.legal_type_source_id),
            prefecture=ref("prefecture", snapshot.prefecture_source_id),
            municipality=ref("municipality", snapshot.municipality_source_id),
            incorporation_date=snapshot.incorporation_date if snapshot.incorporation_date_quality == "valid" else None,
            activities=tuple(
                CurrentActivity(code=entry["code"], kad_version=entry.get("kad_version"),
                                activity_type=ACTIVITY_TYPE_LABELS.get(entry.get("activity_type"), "—"),
                                description=kad_names.get((entry["code"], entry.get("kad_version") or "")))
                for entry in snapshot.activities_state or ()
            ),
            indeterminate_activity_count=snapshot.unknown_current_activity_count,
        )

    def evidence_text(item) -> str:
        kind = type(item).__name__
        if kind == "KadEvidence":
            description = kad_names.get((item.code, item.kad_version or ""))
            return f"ΚΑΔ {item.code} · {item.kad_version or '—'}" + (f" — {description}" if description else "")
        if kind == "RegionEvidence":
            label = "Περιφερειακή ενότητα" if item.level == "prefecture" else "Δήμος"
            return f"{label} {ref(item.level, item.source_id)}"
        if kind == "LegalFormEvidence":
            return f"Νομική μορφή {ref('legal', item.source_id)}"
        if kind == "SignalTypeEvidence":
            return SIGNAL_LABELS.get(item.signal_type, item.signal_type)
        return f"Ηλικία γεγονότος κατά τον υπολογισμό: {_age(item.age_seconds)}"  # FreshnessEvidence

    breakdown = tuple(BreakdownLine(
        code=line.code, label=line.label, awarded_points=line.awarded_points, max_points=line.max_points,
        awarded=bool(line.awarded_points), reason_code=line.reason_code,
        reason_label=REASON_LABELS.get(line.reason_code, line.reason_code),
        evidence=tuple(evidence_text(item) for item in line.evidence),
    ) for line in frozen.components)

    opportunities = tuple(PageOpportunity(
        opportunity_id=row.pk, radar_id=row.radar_id, radar_name=row.radar.name, score=row.score,
        score_class=row.score_class, score_class_label=SCORE_CLASS_LABELS.get(row.score_class, row.score_class),
        status=row.status, status_label=STATUS_LABELS.get(row.status, row.status),
        primary_reason_code=row.primary_reason_code,
        primary_reason_label=REASON_LABELS.get(row.primary_reason_code, "—") if row.primary_reason_code else "—",
        latest_signal_type=row.latest_signal.signal_type,
        latest_signal_label=SIGNAL_LABELS.get(row.latest_signal.signal_type, row.latest_signal.signal_type),
        latest_signal_detected_at=row.latest_signal.detected_at, scored_as_of=row.scored_as_of,
        live_signal_count=live_signal_counts.get(row.pk, 0), is_primary=index == 0,
        save_action=(save_actions or {}).get(row.pk),
        assigned_membership_id=row.assigned_to_id if (assignments or {}).get(row.pk) else None,
        assigned_display_name=((assignments or {}).get(row.pk) or (None, None))[0],
        assigned_at=((assignments or {}).get(row.pk) or (None, None))[1],
        can_assign=bool((assign_actions or {}).get(row.pk)),
        status_targets=tuple((target, STATUS_LABELS[target]) for target in (status_actions or {}).get(row.pk, ())),
        notes=tuple(PageNote(note_id=note_id, body=body, created_at=created_at, author_display_name=author)
                    for note_id, body, created_at, author in (notes or {}).get(row.pk, ())),
        can_add_note=bool((note_actions or {}).get(row.pk)),
    ) for index, row in enumerate(rows))

    timeline = tuple(TimelineItem(
        signal_id=entry.signal_id, signal_type=entry.signal_type,
        label=SIGNAL_LABELS.get(entry.signal_type, entry.signal_type), detected_at=entry.detected_at,
        effective_date=entry.effective_date, effective_at=entry.effective_at,
        effective_precision=entry.effective_precision, summary=_timeline_summary(entry, ref, kad_names),
    ) for entry in timeline_page.entries)

    primary = opportunities[0]
    return CompanyOpportunityPage(
        organization_id=organization.pk, organization_name=organization.name,
        company=PageCompany(company_id=company.pk, gemi_number=company.gemi_number, name=company.name,
                            trade_names=company.trade_names),
        primary=primary, opportunities=opportunities, score=frozen.score, score_class=frozen.score_class,
        score_class_label=SCORE_CLASS_LABELS.get(frozen.score_class, frozen.score_class),
        scored_as_of=frozen.as_of, breakdown=breakdown, current=current, timeline=timeline,
        timeline_truncated=timeline_page.next_cursor is not None,
        assignees=tuple((assignee.membership_id, assignee.display_name) for assignee in assignees),
        notes_truncated=notes_truncated,
    )


def _timeline_summary(entry, ref, kad_names) -> str:
    """A short canonical description of what changed: ids with reference descriptions, never payload."""
    if entry.subject_kind == "kad":
        description = kad_names.get((entry.activity_code, entry.kad_version or ""))
        return f"ΚΑΔ {entry.activity_code} · {entry.kad_version or '—'}" + (f" — {description}" if description else "")
    if entry.subject_kind in ("status", "legal_form", "municipality"):
        bucket = {"status": "status", "legal_form": "legal", "municipality": "municipality"}[entry.subject_kind]
        before, after = ref(bucket, entry.before_source_id), ref(bucket, entry.after_source_id)
        return f"{before or '—'} → {after or '—'}"
    if entry.signal_type == "new_company":
        return "Πρώτη καταγραφή της εταιρείας από το Gemi Leads"
    return ""
