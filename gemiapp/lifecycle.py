"""Lifecycle emails (E1–E15): reservation, eligibility, sending and the daily job.

Shape of every send
-------------------
1. A trigger *reserves* a LifecycleEmail row under a unique dedupe_key (``enqueue`` for events,
   ``run_daily`` for time-based triggers). A second webhook / cron run finds the row: nothing more.
2. ``deliver`` claims the row (queued/failed -> sending, conditional UPDATE), re-checks eligibility,
   builds the content from the *current* state (gemiapp.lifecycle_content) and sends.
3. Outcome: sent / skipped (with reason) / failed (retried by the next daily job, up to MAX_ATTEMPTS).

Rules (brief 2026-11-06)
------------------------
* ``LIFECYCLE_EMAILS_ENABLED`` off => nothing is reserved and nothing is sent.
* Account emails (ACCOUNT_KINDS) always go out to an active, deliverable address.
* Reminders (REMINDER_KINDS) additionally need ``reminders_enabled`` and no ``do_not_contact``, and at
  most one reminder per user per local day: a second one waits for the next day (not_before).
* Never to inactive accounts (except E1), never to a suppressed address (EmailDeliverySuppression).
* Time-based triggers only count events after ``LIFECYCLE_START_AT``: no retroactive mail.

Events never send inline: the webhook / view reserves the row and hands the id to a worker on commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .email_deliverability import is_email_delivery_suppressed
from .email_headers import user_reply_to
from .models import CustomerRadar, LifecycleEmail, SubscriptionPeriod, UserLifecycleProfile, has_targeted_radar

logger = logging.getLogger(__name__)

ACCOUNT_KINDS = frozenset({"e1", "e2", "e4", "e7", "e8", "e10", "e15"})
REMINDER_KINDS = frozenset({"e3", "e5", "e6", "e9", "e11", "e12", "e13", "e14"})
MAX_ATTEMPTS = 5
# A queued row a worker should have picked up long ago: the daily job sends it as the safety net.
QUEUE_GRACE = timedelta(minutes=10)
REMINDER_UNSUBSCRIBE_SALT = "lifecycle-reminders"
REMINDER_UNSUBSCRIBE_MAX_AGE = timedelta(days=120)


def lifecycle_enabled() -> bool:
    return bool(getattr(settings, "LIFECYCLE_EMAILS_ENABLED", False))


def lifecycle_start_at():
    """The activation moment, or None (=> no time-based trigger fires at all)."""
    raw = (getattr(settings, "LIFECYCLE_START_AT", "") or "").strip()
    if not raw:
        return None
    value = parse_datetime(raw)
    if value is None:
        logger.error("LIFECYCLE_START_AT is not an ISO 8601 datetime: %r", raw)
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value


def brevo_tag(kind: str, user_id) -> str:
    return f"lifecycle:{kind}:{user_id}"


def profile_for(user) -> UserLifecycleProfile:
    profile, _ = UserLifecycleProfile.objects.get_or_create(user=user)
    return profile


def reminder_unsubscribe_url(user) -> str:
    token = signing.dumps({"u": user.pk}, salt=REMINDER_UNSUBSCRIBE_SALT)
    return f"{settings.BASE_URL.rstrip('/')}{reverse('lifecycle_unsubscribe', kwargs={'token': token})}"


def user_from_unsubscribe_token(token):
    from django.contrib.auth.models import User

    try:
        data = signing.loads(token, salt=REMINDER_UNSUBSCRIBE_SALT,
                             max_age=REMINDER_UNSUBSCRIBE_MAX_AGE.total_seconds())
    except signing.BadSignature:
        return None
    return User.objects.filter(pk=data.get("u")).first()


KEEP_SALT = "lifecycle-keep"
KEEP_MAX_AGE = timedelta(days=60)
CLICK_EVENTS = ("click", "clicked", "unique_clicked")


def keep_url(user) -> str:
    token = signing.dumps({"u": user.pk}, salt=KEEP_SALT)
    return f"{settings.BASE_URL.rstrip('/')}{reverse('lifecycle_keep', kwargs={'token': token})}"


def user_from_keep_token(token):
    from django.contrib.auth.models import User

    try:
        data = signing.loads(token, salt=KEEP_SALT, max_age=KEEP_MAX_AGE.total_seconds())
    except signing.BadSignature:
        return None
    return User.objects.filter(pk=data.get("u")).first()


# --- reservation ---------------------------------------------------------------------------------

def reserve(kind: str, user, dedupe_key: str, params: dict | None = None, *, not_before=None):
    """Reserve ``dedupe_key``; returns the new row, or None if disabled or already reserved."""
    if not lifecycle_enabled():
        return None
    try:
        with transaction.atomic():
            row, created = LifecycleEmail.objects.get_or_create(
                dedupe_key=dedupe_key,
                defaults={"kind": kind, "user": user, "params": params or {}, "not_before": not_before,
                          "brevo_tag": brevo_tag(kind, user.pk if user else "")},
            )
    except IntegrityError:          # a concurrent reservation of the same key won
        return None
    return row if created else None


def enqueue(kind: str, user, dedupe_key: str, params: dict | None = None):
    """Event trigger: reserve now (inside the caller's transaction) and send from a worker on commit.

    Never raises into the caller: a webhook or the verification view must not fail because of an
    email. If the queue is unreachable the row stays queued and the daily job sends it.
    """
    try:
        row = reserve(kind, user, dedupe_key, params)
    except Exception:
        logger.exception("Could not reserve lifecycle email %s", dedupe_key)
        return None
    if row is None:
        return None

    def _hand_off():
        try:
            from django_q.tasks import async_task

            async_task("gemiapp.tasks.send_lifecycle_email_task", row.pk)
        except Exception:
            logger.exception("Could not queue lifecycle email %s; the daily job will send it.", row.pk)

    transaction.on_commit(_hand_off)
    return row


# --- delivery ------------------------------------------------------------------------------------

@dataclass
class DeliveryResult:
    status: str
    reason: str = ""


def _next_local_morning(now):
    local = timezone.localtime(now)
    tomorrow = local.date() + timedelta(days=1)
    return timezone.make_aware(datetime.combine(tomorrow, time(9, 30)), timezone.get_current_timezone())


def _reminder_already_sent_today(user, now, exclude_pk) -> bool:
    local_day = timezone.localtime(now).date()
    start = timezone.make_aware(datetime.combine(local_day, time.min), timezone.get_current_timezone())
    others = LifecycleEmail.objects.filter(user=user, kind__in=REMINDER_KINDS).exclude(pk=exclude_pk)
    return others.filter(Q(status="sent", sent_at__gte=start) | Q(status="sending")).exists()


def ineligibility_reason(row: LifecycleEmail):
    user = row.user
    if user is None:
        return "Ο λογαριασμός δεν υπάρχει πλέον"
    if not user.is_active and row.kind != "e1":
        return "Ανενεργός λογαριασμός"
    if not user.email:
        return "Ο λογαριασμός δεν έχει email"
    if is_email_delivery_suppressed(user.email):
        return "Η διεύθυνση είναι μη παραδόσιμη (hard bounce / blocked)"
    profile = profile_for(user)
    if row.kind in REMINDER_KINDS:
        if not profile.reminders_enabled:
            return "Απενεργοποίησε τις συμβουλές και υπενθυμίσεις"
        if profile.do_not_contact:
            return "do_not_contact"
    if row.kind == "e10" and profile.do_not_contact:
        return "do_not_contact"
    return None


def _send(row: LifecycleEmail, content):
    context = {"content": content, "user": row.user}
    message = EmailMultiAlternatives(
        content.subject,
        render_to_string("emails/lifecycle.txt", context),
        settings.DEFAULT_FROM_EMAIL,
        [row.user.email],
        headers=content.headers(row),
        reply_to=user_reply_to(),
    )
    message.attach_alternative(render_to_string("emails/lifecycle.html", context), "text/html")
    message.send()


def deliver(row_id: int, *, now=None) -> DeliveryResult:
    """Send one reserved row if it is still due and eligible. Safe to call any number of times."""
    from .lifecycle_content import build_content

    if not lifecycle_enabled():
        return DeliveryResult("disabled")
    now = now or timezone.now()
    claimed = LifecycleEmail.objects.filter(
        pk=row_id, status__in=["queued", "failed"], attempts__lt=MAX_ATTEMPTS,
    ).filter(Q(not_before__isnull=True) | Q(not_before__lte=now)).update(status="sending", attempts=F("attempts") + 1)
    if not claimed:
        return DeliveryResult("not_due")
    row = LifecycleEmail.objects.select_related("user").get(pk=row_id)

    def finish(status, reason="", **extra):
        LifecycleEmail.objects.filter(pk=row.pk).update(status=status, reason=reason[:300], **extra)
        return DeliveryResult(status, reason)

    reason = ineligibility_reason(row)
    if reason:
        return finish("skipped", reason)
    if row.kind in REMINDER_KINDS and _reminder_already_sent_today(row.user, now, row.pk):
        # Not a skip: the brief's rule is «the second one goes the next day».
        LifecycleEmail.objects.filter(pk=row.pk).update(
            status="queued", attempts=F("attempts") - 1, not_before=_next_local_morning(now),
            reason="Μεταφέρθηκε: ήδη στάλθηκε υπενθύμιση σήμερα",
        )
        return DeliveryResult("deferred")
    try:
        content = build_content(row, now=now)
    except Exception as exc:
        logger.exception("Could not build lifecycle email %s", row.pk)
        return finish("failed", f"build: {type(exc).__name__}")
    if content is None:
        return finish("skipped", "Δεν ισχύει πλέον (άλλαξε η κατάσταση)")
    try:
        _send(row, content)
    except Exception as exc:
        logger.warning("Lifecycle email %s failed: %s", row.pk, type(exc).__name__)
        return finish("failed", f"smtp: {type(exc).__name__}: {exc}")
    return finish("sent", sent_at=now)


# --- time-based triggers (daily job) ---------------------------------------------------------------

@dataclass
class Candidate:
    kind: str
    user: object
    dedupe_key: str
    params: dict = field(default_factory=dict)


def _e5_candidates(now, start_at):
    """E5: paid (or complimentary) access open for >= 24h, started after activation, 0 active Radars."""
    periods = (
        SubscriptionPeriod.objects.filter(ended_at__isnull=True, user__isnull=False,
                                          started_at__gte=start_at, started_at__lte=now - timedelta(hours=24))
        .select_related("user", "user__subscription")
    )
    for period in periods:
        user = period.user
        sub = getattr(user, "subscription", None)
        if sub is None or sub.radar_limit <= 0:
            continue
        if has_targeted_radar(user):
            continue
        yield Candidate("e5", user, f"e5:{period.pk}", {"period_id": period.pk})


def _e1_candidates(now, start_at):
    """E1: signed up after activation, still unverified 24h later. Once."""
    from django.contrib.auth.models import User

    for user in User.objects.filter(is_active=False, date_joined__gte=start_at,
                                    date_joined__lte=now - timedelta(hours=24)).exclude(email=""):
        yield Candidate("e1", user, f"e1:{user.pk}")


def _e3_candidates(now, start_at):
    """E3: Free, 48h after the welcome (E2 = first verification, only ever after activation), no filter used."""
    rows = LifecycleEmail.objects.filter(kind="e2", status="sent", sent_at__gte=start_at,
                                         sent_at__lte=now - timedelta(hours=48), user__isnull=False).select_related("user")
    for row in rows:
        user = row.user
        sub = getattr(user, "subscription", None)
        if sub is None or sub.radar_limit > 0 or profile_for(user).last_filter_used_at:
            continue
        yield Candidate("e3", user, f"e3:{user.pk}")


def _e6_candidates(now, start_at):
    """E6: a Radar active for >= 7 days (counted from activation at the earliest) with no match. Once per Radar."""
    radars = CustomerRadar.objects.filter(is_active=True, deleted_at__isnull=True, matches__isnull=True,
                                          monitor_from__lte=now - timedelta(days=7)).select_related("user")
    for radar in radars:
        if max(radar.monitor_from, start_at) > now - timedelta(days=7):
            continue
        sub = getattr(radar.user, "subscription", None)
        if sub is None or not sub.has_entitlement:
            continue
        yield Candidate("e6", radar.user, f"e6:{radar.pk}", {"radar_id": radar.pk})


def _e9_candidates(now, start_at):
    """E9: three days before a scheduled end that E8 announced (E8 rows only exist after activation)."""
    from .models import UserSubscription

    for sub in UserSubscription.objects.filter(active_until__gt=now, active_until__lte=now + timedelta(days=3, hours=12)) \
            .select_related("user"):
        key = f"{sub.stripe_subscription_id}:{sub.active_until.date().isoformat()}"
        if LifecycleEmail.objects.filter(dedupe_key=f"e8:{key}").exists():
            yield Candidate("e9", sub.user, f"e9:{key}")


def inactivity_start(user):
    """The last sign of life: last login or last click in one of our emails (opens are unreliable)."""
    from .models import EmailEngagementEvent

    last_click = EmailEngagementEvent.objects.filter(email__iexact=user.email, event_type__in=CLICK_EVENTS) \
        .order_by("-received_at").values_list("received_at", flat=True).first()
    marks = [m for m in (user.last_login, last_click, user.date_joined) if m]
    return max(marks) if marks else None


def _inactive_users(now, start_at, days):
    from django.contrib.auth.models import User

    for user in User.objects.filter(is_active=True).exclude(email=""):
        start = inactivity_start(user)
        if start is None:
            continue
        if now - max(start, start_at) >= timedelta(days=days):
            yield user, start


def _e11_candidates(now, start_at):
    """E11: 7 days without login or click (measured from activation at the earliest). Once per inactivity period."""
    for user, start in _inactive_users(now, start_at, 7):
        yield Candidate("e11", user, f"e11:{user.pk}:{start.date().isoformat()}")


def _e12_candidates(now, start_at):
    """E12: 30 days of the same inactivity period, after its E11 went out."""
    for user, start in _inactive_users(now, start_at, 30):
        period_key = f"{user.pk}:{start.date().isoformat()}"
        if LifecycleEmail.objects.filter(dedupe_key=f"e11:{period_key}", status="sent").exists():
            yield Candidate("e12", user, f"e12:{period_key}")


def _ended_paid_periods(now, start_at, days):
    from .subscription_history import is_former_paid

    for period in SubscriptionPeriod.objects.filter(source="stripe", user__isnull=False, ended_at__gte=start_at,
                                                    ended_at__lte=now - timedelta(days=days)) \
            .exclude(end_reason__in=["upgraded", "downgraded", "account_deleted"]).select_related("user"):
        if is_former_paid(period.user) and not profile_for(period.user).do_not_contact:
            yield period


def _e13_candidates(now, start_at):
    for period in _ended_paid_periods(now, start_at, 14):
        yield Candidate("e13", period.user, f"e13:{period.pk}", {"period_id": period.pk})


def _e14_candidates(now, start_at):
    from .lifecycle_content_reminders import whats_new_items

    if not whats_new_items():
        return
    for period in _ended_paid_periods(now, start_at, 60):
        yield Candidate("e14", period.user, f"e14:{period.pk}", {"period_id": period.pk})


def stop_unanswered_reminders(now, *, dry_run=False):
    """E12 follow-up: no «Ναι, κράτα με» and no login within 14 days of the question => reminders stop.
    The digest is untouched; only reminders_enabled changes (and is restored by the keep link)."""
    stopped = []
    for row in LifecycleEmail.objects.filter(kind="e12", status="sent", sent_at__lte=now - timedelta(days=14),
                                             user__isnull=False).select_related("user"):
        user, profile = row.user, profile_for(row.user)
        answered = (profile.kept_at and profile.kept_at >= row.sent_at) or (user.last_login and user.last_login >= row.sent_at)
        if answered or not profile.reminders_enabled or (profile.reminders_stopped_at and profile.reminders_stopped_at >= row.sent_at):
            continue
        stopped.append(user)
        if not dry_run:
            profile.reminders_enabled, profile.reminders_stopped_at = False, now
            profile.save(update_fields=["reminders_enabled", "reminders_stopped_at", "updated_at"])
    return stopped


TIME_BASED_TRIGGERS = (_e1_candidates, _e3_candidates, _e5_candidates, _e6_candidates, _e9_candidates,
                       _e11_candidates, _e12_candidates, _e13_candidates, _e14_candidates)


def time_based_candidates(now):
    start_at = lifecycle_start_at()
    if start_at is None:
        return []
    found = []
    for trigger in TIME_BASED_TRIGGERS:
        found.extend(trigger(now, start_at))
    already = set(LifecycleEmail.objects.filter(dedupe_key__in=[c.dedupe_key for c in found])
                  .values_list("dedupe_key", flat=True))
    return [c for c in found if c.dedupe_key not in already]


def due_rows(now):
    return LifecycleEmail.objects.filter(
        Q(status="queued", created_at__lte=now - QUEUE_GRACE) | Q(status="queued", not_before__isnull=False)
        | Q(status="failed", attempts__lt=MAX_ATTEMPTS),
    ).filter(Q(not_before__isnull=True) | Q(not_before__lte=now)).order_by("created_at", "pk")


@dataclass
class DailyReport:
    lines: list = field(default_factory=list)
    reserved: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    deferred: int = 0


def run_daily(*, now=None, dry_run=False) -> DailyReport:
    """The daily job: close lapsed complimentary periods, reserve time-based emails, send what is due."""
    from .subscription_history import sweep_expired_complimentary

    now = now or timezone.now()
    report = DailyReport()
    if not lifecycle_enabled() and not dry_run:
        report.lines.append("LIFECYCLE_EMAILS_ENABLED=0: τίποτα δεν στέλνεται.")
        return report
    if lifecycle_start_at() is None:
        report.lines.append("LIFECYCLE_START_AT δεν έχει οριστεί: κανένα χρονικό email.")

    lapsed = sweep_expired_complimentary(now=now, dry_run=dry_run)
    for period in lapsed:
        report.lines.append(f"{'[dry-run] ' if dry_run else ''}complimentary period {period.pk} κλείνει (λήξη)")

    if lifecycle_start_at() is not None:
        for user in stop_unanswered_reminders(now, dry_run=dry_run):
            report.lines.append(f"{'[dry-run] ' if dry_run else ''}υπενθυμίσεις σταματούν για user {user.pk} (E12 χωρίς απάντηση)")

    for candidate in time_based_candidates(now):
        report.lines.append(f"{'[dry-run] ' if dry_run else ''}{candidate.kind.upper()} → user {candidate.user.pk} "
                            f"<{candidate.user.email}> ({candidate.dedupe_key})")
        if not dry_run and reserve(candidate.kind, candidate.user, candidate.dedupe_key, candidate.params, not_before=now):
            report.reserved += 1

    for row in due_rows(now):
        if dry_run:
            report.lines.append(f"[dry-run] θα σταλεί {row.kind.upper()} #{row.pk} ({row.dedupe_key}, {row.status})")
            continue
        result = deliver(row.pk, now=now)
        if result.status in ("sent", "skipped", "failed", "deferred"):
            setattr(report, result.status, getattr(report, result.status) + 1)
        report.lines.append(f"{row.kind.upper()} #{row.pk} {row.dedupe_key}: {result.status} {result.reason}".rstrip())

    stuck = LifecycleEmail.objects.filter(status="sending", created_at__lte=now - timedelta(hours=1)).count()
    if stuck:
        report.lines.append(f"ΠΡΟΣΟΧΗ: {stuck} email σε κατάσταση «sending» >1 ώρα — δεν ξαναστέλνονται αυτόματα.")
    return report
