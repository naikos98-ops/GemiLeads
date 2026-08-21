from datetime import date, datetime, timedelta
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .decorators import superadmin_required
from .services import (
    get_chart_data_last_30_days,
    get_saas_overview_metrics,
    get_system_health,
    grant_complimentary_access,
    log_admin_action,
    revoke_complimentary_access,
    toggle_user_active_state,
)
from ..models import (
    AdminAuditLog,
    CustomerRadar,
    DigestDelivery,
    ImportRun,
    RadarMatch,
    UserCompanyLead,
    UserSubscription,
    complimentary_q,
    effective_tier_q,
    entitlement_q,
    get_user_radar_limit,
    paid_subscription_q,
)
from ..services import import_for_date, send_digests


@superadmin_required
def overview(request):
    metrics = get_saas_overview_metrics()
    chart_data = get_chart_data_last_30_days()
    recent_users = User.objects.order_by("-date_joined")[:5]
    recent_audits = AdminAuditLog.objects.order_by("-timestamp")[:5]

    context = {
        "metrics": metrics,
        "chart_data": chart_data,
        "recent_users": recent_users,
        "recent_audits": recent_audits,
    }
    return render(request, "superadmin/overview.html", context)


@superadmin_required
def user_list(request):
    # distinct=True: two Counts over different multi-valued relations otherwise inflate each other.
    qs = User.objects.select_related("subscription").annotate(
        radar_count=Count("customer_radars", filter=Q(customer_radars__deleted_at__isnull=True), distinct=True),
        lead_count=Count("company_leads", distinct=True),
    ).order_by("-date_joined")

    # Search
    search = request.GET.get("q", "").strip()
    if search:
        qs = qs.filter(Q(email__icontains=search) | Q(username__icontains=search) | Q(first_name__icontains=search) | Q(last_name__icontains=search))

    # Filters — expressed as database predicates so pagination never loads every user.
    paid_status = request.GET.get("paid", "").strip()
    if paid_status == "paid":
        qs = qs.filter(paid_subscription_q())
    elif paid_status == "unpaid":
        qs = qs.exclude(paid_subscription_q())
    elif paid_status == "complimentary":
        qs = qs.filter(complimentary_q())

    tier = request.GET.get("tier", "").strip()
    if tier in ("free", "pro", "business", "enterprise", "custom"):
        qs = qs.filter(effective_tier_q(tier))

    active_filter = request.GET.get("active", "").strip()
    if active_filter == "1":
        qs = qs.filter(is_active=True)
    elif active_filter == "0":
        qs = qs.filter(is_active=False)


    # Server-side Paginator
    paginator = Paginator(qs, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(request, "superadmin/users/list.html", {
        "page_obj": page_obj,
        "search": search,
        "paid_filter": paid_status,
        "tier_filter": tier,
        "active_filter": active_filter,
    })


@superadmin_required
def user_detail(request, user_id):
    target_user = get_object_or_404(User.objects.select_related("subscription", "digest_preference"), pk=user_id)
    radars = CustomerRadar.objects.filter(user=target_user, deleted_at__isnull=True).annotate(match_count=Count("matches"))
    leads = UserCompanyLead.objects.filter(user=target_user).select_related("company")[:10]
    audit_logs = AdminAuditLog.objects.filter(target_type="User", target_id=str(target_user.id))[:10]

    context = {
        "target_user": target_user,
        "radars": radars,
        "recent_leads": leads,
        "audit_logs": audit_logs,
        "radar_limit": get_user_radar_limit(target_user),
    }
    return render(request, "superadmin/users/detail.html", context)


@superadmin_required
@require_POST
def user_toggle_active(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    active_state = request.POST.get("active") == "1"
    try:
        toggle_user_active_state(request.user, target_user, active_state)
        messages.success(request, f"Ο λογαριασμός {target_user.email} {'ενεργοποιήθηκε' if active_state else 'απενεργοποιήθηκε'}.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("superadmin:user_detail", user_id=target_user.id)


@superadmin_required
@require_POST
def user_complimentary(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    action = request.POST.get("action")

    if action == "grant":
        tier = request.POST.get("tier", "pro")
        duration = request.POST.get("duration", "permanent")
        until_str = request.POST.get("until", "").strip()
        until_dt = None

        if duration == "1m":
            until_dt = timezone.now() + timedelta(days=30)
        elif duration == "3m":
            until_dt = timezone.now() + timedelta(days=90)
        elif duration == "6m":
            until_dt = timezone.now() + timedelta(days=180)
        elif duration == "1y":
            until_dt = timezone.now() + timedelta(days=365)
        elif duration == "custom" and until_str:
            try:
                until_dt = timezone.make_aware(datetime.strptime(until_str, "%Y-%m-%d"))
            except ValueError:
                messages.error(request, "Μη έγκυρη ημερομηνία λήξης.")
                return redirect("superadmin:user_detail", user_id=target_user.id)

        grant_complimentary_access(request.user, target_user, tier, until_dt)

        custom_limit_str = request.POST.get("custom_radar_limit", "").strip()
        if custom_limit_str.isdigit() and int(custom_limit_str) > 0:
            sub = target_user.subscription
            sub.custom_radar_limit = int(custom_limit_str)
            sub.save(update_fields=["custom_radar_limit"])

        until_desc = "για πάντα (Μόνιμη)" if not until_dt else f"έως {until_dt:%d/%m/%Y}"
        messages.success(request, f"Δόθηκε πρόσβαση {tier.upper()} ({until_desc}) στον χρήστη {target_user.email}.")

    elif action == "revoke":
        revoke_complimentary_access(request.user, target_user)
        messages.success(request, f"Αφαιρέθηκε η δωρεάν πρόσβαση από τον χρήστη {target_user.email}.")

    return redirect("superadmin:user_detail", user_id=target_user.id)


@superadmin_required
@require_POST
def user_send_yesterday_digest(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    try:
        from gemiapp.services import send_user_yesterday_digest
        count = send_user_yesterday_digest(target_user)
        log_admin_action(
            admin_user=request.user,
            action="send_user_yesterday_digest",
            target_type="User",
            target_id=target_user.id,
            target_repr=target_user.email,
            metadata={"company_count": count},
        )
        messages.success(request, f"Απεστάλησαν επιτυχώς {count} χθεσινές εγγραφές στο email {target_user.email}.")
    except Exception as exc:
        messages.error(request, f"Σφάλμα κατά την αποστολή email: {exc}")

    return redirect("superadmin:user_detail", user_id=target_user.id)


@superadmin_required
def subscription_list(request):
    subs = UserSubscription.objects.select_related("user").order_by("-created_at")

    search = request.GET.get("q", "").strip()
    if search:
        subs = subs.filter(Q(user__email__icontains=search) | Q(stripe_customer_id__icontains=search) | Q(stripe_subscription_id__icontains=search))

    tier = request.GET.get("tier", "").strip()
    if tier:
        subs = subs.filter(tier=tier)

    status = request.GET.get("status", "").strip()
    if status:
        subs = subs.filter(status=status)

    entitlement = request.GET.get("entitlement", "").strip()
    if entitlement == "yes":
        subs = subs.filter(entitlement_q(prefix=""))
    elif entitlement == "no":
        subs = subs.exclude(entitlement_q(prefix=""))

    paginator = Paginator(subs, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "superadmin/subscriptions/list.html", {
        "page_obj": page_obj,
        "search": search,
        "tier_filter": tier,
        "status_filter": status,
        "entitlement_filter": entitlement,
    })


@superadmin_required
def radar_list(request):
    radars = CustomerRadar.objects.filter(deleted_at__isnull=True).select_related("user", "user__subscription").annotate(
        match_count=Count("matches"),
        latest_match=Max("matches__matched_on"),
    ).order_by("-created_at")

    search = request.GET.get("q", "").strip()
    if search:
        radars = radars.filter(Q(name__icontains=search) | Q(user__email__icontains=search))

    active_filter = request.GET.get("active", "").strip()
    if active_filter == "1":
        radars = radars.filter(is_active=True)
    elif active_filter == "0":
        radars = radars.filter(is_active=False)

    paginator = Paginator(radars, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "superadmin/radars/list.html", {
        "page_obj": page_obj,
        "search": search,
        "active_filter": active_filter,
    })


@superadmin_required
def radar_detail(request, radar_id):
    radar = get_object_or_404(CustomerRadar.objects.select_related("user", "user__subscription"), pk=radar_id, deleted_at__isnull=True)
    matches = radar.matches.select_related("company", "lead").order_by("-matched_on")[:20]

    context = {
        "radar": radar,
        "matches": matches,
        "owner_entitlement": radar.user.subscription.has_entitlement,
        "owner_limit": get_user_radar_limit(radar.user),
    }
    return render(request, "superadmin/radars/detail.html", context)


@superadmin_required
def lead_list(request):
    leads = UserCompanyLead.objects.select_related("company", "user").prefetch_related("radar_matches__radar").order_by("-first_seen_at")

    search = request.GET.get("q", "").strip()
    if search:
        leads = leads.filter(Q(company__name__icontains=search) | Q(company__gemi_number__icontains=search) | Q(user__email__icontains=search))

    status = request.GET.get("status", "").strip()
    if status:
        leads = leads.filter(status=status)

    paginator = Paginator(leads, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "superadmin/leads/list.html", {
        "page_obj": page_obj,
        "search": search,
        "status_filter": status,
    })


@superadmin_required
def lead_detail(request, lead_id):
    lead = get_object_or_404(UserCompanyLead.objects.select_related("company", "user"), pk=lead_id)
    matches = RadarMatch.objects.filter(lead=lead).select_related("radar", "import_run")

    context = {
        "lead": lead,
        "matches": matches,
    }
    return render(request, "superadmin/leads/detail.html", context)


@superadmin_required
def pipeline_overview(request):
    runs = ImportRun.objects.order_by("-started_at")[:30]
    latest_run = runs[0] if runs else None

    return render(request, "superadmin/pipeline/overview.html", {
        "runs": runs,
        "latest_run": latest_run,
        "today_str": timezone.localdate().isoformat(),
    })


@superadmin_required
@require_POST
def pipeline_run_now(request):
    target_date_str = request.POST.get("target_date", "").strip()
    try:
        target_dt = date.fromisoformat(target_date_str) if target_date_str else timezone.localdate()
    except ValueError:
        messages.error(request, "Μη έγκυρη ημερομηνία για το pipeline.")
        return redirect("superadmin:pipeline_overview")

    try:
        import_for_date(target_dt)
        send_digests(target_dt)
        log_admin_action(
            admin_user=request.user,
            action="pipeline_manual_run",
            target_type="ImportRun",
            target_id=target_dt.isoformat(),
            target_repr=f"Pipeline for {target_dt.isoformat()}",
        )
        messages.success(request, f"Το pipeline εκτελέστηκε επιτυχώς για τις {target_dt.strftime('%d/%m/%Y')}.")
    except Exception as exc:
        messages.error(request, f"Σφάλμα εκτέλεσης pipeline: {str(exc)}")

    return redirect("superadmin:pipeline_overview")


@superadmin_required
def digest_list(request):
    deliveries = DigestDelivery.objects.select_related("user").order_by("-sent_at")

    search = request.GET.get("q", "").strip()
    if search:
        deliveries = deliveries.filter(user__email__icontains=search)

    status = request.GET.get("status", "").strip()
    if status:
        deliveries = deliveries.filter(status=status)

    paginator = Paginator(deliveries, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "superadmin/digests/list.html", {
        "page_obj": page_obj,
        "search": search,
        "status_filter": status,
    })


@superadmin_required
@require_POST
def digest_retry(request, delivery_id):
    delivery = get_object_or_404(DigestDelivery, pk=delivery_id)
    if delivery.status == "sent":
        messages.error(request, "Το digest έχει ήδη σταλεί επιτυχώς.")
        return redirect("superadmin:digest_list")

    try:
        send_digests(delivery.digest_date, delivery.frequency)
        log_admin_action(
            admin_user=request.user,
            action="digest_retry",
            target_type="DigestDelivery",
            target_id=delivery.id,
            target_repr=f"Digest for {delivery.user.email}",
        )
        messages.success(request, f"Εκτελέστηκε επανενέργεια αποστολής digest για {delivery.user.email}.")
    except Exception as exc:
        messages.error(request, f"Σφάλμα επανενέργειας digest: {str(exc)}")

    return redirect("superadmin:digest_list")


@superadmin_required
def health_overview(request):
    health = get_system_health()
    return render(request, "superadmin/health/overview.html", {"health": health})


@superadmin_required
def audit_list(request):
    audits = AdminAuditLog.objects.select_related("admin_user").order_by("-timestamp")

    search = request.GET.get("q", "").strip()
    if search:
        audits = audits.filter(Q(action__icontains=search) | Q(target_repr__icontains=search) | Q(admin_user__email__icontains=search))

    paginator = Paginator(audits, 30)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "superadmin/audit/list.html", {
        "page_obj": page_obj,
        "search": search,
    })


@superadmin_required
@require_POST
def impersonate_start(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    if target_user.is_superuser:
        messages.error(request, "Δεν επιτρέπεται impersonation Superadmin χρήστη.")
        return redirect("superadmin:user_detail", user_id=target_user.id)

    if request.session.get("impersonator_id"):
        messages.error(request, "Ένα impersonation είναι ήδη ενεργό.")
        return redirect("superadmin:user_list")

    admin_id = request.user.id
    log_admin_action(
        admin_user=request.user,
        action="impersonate_start",
        target_type="User",
        target_id=target_user.id,
        target_repr=target_user.email,
    )

    login(request, target_user)
    request.session["impersonator_id"] = admin_id
    messages.info(request, f"Ξεκινήσατε impersonation ως {target_user.email}.")
    return redirect("dashboard")


@require_POST
def impersonate_stop(request):
    impersonator_id = request.session.get("impersonator_id")
    if not impersonator_id:
        return redirect("dashboard")

    try:
        admin_user = User.objects.get(pk=impersonator_id, is_superuser=True)
        target_email = request.user.email
        log_admin_action(
            admin_user=admin_user,
            action="impersonate_stop",
            target_type="User",
            target_id=request.user.id,
            target_repr=target_email,
        )
        login(request, admin_user)
        if "impersonator_id" in request.session:
            del request.session["impersonator_id"]
        messages.success(request, "Επιστρέψατε στο Superadmin account σας.")
        return redirect("superadmin:overview")
    except User.DoesNotExist:
        messages.error(request, "Αδυναμία επαναφοράς Superadmin identity.")
        return redirect("login")
