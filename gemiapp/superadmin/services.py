from datetime import timedelta
import logging
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.db.models import Count
from django.utils import timezone
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from ..models import (
    ActivityCode,
    AdminAuditLog,
    Company,
    CompanyActivity,
    CompanyOutreach,
    OutreachSuppression,
    CustomerRadar,
    DigestDelivery,
    EmailEngagementEvent,
    RadarMatch,
    StripeWebhookEvent,
    UserCompanyLead,
    UserSubscription,
    complimentary_q,
    entitlement_q,
    paid_subscription_q,
)
from ..services import digest_email_tag

logger = logging.getLogger(__name__)

# A scheduler failure older than this is history, not an active problem.
SCHEDULER_FAILURE_WINDOW_DAYS = 7

# A failed Stripe webhook older than this is history, not an active problem -- same reasoning
# as SCHEDULER_FAILURE_WINDOW_DAYS: a health check that can never return to green after one
# incident stops carrying information.
STRIPE_WEBHOOK_FAILURE_WINDOW_DAYS = 7

# Plan Prices for Subscription MRR Calculation
PLAN_PRICES = {
    "pro": 19,
    "business": 49,
    "enterprise": 99,
    # Custom deals are priced per contract, so they are counted but contribute 0 to automatic MRR.
    "custom": 0,
}


def log_admin_action(admin_user, action, target_type="", target_id="", target_repr="", metadata=None):
    """
    Persists a Superadmin administrative action to the Audit Log.
    """
    return AdminAuditLog.objects.create(
        admin_user=admin_user,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        target_repr=str(target_repr)[:255],
        metadata=metadata or {},
    )


def get_saas_overview_metrics():
    """
    Calculates executive SaaS indicators (Users, Subscriptions, MRR/ARR, Product, GEMI, Digests).
    Uses efficient single-query aggregations to prevent performance overhead.
    """
    now = timezone.now()
    today = timezone.localdate()

    # User Metrics
    users_qs = User.objects.all()
    total_users = users_qs.count()
    new_today = users_qs.filter(date_joined__date=today).count()
    new_7d = users_qs.filter(date_joined__gte=now - timedelta(days=7)).count()
    new_30d = users_qs.filter(date_joined__gte=now - timedelta(days=30)).count()
    verified_users = users_qs.filter(is_active=True).count()
    unverified_users = total_users - verified_users

    # Subscription Metrics & MRR Calculation — aggregated in the database, not in Python.
    subs = UserSubscription.objects.all()
    paid = paid_subscription_q(prefix="")
    unpaid_subs = subs.exclude(paid)

    active_pro_count = subs.filter(paid, tier="pro").count()
    active_business_count = subs.filter(paid, tier="business").count()
    active_enterprise_count = subs.filter(paid, tier="enterprise").count()
    active_custom_count = subs.filter(paid, tier="custom").count()
    complimentary_count = subs.filter(complimentary_q(prefix="")).count()

    canceled_count = unpaid_subs.filter(status="canceled").count()
    past_due_count = unpaid_subs.filter(status="past_due").count()
    unpaid_count = unpaid_subs.filter(status="unpaid").count()
    inactive_count = unpaid_subs.exclude(status__in=("canceled", "past_due", "unpaid")).count()

    active_paid_users = active_pro_count + active_business_count + active_enterprise_count + active_custom_count
    unpaid_users = total_users - active_paid_users

    # Calculated Subscription MRR & ARR
    mrr = (
        active_pro_count * PLAN_PRICES["pro"]
        + active_business_count * PLAN_PRICES["business"]
        + active_enterprise_count * PLAN_PRICES["enterprise"]
        + active_custom_count * PLAN_PRICES["custom"]
    )
    arr = mrr * 12

    # Subscriptions started & canceled this month
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    subs_created_this_month = subs.filter(created_at__gte=start_of_month).exclude(tier="free").count()

    # Product Metrics
    total_radars = CustomerRadar.objects.filter(deleted_at__isnull=True).count()
    active_radars = CustomerRadar.objects.filter(is_active=True, deleted_at__isnull=True).count()
    
    # Active eligible radars (radars whose owner has active entitlement)
    eligible_radars_count = CustomerRadar.objects.filter(
        entitlement_q(prefix="user__subscription__"),
        is_active=True,
        deleted_at__isnull=True,
    ).count()

    total_leads = UserCompanyLead.objects.count()
    leads_today = UserCompanyLead.objects.filter(first_seen_at__date=today).count()
    leads_7d = UserCompanyLead.objects.filter(first_seen_at__gte=now - timedelta(days=7)).count()
    total_matches = RadarMatch.objects.count()
    matches_today = RadarMatch.objects.filter(matched_on=today).count()

    # GEMI Metrics
    total_companies = Company.objects.count()
    companies_today = Company.objects.filter(incorporation_date=today).count()
    companies_7d = Company.objects.filter(incorporation_date__gte=today - timedelta(days=6)).count()
    latest_company = Company.objects.order_by("-incorporation_date").first()
    latest_registration_date = latest_company.incorporation_date if latest_company else None
    total_company_activities = CompanyActivity.objects.count()
    total_kads = ActivityCode.objects.count()

    # Digest Deliveries Metrics
    deliveries_today = DigestDelivery.objects.filter(digest_date=today)
    sent_today = deliveries_today.filter(status="sent").count()
    failed_today = deliveries_today.filter(status="failed").count()
    skipped_today = deliveries_today.filter(status="skipped").count()
    sent_7d = DigestDelivery.objects.filter(digest_date__gte=today - timedelta(days=6), status="sent").count()
    last_delivery = DigestDelivery.objects.order_by("-sent_at").first()

    return {
        "total_users": total_users,
        "new_today": new_today,
        "new_7d": new_7d,
        "new_30d": new_30d,
        "verified_users": verified_users,
        "unverified_users": unverified_users,
        "active_paid_users": active_paid_users,
        "unpaid_users": unpaid_users,
        "active_pro_count": active_pro_count,
        "active_business_count": active_business_count,
        "active_enterprise_count": active_enterprise_count,
        "active_custom_count": active_custom_count,
        "canceled_count": canceled_count,
        "past_due_count": past_due_count,
        "unpaid_count": unpaid_count,
        "inactive_count": inactive_count,
        "complimentary_count": complimentary_count,
        "mrr": mrr,
        "arr": arr,
        "subs_created_this_month": subs_created_this_month,
        "total_radars": total_radars,
        "active_radars": active_radars,
        "eligible_radars_count": eligible_radars_count,
        "total_leads": total_leads,
        "leads_today": leads_today,
        "leads_7d": leads_7d,
        "total_matches": total_matches,
        "matches_today": matches_today,
        "total_companies": total_companies,
        "companies_today": companies_today,
        "companies_7d": companies_7d,
        "latest_registration_date": latest_registration_date,
        "total_company_activities": total_company_activities,
        "total_kads": total_kads,
        "sent_today": sent_today,
        "failed_today": failed_today,
        "skipped_today": skipped_today,
        "sent_7d": sent_7d,
        "last_delivery_at": last_delivery.sent_at if last_delivery else None,
    }


def get_chart_data_last_30_days():
    """
    Returns daily stats for the last 30 days (User Registrations, Leads Generated, GEMI Imports).
    """
    today = timezone.localdate()
    start_date = today - timedelta(days=29)

    user_data = dict(
        User.objects.filter(date_joined__date__gte=start_date)
        .values_list("date_joined__date")
        .annotate(total=Count("id"))
    )

    lead_data = dict(
        UserCompanyLead.objects.filter(first_seen_at__date__gte=start_date)
        .values_list("first_seen_at__date")
        .annotate(total=Count("id"))
    )

    company_data = dict(
        Company.objects.filter(incorporation_date__gte=start_date)
        .values_list("incorporation_date")
        .annotate(total=Count("id"))
    )

    chart = []
    curr = start_date
    while curr <= today:
        chart.append({
            "date": curr.strftime("%d/%m"),
            "users": user_data.get(curr, 0),
            "leads": lead_data.get(curr, 0),
            "companies": company_data.get(curr, 0),
        })
        curr += timedelta(days=1)

    # Bar heights are scaled against the single largest value across both series (not each
    # series independently) so a tall "leads" day and a tall "users" day stay visually
    # comparable -- and, critically, so every bar fits inside the fixed-height chart container
    # in the template no matter how big a single day's count gets (a spike day used to render
    # taller than the container and get clipped at its edge).
    max_value = max([d["users"] for d in chart] + [d["leads"] for d in chart] + [1])
    for d in chart:
        d["users_height"] = round((d["users"] / max_value) * 118) + 2 if d["users"] else 2
        d["leads_height"] = round((d["leads"] / max_value) * 118) + 2 if d["leads"] else 2

    return chart


# Brevo's own event vocabulary (transactional webhooks) collapsed into the handful of buckets
# the digest list actually wants to show. Anything not listed here (deferred, blocked, invalid,
# error, ...) falls into "other" rather than being silently dropped, so a new/unexpected Brevo
# event type is still visible somewhere instead of vanishing from the count.
_ENGAGEMENT_BUCKETS = {
    "delivered": "delivered",
    # Brevo sends both "opened" (fires on every open, including re-opens by the same person)
    # and "uniqueOpened"/"unique_opened" (fires once per recipient) for the SAME underlying
    # open action -- bucketing both into "opened" would double-count a single real open.
    # "opened" is the one kept: a raw activity count is the more useful vanity metric here,
    # and it's what most Brevo accounts have enabled. The unique variant is intentionally left
    # unmapped (falls into "other") rather than merged in, so this can never over-count --
    # under-counting is the safer failure mode for a display-only metric.
    "opened": "opened",
    "click": "clicked",
    "unsubscribed": "unsubscribed",
    # Brevo's SMTP relay emits snake_case event names ("hard_bounce"); the transactional
    # API emits camelCase ("hardBounce"). Map both so the source doesn't change the count.
    "hardBounce": "bounced",
    "hard_bounce": "bounced",
    "softBounce": "bounced",
    "soft_bounce": "bounced",
    "blocked": "bounced",
    "spam": "spam",
    # "request" fires once per accepted message -- the closest thing to "we handed it to
    # Brevo". Kept separate from "delivered" (which fires only after the receiving MX
    # accepts it) so the gap between the two stays visible.
    "request": "sent",
}


def _attach_engagement_stats(objects, tag_of):
    """Shared implementation: annotates each object with `.engagement`, a dict of bucketed
    Brevo event counts (delivered/opened/clicked/unsubscribed/bounced/spam/other), matched via
    the tag it was sent with (`tag_of(obj)` -> that tag string). One query for the whole page
    rather than one per row.
    """
    objects = list(objects)
    tag_by_pk = {o.pk: tag_of(o) for o in objects}
    tags = list(tag_by_pk.values())

    counts_by_tag = {}
    if tags:
        rows = (
            EmailEngagementEvent.objects.filter(tag__in=tags)
            .values("tag", "event_type")
            .annotate(n=Count("id"))
        )
        for row in rows:
            bucket = _ENGAGEMENT_BUCKETS.get(row["event_type"], "other")
            per_tag = counts_by_tag.setdefault(row["tag"], {})
            per_tag[bucket] = per_tag.get(bucket, 0) + row["n"]

    for o in objects:
        o.engagement = counts_by_tag.get(tag_by_pk[o.pk], {})
    return objects


def attach_email_engagement_stats(deliveries):
    """Annotates each DigestDelivery with `.engagement` -- see _attach_engagement_stats. Tag
    format matches gemiapp.services.digest_email_tag, used at send time.
    """
    return _attach_engagement_stats(
        deliveries, lambda d: digest_email_tag(d.user_id, d.digest_date, d.frequency)
    )


def attach_outreach_engagement_stats(outreach_rows):
    """Annotates each CompanyOutreach with `.engagement` -- see _attach_engagement_stats. Tag
    format matches build_outreach_email's own "outreach:<company_id>", used at send time.
    """
    return _attach_engagement_stats(outreach_rows, lambda o: f"outreach:{o.company_id}")


def grant_complimentary_access(admin_user, target_user, tier, until_datetime=None):
    """
    Grants complimentary Pro or Business access to a user without modifying Stripe state.
    """
    sub, _ = UserSubscription.objects.get_or_create(user=target_user)
    old_tier = sub.complimentary_tier
    old_until = sub.complimentary_until

    sub.complimentary_tier = tier
    sub.complimentary_until = until_datetime
    sub.save()

    log_admin_action(
        admin_user=admin_user,
        action="grant_complimentary_access",
        target_type="User",
        target_id=target_user.id,
        target_repr=target_user.email,
        metadata={
            "tier": tier,
            "until": until_datetime.isoformat() if until_datetime else "permanent",
            "previous_tier": old_tier,
            "previous_until": old_until.isoformat() if old_until else None,
        },
    )
    return sub


def revoke_complimentary_access(admin_user, target_user):
    """
    Revokes complimentary access from a user.
    """
    sub, _ = UserSubscription.objects.get_or_create(user=target_user)
    old_tier = sub.complimentary_tier
    sub.complimentary_tier = "none"
    sub.complimentary_until = None
    sub.save()

    log_admin_action(
        admin_user=admin_user,
        action="revoke_complimentary_access",
        target_type="User",
        target_id=target_user.id,
        target_repr=target_user.email,
        metadata={"previous_tier": old_tier},
    )
    return sub


def toggle_user_active_state(admin_user, target_user, active_state):
    """
    Deactivates or Reactivates a Django User account.
    """
    if target_user.is_superuser:
        raise ValueError("Δεν επιτρέπεται η απενεργοποίηση Superadmin χρήστη.")
    
    target_user.is_active = active_state
    target_user.save(update_fields=["is_active"])

    action = "reactivate_user" if active_state else "deactivate_user"
    log_admin_action(
        admin_user=admin_user,
        action=action,
        target_type="User",
        target_id=target_user.id,
        target_repr=target_user.email,
        metadata={"is_active": active_state},
    )
    return target_user


# The web request only claims rows and enqueues; a django-q worker does the sending.
# The cap bounds how much one enqueue can hand a single worker.
OUTREACH_BATCH_LIMIT = 500

# Brevo's plan allows a fixed number of emails per rolling 24h. Past that limit the SMTP
# relay still returns 250 OK and then silently drops the message, so send() succeeds and we
# would wrongly mark the row "sent". Stop well below the real ceiling and leave the rest
# "pending" for the next worker run. Every transactional email the app sends (digests
# included) counts against the same quota, so this is deliberately conservative.
OUTREACH_DAILY_SEND_CAP_DEFAULT = 250


def _outreach_daily_send_cap():
    return int(getattr(settings, "OUTREACH_DAILY_SEND_CAP", OUTREACH_DAILY_SEND_CAP_DEFAULT))


def _outreach_sent_last_24h():
    cutoff = timezone.now() - timedelta(hours=24)
    return CompanyOutreach.objects.filter(status="sent", created_at__gte=cutoff).count()


def uncontacted_companies_qs():
    """Companies eligible for a first outreach email.

    Excludes any company that already has a CompanyOutreach row (any status, so a
    queued send is not re-queued) and any address on the suppression list.
    """
    suppressed = OutreachSuppression.objects.values_list("email", flat=True)
    return (
        Company.objects.filter(outreach__isnull=True)
        .exclude(email="")
        .exclude(email__in=suppressed)
        .order_by("-incorporation_date", "-id")
    )


OUTREACH_SUBJECT = "Μπήκατε στην αγορά — τώρα χρειάζεστε πελάτες"


def _outreach_reply_to():
    addr = getattr(settings, "EMAIL_REPLY_TO", "")
    return [addr] if addr else None


def build_outreach_email(company, to_email=None):
    """Render the outreach email for ``company``. Returns an unsent EmailMultiAlternatives.

    ``to_email`` overrides the recipient (used for test sends); otherwise the company's
    own published address is used.
    """
    from django.core.signing import TimestampSigner
    from django.urls import reverse

    from ..views import OUTREACH_UNSUBSCRIBE_SALT

    recipient = to_email or company.email
    signer = TimestampSigner(salt=OUTREACH_UNSUBSCRIBE_SALT)
    people = company.people if company.pk else []
    context = {
        "company": company,
        "contact_name": people[0]["name"] if people else "",
        "signup_url": f"{settings.BASE_URL}{reverse('signup')}",
        "site_url": settings.BASE_URL,
        "unsubscribe_url": f"{settings.BASE_URL}{reverse('outreach_unsubscribe', kwargs={'token': signer.sign(recipient)})}",
    }
    # No tag on a test send (company.pk is None for the in-memory representative company used
    # by send_outreach_test_email) -- there is no CompanyOutreach row to ever match it against.
    headers = {"X-Mailin-Tag": f"outreach:{company.pk}"} if company.pk else None
    msg = EmailMultiAlternatives(
        OUTREACH_SUBJECT,
        render_to_string("emails/client_outreach.txt", context),
        settings.DEFAULT_FROM_EMAIL,
        [recipient],
        reply_to=_outreach_reply_to(),
        headers=headers,
    )
    msg.attach_alternative(render_to_string("emails/client_outreach.html", context), "text/html")
    return msg


def send_outreach_test_email(admin_user, to_email):
    """Send the outreach email to an arbitrary address for visual review.

    Uses a representative in-memory Company. Records nothing in CompanyOutreach and
    does not consult the suppression list — this is a manual test, not real outreach.
    """
    from datetime import date

    sample = Company(
        gemi_number="000000000000",
        name="ΠΑΡΑΔΕΙΓΜΑ ΕΜΠΟΡΙΚΗ ΜΟΝΟΠΡΟΣΩΠΗ ΙΚΕ",
        incorporation_date=date.today(),
        legal_type="ΙΔΙΩΤΙΚΗ ΚΕΦΑΛΑΙΟΥΧΙΚΗ ΕΤΑΙΡΕΙΑ",
        city="Θεσσαλονίκη",
        prefecture="ΘΕΣΣΑΛΟΝΙΚΗΣ",
        email=to_email,
    )
    build_outreach_email(sample, to_email=to_email).send()
    log_admin_action(
        admin_user=admin_user,
        action="company_outreach_test",
        target_type="Email",
        target_id=to_email,
        target_repr=to_email,
    )


def queue_company_outreach(admin_user, company_ids):
    """Claim eligible companies as pending and hand the send to a background worker.

    Fast and synchronous: only writes CompanyOutreach(status="pending") rows and calls
    async_task. The rows make the companies vanish from the tool immediately and stop a
    second enqueue from picking them up. Returns the number of companies queued.
    """
    from django_q.tasks import async_task

    eligible = uncontacted_companies_qs().filter(id__in=company_ids)[:OUTREACH_BATCH_LIMIT]
    rows = [
        CompanyOutreach(company=c, status="pending", sent_to=c.email, sent_by=admin_user)
        for c in eligible
    ]
    if not rows:
        return 0

    # ignore_conflicts: a company claimed by a racing enqueue keeps its existing row.
    CompanyOutreach.objects.bulk_create(rows, ignore_conflicts=True)
    queued_ids = list(
        CompanyOutreach.objects.filter(
            company_id__in=[r.company_id for r in rows], status="pending"
        ).values_list("company_id", flat=True)
    )

    log_admin_action(
        admin_user=admin_user,
        action="company_outreach_queue",
        target_type="Company",
        target_id="batch",
        target_repr=f"{len(queued_ids)} queued",
        metadata={"queued": len(queued_ids)},
    )
    async_task("gemiapp.tasks.send_company_outreach_task", queued_ids)
    return len(queued_ids)


def process_pending_outreach(company_ids):
    """Send the email for each pending CompanyOutreach row. Runs in a django-q worker.

    Returns (sent, failed, skipped). A suppressed address or a missing email marks the row
    failed rather than deleting it, so the company is not re-queued and the reason is
    visible. Rows left unsent because the daily Brevo quota (OUTREACH_DAILY_SEND_CAP) is
    exhausted stay "pending" and are counted as skipped -- a later run sends them.
    """
    sent = failed = skipped = 0
    rows = CompanyOutreach.objects.filter(
        company_id__in=company_ids, status="pending"
    ).select_related("company")

    # Rows sent earlier today (this run or a previous one) already spent quota.
    budget = _outreach_daily_send_cap() - _outreach_sent_last_24h()

    for row in rows:
        company = row.company
        if not company.email or OutreachSuppression.is_suppressed(company.email):
            row.status = "failed"
            row.error_message = "Χωρίς email ή στη λίστα απεγγραφών."
            row.save(update_fields=["status", "error_message"])
            failed += 1
            continue
        if budget <= 0:
            # Leave "pending" -- the next worker run picks it up once quota resets.
            skipped += 1
            continue
        try:
            build_outreach_email(company).send()
            row.status = "sent"
            row.error_message = ""
            row.save(update_fields=["status", "error_message"])
            sent += 1
            budget -= 1
        except Exception as exc:
            logger.exception("Failed sending outreach to company %s", company.pk)
            row.status = "failed"
            row.error_message = str(exc)[:2000]
            row.save(update_fields=["status", "error_message"])
            failed += 1

    return sent, failed, skipped


def get_system_health():
    """
    Performs non-destructive health checks for Database, GEMI Config, Brevo SMTP, Stripe, django-q2, Sentry.
    No secrets are exposed.
    """
    checks = {}

    # 1. Database Check
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = {"name": "Database (SQLite/PostgreSQL)", "status": "Operational", "details": "Database connection healthy."}
    except Exception as e:
        checks["database"] = {"name": "Database", "status": "Error", "details": str(e)}

    # 2. GEMI API Config Check
    gemi_key = getattr(settings, "GEMI_API_KEY", "")
    if gemi_key:
        checks["gemi_api"] = {"name": "GEMI Open Data API", "status": "Operational", "details": "GEMI_API_KEY configured locally."}
    else:
        checks["gemi_api"] = {"name": "GEMI Open Data API", "status": "Warning", "details": "GEMI_API_KEY missing in environment."}

    # 3. Brevo Email / SMTP Config Check
    email_host = getattr(settings, "EMAIL_HOST", "")
    email_user = getattr(settings, "EMAIL_HOST_USER", "")
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "")
    if email_host and email_user and from_email:
        checks["brevo_email"] = {"name": "Brevo Email / Sender", "status": "Operational", "details": f"Configured sender: {from_email}"}
    else:
        checks["brevo_email"] = {"name": "Brevo Email / Sender", "status": "Warning", "details": "SMTP credentials incomplete."}

    # 4. Stripe Config Check
    #
    # Being configured is necessary but not sufficient: real webhook deliveries can still fail
    # (a code bug, a malformed payload, a Stripe API change) with nothing in the config check
    # above to catch it. A recent StripeWebhookEvent(status="failed") row is the only signal
    # that something is actually going wrong right now, so it must independently gate the
    # overall status -- a fully-configured-but-silently-failing webhook must never show green.
    stripe_key = getattr(settings, "STRIPE_SECRET_KEY", "")
    price_pro = getattr(settings, "STRIPE_PRICE_PRO", "")
    price_biz = getattr(settings, "STRIPE_PRICE_BUSINESS", "")
    price_ent = getattr(settings, "STRIPE_PRICE_ENTERPRISE", "")
    webhook_sec = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")

    webhook_cutoff = timezone.now() - timedelta(days=STRIPE_WEBHOOK_FAILURE_WINDOW_DAYS)
    recent_webhook_failures = StripeWebhookEvent.objects.filter(
        status="failed", received_at__gte=webhook_cutoff,
    ).count()
    older_webhook_failures = StripeWebhookEvent.objects.filter(
        status="failed", received_at__lt=webhook_cutoff,
    ).count()

    if stripe_key and price_pro and price_biz:
        details = "Stripe API & Price IDs configured."
        healthy = bool(webhook_sec) and bool(price_ent)
        if not webhook_sec:
            details += " (Webhook secret missing)"
        if not price_ent:
            details += " (Enterprise price id missing — το Enterprise checkout δεν λειτουργεί)"
        if recent_webhook_failures:
            healthy = False
            details += (
                f" ({recent_webhook_failures} failed webhook event(s) in the last "
                f"{STRIPE_WEBHOOK_FAILURE_WINDOW_DAYS} days — check StripeWebhookEvent)"
            )
        elif older_webhook_failures:
            details += f" ({older_webhook_failures} older failed webhook event(s) retained for history.)"
        checks["stripe"] = {"name": "Stripe Payments", "status": "Operational" if healthy else "Warning", "details": details}
    else:
        details = "Stripe keys or price IDs missing."
        if recent_webhook_failures:
            details += (
                f" Also: {recent_webhook_failures} failed webhook event(s) in the last "
                f"{STRIPE_WEBHOOK_FAILURE_WINDOW_DAYS} days."
            )
        checks["stripe"] = {"name": "Stripe Payments", "status": "Warning", "details": details}

    # 5. django-q2 Scheduler Check
    #
    # Only recent failures are actionable. django-q2 prunes successful tasks via save_limit but
    # never deletes failed ones, so counting every failure ever recorded pins this check to
    # Warning permanently, even after the underlying bug is fixed. A health check that can never
    # return to green stops carrying information.
    try:
        from django_q.models import Schedule, Task

        scheduled_count = Schedule.objects.count()
        cutoff = timezone.now() - timedelta(days=SCHEDULER_FAILURE_WINDOW_DAYS)
        recent_failures = Task.objects.filter(success=False, started__gte=cutoff).count()
        older_failures = Task.objects.filter(success=False, started__lt=cutoff).count()

        if scheduled_count == 0:
            status = "Warning"
            details = "No scheduled tasks registered. The daily and intraday pipelines will not run."
        elif recent_failures:
            status = "Warning"
            details = (
                f"{scheduled_count} scheduled tasks, {recent_failures} failed run(s) in the last "
                f"{SCHEDULER_FAILURE_WINDOW_DAYS} days."
            )
        else:
            status = "Operational"
            details = f"{scheduled_count} scheduled tasks, no failures in the last {SCHEDULER_FAILURE_WINDOW_DAYS} days."
            if older_failures:
                details += f" ({older_failures} older failure(s) retained for history.)"

        checks["scheduler"] = {"name": "django-q2 Scheduler", "status": status, "details": details}
    except Exception:
        checks["scheduler"] = {"name": "django-q2 Scheduler", "status": "Warning", "details": "django-q2 tables not installed or idle."}

    # 6. Sentry Config Check
    sentry_dsn = getattr(settings, "SENTRY_DSN", None)
    if sentry_dsn:
        checks["sentry"] = {"name": "Sentry Exception Tracking", "status": "Operational", "details": "Sentry DSN configured."}
    else:
        checks["sentry"] = {"name": "Sentry Exception Tracking", "status": "Warning", "details": "Sentry DSN not set (Development mode)."}

    # Environment Info
    env_info = {
        "debug_mode": settings.DEBUG,
        "django_version": getattr(settings, "DJANGO_VERSION", "5.2"),
        "db_engine": settings.DATABASES["default"]["ENGINE"].split(".")[-1],
        "timezone": str(settings.TIME_ZONE),
    }

    return {"services": checks, "environment": env_info}
