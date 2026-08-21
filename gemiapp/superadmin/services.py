from datetime import timedelta
import logging
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.db.models import Count, Q, Sum
from django.utils import timezone
from ..models import (
    ActivityCode,
    AdminAuditLog,
    Company,
    CompanyActivity,
    CustomerRadar,
    DigestDelivery,
    ImportRun,
    RadarMatch,
    UserCompanyLead,
    UserSubscription,
    complimentary_q,
    entitlement_q,
    paid_subscription_q,
)

logger = logging.getLogger(__name__)

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
    return chart


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
    stripe_key = getattr(settings, "STRIPE_SECRET_KEY", "")
    price_pro = getattr(settings, "STRIPE_PRICE_PRO", "")
    price_biz = getattr(settings, "STRIPE_PRICE_BUSINESS", "")
    price_ent = getattr(settings, "STRIPE_PRICE_ENTERPRISE", "")
    webhook_sec = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")
    if stripe_key and price_pro and price_biz:
        details = "Stripe API & Price IDs configured."
        healthy = bool(webhook_sec) and bool(price_ent)
        if not webhook_sec:
            details += " (Webhook secret missing)"
        if not price_ent:
            details += " (Enterprise price id missing — το Enterprise checkout δεν λειτουργεί)"
        checks["stripe"] = {"name": "Stripe Payments", "status": "Operational" if healthy else "Warning", "details": details}
    else:
        checks["stripe"] = {"name": "Stripe Payments", "status": "Warning", "details": "Stripe keys or price IDs missing."}

    # 5. django-q2 Scheduler Check
    try:
        from django_q.models import Task, Schedule
        scheduled_count = Schedule.objects.count()
        failed_tasks = Task.objects.filter(success=False).count()
        checks["scheduler"] = {
            "name": "django-q2 Scheduler",
            "status": "Warning" if failed_tasks > 0 else "Operational",
            "details": f"{scheduled_count} scheduled tasks, {failed_tasks} failed task runs.",
        }
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
