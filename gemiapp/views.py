import csv
from datetime import timedelta
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST
from django.contrib.auth.views import LoginView
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit
from .forms import CustomerRadarForm, DigestPreferenceForm, LeadNotesForm, LeadStatusForm, SignupForm
from .kad import normalize_kad_code, normalize_kad_search
from .models import (
    ActivityCode,
    Company,
    CustomerRadar,
    DigestPreference,
    DigestDelivery,
    ImportRun,
    UserCompanyLead,
    get_user_radar_limit,
)
from .services import filter_companies_for_radar


MAX_SELECTED_KADS = 25
# An unfiltered export would otherwise buffer every company in the database into one response.
MAX_EXPORT_ROWS = 5000
LEAD_STATUSES = dict(UserCompanyLead.STATUSES)


def _requested_kad_codes(request, field_name="kad"):
    result = []
    for value in request.GET.getlist(field_name) if request.method == "GET" else request.POST.getlist(field_name):
        code = normalize_kad_code(value)
        if code and code not in result:
            result.append(code)
        if len(result) == MAX_SELECTED_KADS:
            break
    return result


def _catalog_entries(codes):
    entries = {item.normalized_code: item for item in ActivityCode.objects.filter(normalized_code__in=codes)}
    return [entries[code] for code in codes if code in entries]


def _too_many_requested_kads(request, field_name="activity_codes"):
    codes = {normalize_kad_code(value) for value in request.POST.getlist(field_name)}
    codes.discard("")
    return len(codes) > MAX_SELECTED_KADS


def _radar_companies(form, activity_codes):
    return filter_companies_for_radar(
        Company.objects.all(),
        name_query=form.cleaned_data.get("name_query", ""),
        prefectures=form.cleaned_data.get("prefectures", []),
        legal_types=form.cleaned_data.get("legal_types", []),
        only_active=form.cleaned_data.get("only_active", True),
        activity_codes=activity_codes,
    )


def home(request):
    from django.core.cache import cache

    today = timezone.localdate()
    # Public landing page: these aggregates change at most once per daily import, so a short
    # cache keeps three COUNT queries off the critical path without affecting correctness.
    context = {
        "today_count": cache.get_or_set(
            f"home_today_count_{today}",
            lambda: Company.objects.filter(incorporation_date=today).count(),
            600,
        ),
        "latest_companies": Company.objects.all()[:6],
        "recent_count": cache.get_or_set(
            f"home_recent_count_{today}",
            lambda: Company.objects.filter(incorporation_date__gte=today - timedelta(days=6)).count(),
            600,
        ),
    }
    return render(request, "home.html", context)


@method_decorator(ratelimit(key="ip", rate="5/m", block=True), name="dispatch")
class RateLimitedLoginView(LoginView):
    pass


@ratelimit(key="ip", rate="5/m", block=True)
def signup(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        send_verification_email(request, user)
        return render(request, "registration/verify_pending.html", {"email": user.email})
    return render(request, "registration/signup.html", {"form": form})


def send_verification_email(request, user):
    """Build and send the account verification link for ``user``."""
    domain = request.get_host()
    protocol = "https" if request.is_secure() else "http"
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    verify_url = f"{protocol}://{domain}{reverse('verify_email', kwargs={'uidb64': uid, 'token': token})}"

    context = {"verify_url": verify_url, "user": user}
    send_mail(
        "Επιβεβαίωση email στο Gemi Leads",
        render_to_string("emails/verification.txt", context),
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
        html_message=render_to_string("emails/verification.html", context),
    )


@ratelimit(key="ip", rate="5/h", block=True)
def resend_verification(request):
    """Self-service escape hatch for an account stuck unverified.

    Without this the user is in a dead end: signing up again is refused because the email exists,
    Django's password reset silently ignores inactive users, and the login error says nothing about
    verification. Always renders the same confirmation so the page cannot be used to discover which
    email addresses have accounts.
    """
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        email = (request.POST.get("email") or "").strip()
        if email:
            user = User.objects.filter(email__iexact=email, is_active=False).first()
            if user is not None:
                send_verification_email(request, user)
        return render(request, "registration/verify_pending.html", {"email": email, "resent": True})

    return render(request, "registration/resend_verification.html")

def verify_email(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user is not None and default_token_generator.check_token(user, token):
        user.is_active = True
        user.save()
        login(request, user)
        messages.success(request, "Το email σου επιβεβαιώθηκε! Καλώς ήρθες στο Gemi Leads.")
        return redirect("dashboard")
    else:
        messages.error(request, "Ο σύνδεσμος επιβεβαίωσης είναι άκυρος ή έχει λήξει.")
        return redirect("login")

def unsubscribe(request, token):
    signer = TimestampSigner()
    try:
        user_id = signer.unsign(token, max_age=timedelta(days=30))
        user = User.objects.get(pk=user_id)
    except (BadSignature, SignatureExpired, User.DoesNotExist):
        return render(request, "unsubscribed.html", {"error": "Ο σύνδεσμος είναι άκυρος ή έχει λήξει."})

    # get_or_create, not user.digest_preference: a legacy account without the row used to raise
    # RelatedObjectDoesNotExist here, turning an unsubscribe link into a 500.
    preference, _ = DigestPreference.objects.get_or_create(user=user)
    preference.frequency = "off"
    preference.save(update_fields=["frequency", "updated_at"])
    return render(request, "unsubscribed.html")


def _filtered_companies(request):
    today = timezone.localdate()
    qs = Company.objects.filter(incorporation_date__lte=today)
    query = request.GET.get("q", "").strip()
    prefecture = request.GET.get("prefecture", "").strip()
    legal_type = request.GET.get("legal_type", "").strip()
    date_from = parse_date(request.GET.get("date_from", "").strip())
    date_to = parse_date(request.GET.get("date_to", "").strip())
    activity_codes = _requested_kad_codes(request)
    if query:
        qs = qs.filter(Q(name__icontains=query) | Q(vat_number__icontains=query) | Q(gemi_number__icontains=query))
    if prefecture:
        qs = qs.filter(prefecture=prefecture)
    if legal_type:
        qs = qs.filter(legal_type=legal_type)
    if date_from:
        qs = qs.filter(incorporation_date__gte=date_from)
    if date_to:
        qs = qs.filter(incorporation_date__lte=min(date_to, today))
    if activity_codes:
        qs = qs.filter(activity_records__code__in=activity_codes).distinct()
    return qs


@login_required
def dashboard(request):
    today = timezone.localdate()
    companies = _filtered_companies(request)
    paginator = Paginator(companies, 20)
    page_num = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_num)

    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json" or (request.GET.get("page") and str(request.GET.get("page")).isdigit() and int(request.GET.get("page")) > 1):
        html = render_to_string("includes/dashboard_rows.html", {"companies": page_obj, "request": request, "is_ajax": True, "today": today})
        return JsonResponse({
            "html": html,
            "has_next": page_obj.has_next(),
            "next_page": page_obj.next_page_number() if page_obj.has_next() else None,
            "total_count": paginator.count,
        })

    from django.core.cache import cache
    prefectures = cache.get_or_set(
        "dashboard_prefectures",
        lambda: list(Company.objects.filter(incorporation_date__lte=today).exclude(prefecture="").values_list("prefecture", flat=True).distinct().order_by("prefecture")),
        3600,
    )
    legal_types = cache.get_or_set(
        "dashboard_legal_types",
        lambda: list(Company.objects.filter(incorporation_date__lte=today).exclude(legal_type="").values_list("legal_type", flat=True).distinct().order_by("legal_type")),
        3600,
    )

    preference, _ = DigestPreference.objects.get_or_create(user=request.user)
    selected_codes = _requested_kad_codes(request)
    date_from = parse_date(request.GET.get("date_from", "").strip())
    date_to = parse_date(request.GET.get("date_to", "").strip())
    user_leads = UserCompanyLead.objects.filter(user=request.user)
    subscription = getattr(request.user, "subscription", None)
    has_paid = subscription is not None and subscription.has_entitlement
    # Enterprise / Custom are the tiers fed by the 3-hour GEMI pipeline, so they get a live panel
    # showing what today's intraday runs have already brought in.
    is_realtime_tier = has_paid and subscription.effective_tier in ("enterprise", "custom")
    last_intraday_run = (
        ImportRun.objects.filter(target_date=today, status="success").order_by("-finished_at").first()
        if is_realtime_tier else None
    )
    context = {
        "companies": page_obj,
        "page_obj": page_obj,
        "has_next_page": page_obj.has_next(),
        "result_count": paginator.count,
        "today_count": Company.objects.filter(incorporation_date=today).count(),
        "week_count": Company.objects.filter(incorporation_date__gte=today - timedelta(days=6), incorporation_date__lte=today).count(),
        "prefectures": prefectures,
        "legal_types": legal_types,
        "preference": preference,
        "latest_run": ImportRun.objects.first(),
        "latest_delivery": DigestDelivery.objects.filter(user=request.user).first(),
        "chart_data": list(Company.objects.filter(incorporation_date__gte=today - timedelta(days=6), incorporation_date__lte=today).values("incorporation_date").annotate(total=Count("id")).order_by("incorporation_date")),
        "selected_kads": _catalog_entries(selected_codes),
        "date_range_error": bool(date_from and date_to and date_from > date_to),
        "lead_count": user_leads.count(),
        "unread_lead_count": user_leads.filter(status="new").count(),
        "interested_lead_count": user_leads.filter(status="interested").count(),
        "active_radar_count": CustomerRadar.objects.filter(
            user=request.user, is_active=True, deleted_at__isnull=True
        ).count() if has_paid else 0,
        "recent_leads": user_leads.select_related("company").prefetch_related("radar_matches__radar")[:6],
        "has_active_paid_subscription": has_paid,
        "today": today,
        "is_realtime_tier": is_realtime_tier,
        "last_intraday_run": last_intraday_run,
    }
    return render(request, "dashboard.html", context)


@login_required
def settings_view(request):
    preference, _ = DigestPreference.objects.get_or_create(user=request.user)
    form = DigestPreferenceForm(request.POST or None, instance=preference)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Οι προτιμήσεις email αποθηκεύτηκαν.")
        return redirect("settings")
    return render(request, "settings.html", {"form": form, "preference": preference})


@login_required
def radar_list(request):
    radars = (
        CustomerRadar.objects.filter(user=request.user, deleted_at__isnull=True)
        .annotate(match_count=Count("matches"), latest_match=Max("matches__matched_on"))
        .prefetch_related("activity_codes")
    )
    limit = get_user_radar_limit(request.user)
    count = radars.count()
    has_paid = hasattr(request.user, "subscription") and request.user.subscription.has_entitlement
    return render(request, "radars/list.html", {
        "radars": radars,
        "radar_limit": limit,
        "radar_count": count,
        "has_active_paid_subscription": has_paid,
    })


def _save_radar(request, radar=None):
    limit = get_user_radar_limit(request.user)
    form = CustomerRadarForm(request.POST or None, instance=radar, user=request.user)
    selected_codes = (
        _requested_kad_codes(request, "activity_codes")
        if request.method == "POST"
        else list(radar.activity_codes.values_list("normalized_code", flat=True)) if radar else []
    )
    selected_kads = _catalog_entries(selected_codes)
    if request.method == "POST":
        if limit == 0:
            form.add_error(None, "Απαιτείται ενεργή συνδρομή (Pro ή Business) για τη δημιουργία ή επεξεργασία Ραντάρ.")
        elif _too_many_requested_kads(request):
            form.add_error(None, f"Μπορείς να επιλέξεις έως {MAX_SELECTED_KADS} ΚΑΔ.")
            
        if not radar and limit > 0:
            current_count = CustomerRadar.objects.filter(user=request.user, deleted_at__isnull=True).count()
            if current_count >= limit:
                form.add_error(None, f"Έχεις φτάσει το όριο των {limit} Ραντάρ του πλάνου σου. Διέγραψε κάποιο για να προσθέσεις νέο.")

        if form.is_valid() and limit > 0:
            saved_radar = form.save(commit=False)
            saved_radar.user = request.user
            if not saved_radar.pk:
                saved_radar.monitor_from = timezone.now()
            saved_radar.save()
            saved_radar.activity_codes.set(selected_kads)
            messages.success(request, "Το Radar αποθηκεύτηκε και θα παρακολουθεί τις επόμενες εισαγωγές.")
            return redirect("radar_detail", pk=saved_radar.pk)
    return render(request, "radars/form.html", {
        "form": form,
        "radar": radar,
        "selected_kads": selected_kads,
        "radar_limit": limit,
    })


@login_required
def radar_create(request):
    return _save_radar(request)


@login_required
def radar_edit(request, pk):
    radar = get_object_or_404(CustomerRadar, pk=pk, user=request.user, deleted_at__isnull=True)
    return _save_radar(request, radar)


@login_required
def radar_detail(request, pk):
    radar = get_object_or_404(
        CustomerRadar.objects.prefetch_related("activity_codes"),
        pk=pk,
        user=request.user,
        deleted_at__isnull=True,
    )
    matches = radar.matches.select_related("company", "lead")
    selected_status = request.GET.get("status", "").strip()
    if selected_status in LEAD_STATUSES:
        matches = matches.filter(lead__status=selected_status)
    else:
        selected_status = ""
    all_matches = radar.matches.all()
    return render(request, "radars/detail.html", {
        "radar": radar,
        "matches": matches,
        "match_count": all_matches.count(),
        "filtered_match_count": matches.count(),
        "new_match_count": all_matches.filter(lead__status="new").count(),
        "favorite_match_count": all_matches.filter(lead__is_favorite=True).count(),
        "lead_statuses": UserCompanyLead.STATUSES,
        "selected_status": selected_status,
    })


def _lead_queryset(user):
    return (
        UserCompanyLead.objects.filter(user=user)
        .select_related("company")
        .prefetch_related("radar_matches__radar")
    )


@login_required
def lead_list(request):
    leads = _lead_queryset(request.user)
    selected_status = request.GET.get("status", "").strip()
    selected_radar = request.GET.get("radar", "").strip()
    query = request.GET.get("q", "").strip()
    favorites_only = request.GET.get("favorite") == "1"

    if selected_status in LEAD_STATUSES:
        leads = leads.filter(status=selected_status)
    else:
        selected_status = ""
    if selected_radar:
        radar = get_object_or_404(
            CustomerRadar,
            pk=selected_radar,
            user=request.user,
            deleted_at__isnull=True,
        )
        leads = leads.filter(radar_matches__radar=radar)
    else:
        radar = None
    if query:
        leads = leads.filter(
            Q(company__name__icontains=query)
            | Q(company__gemi_number__icontains=query)
            | Q(company__vat_number__icontains=query)
        )
    if favorites_only:
        leads = leads.filter(is_favorite=True)

    all_user_leads = UserCompanyLead.objects.filter(user=request.user)
    return render(request, "leads/list.html", {
        "leads": leads.distinct(),
        "result_count": leads.distinct().count(),
        "lead_statuses": UserCompanyLead.STATUSES,
        "selected_status": selected_status,
        "selected_radar": radar,
        "favorites_only": favorites_only,
        "radars": CustomerRadar.objects.filter(user=request.user, deleted_at__isnull=True),
        "new_count": all_user_leads.filter(status="new").count(),
        "favorite_count": all_user_leads.filter(is_favorite=True).count(),
        "interested_count": all_user_leads.filter(status="interested").count(),
    })


@login_required
def company_detail(request, gemi_number):
    company = get_object_or_404(Company.objects.prefetch_related("activity_records"), gemi_number=gemi_number)

    # A lead is a radar outcome, not a side effect of browsing. Creating one for a user without an
    # entitlement polluted their Lead Inbox and the Superadmin lead metrics with rows that have no
    # RadarMatch behind them. Existing leads stay visible either way.
    subscription = getattr(request.user, "subscription", None)
    if subscription is not None and subscription.has_entitlement:
        lead, _ = UserCompanyLead.objects.get_or_create(user=request.user, company=company)
    else:
        lead = UserCompanyLead.objects.filter(user=request.user, company=company).first()

    if lead is not None and lead.status == "new":
        lead.status = "viewed"
        lead.save(update_fields=["status", "updated_at"])

    return render(request, "companies/detail.html", {
        "lead": lead,
        "company": company,
        "status_form": LeadStatusForm(instance=lead) if lead is not None else None,
        "notes_form": LeadNotesForm(instance=lead) if lead is not None else None,
    })


@login_required
@require_POST
def lead_status(request, pk):
    lead = get_object_or_404(UserCompanyLead, pk=pk, user=request.user)
    form = LeadStatusForm(request.POST, instance=lead)
    if form.is_valid():
        form.save()
        messages.success(request, "Η κατάσταση του lead ενημερώθηκε.")
    else:
        messages.error(request, "Η κατάσταση που επέλεξες δεν είναι έγκυρη.")
    return redirect("company_detail", gemi_number=lead.company.gemi_number)


@login_required
@require_POST
def lead_favorite(request, pk):
    lead = get_object_or_404(UserCompanyLead, pk=pk, user=request.user)
    lead.is_favorite = not lead.is_favorite
    lead.save(update_fields=["is_favorite", "updated_at"])
    messages.success(request, "Το lead προστέθηκε στα αγαπημένα." if lead.is_favorite else "Το lead αφαιρέθηκε από τα αγαπημένα.")
    return redirect("company_detail", gemi_number=lead.company.gemi_number)


@login_required
@require_POST
def lead_notes(request, pk):
    lead = get_object_or_404(UserCompanyLead, pk=pk, user=request.user)
    form = LeadNotesForm(request.POST, instance=lead)
    if form.is_valid():
        form.save()
        messages.success(request, "Οι ιδιωτικές σημειώσεις αποθηκεύτηκαν.")
    else:
        messages.error(request, "Οι σημειώσεις δεν αποθηκεύτηκαν. Έλεγξε το μέγεθός τους.")
    return redirect("company_detail", gemi_number=lead.company.gemi_number)


@login_required
def radar_export_csv(request, pk):
    radar = get_object_or_404(CustomerRadar, pk=pk, user=request.user, deleted_at__isnull=True)
    if get_user_radar_limit(request.user) == 0:
        messages.error(request, "Απαιτείται ενεργή συνδρομή για την εξαγωγή CSV.")
        return redirect("pricing")
    matches = radar.matches.select_related("company", "lead")
    selected_status = request.GET.get("status", "").strip()
    if selected_status in LEAD_STATUSES:
        matches = matches.filter(lead__status=selected_status)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="radar-{radar.pk}-leads.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow([
        "Ημερομηνία match", "Αρ. ΓΕΜΗ", "ΑΦΜ", "Επωνυμία", "Νομική μορφή",
        "Περιφέρεια", "Πόλη", "Κατάσταση lead", "Αγαπημένο", "ΚΑΔ match",
        "Email", "Website", "Σημειώσεις",
    ])
    for match in matches:
        company = match.company
        writer.writerow([
            match.matched_on,
            company.gemi_number,
            company.vat_number,
            company.name,
            company.legal_type,
            company.prefecture,
            company.city,
            match.lead.get_status_display(),
            "Ναι" if match.lead.is_favorite else "Όχι",
            ", ".join(match.matched_activity_codes),
            company.email,
            company.website,
            match.lead.notes,
        ])
    return response


@login_required
@require_POST
def radar_toggle(request, pk):
    radar = get_object_or_404(CustomerRadar, pk=pk, user=request.user, deleted_at__isnull=True)
    limit = get_user_radar_limit(request.user)
    if limit == 0:
        messages.error(request, "Απαιτείται ενεργή συνδρομή για την ενεργοποίηση Ραντάρ.")
        return redirect("pricing")
    if not radar.is_active:
        active_count = CustomerRadar.objects.filter(user=request.user, is_active=True, deleted_at__isnull=True).exclude(pk=pk).count()
        if active_count >= limit:
            messages.error(request, f"Έχεις φτάσει το όριο των {limit} ενεργών Ραντάρ του πλάνου σου.")
            return redirect("radar_list")
        radar.monitor_from = timezone.now()
    radar.is_active = not radar.is_active
    radar.save(update_fields=["is_active", "monitor_from", "updated_at"])
    messages.success(request, "Το Radar ενεργοποιήθηκε." if radar.is_active else "Το Radar τέθηκε σε παύση.")
    return redirect("radar_list")


@login_required
@require_POST
def radar_delete(request, pk):
    radar = get_object_or_404(CustomerRadar, pk=pk, user=request.user, deleted_at__isnull=True)
    radar.is_active = False
    radar.deleted_at = timezone.now()
    radar.save(update_fields=["is_active", "deleted_at", "updated_at"])
    messages.success(request, "Το Radar αφαιρέθηκε. Τα δεδομένα του διατηρούνται προσωρινά με ασφάλεια.")
    return redirect("radar_list")


@login_required
@require_POST
def radar_preview(request):
    if get_user_radar_limit(request.user) == 0:
        return JsonResponse({"ok": False, "error": "Απαιτείται ενεργή συνδρομή για την προεπισκόπηση Ραντάρ."}, status=403)
    radar = None
    if request.POST.get("radar_id"):
        radar = get_object_or_404(
            CustomerRadar,
            pk=request.POST["radar_id"],
            user=request.user,
            deleted_at__isnull=True,
        )
    form = CustomerRadarForm(request.POST, instance=radar, user=request.user)
    codes = _requested_kad_codes(request, "activity_codes")
    if _too_many_requested_kads(request):
        form.add_error(None, f"Μπορείς να επιλέξεις έως {MAX_SELECTED_KADS} ΚΑΔ.")
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors.get_json_data()}, status=400)
    companies = _radar_companies(form, codes)
    samples = list(companies.values("name", "gemi_number", "incorporation_date", "prefecture")[:5])
    for sample in samples:
        sample["incorporation_date"] = sample["incorporation_date"].isoformat()
    return JsonResponse({"ok": True, "count": companies.count(), "samples": samples})


@login_required
def kad_search(request):
    query = request.GET.get("q", "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})
    normalized_text = normalize_kad_search(query)
    normalized_code = normalize_kad_code(query)
    results = ActivityCode.objects.all()
    if normalized_code and not any(character.isalpha() for character in query):
        results = results.filter(normalized_code__startswith=normalized_code)
    else:
        for token in normalized_text.split():
            results = results.filter(search_text__contains=token)
    results = results.order_by("code")[:20]
    return JsonResponse({"results": [
        {"code": item.code, "normalized_code": item.normalized_code, "description": item.description}
        for item in results
    ]})


@login_required
def export_csv(request):
    if get_user_radar_limit(request.user) == 0:
        messages.error(request, "Απαιτείται ενεργή συνδρομή για την εξαγωγή CSV.")
        return redirect("pricing")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="gemi-leads-export.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["Ημερομηνία", "Αρ. ΓΕΜΗ", "ΑΦΜ", "Επωνυμία", "Νομική μορφή", "Νομός", "Πόλη", "Email", "Website"])
    companies = _filtered_companies(request).only(
        "incorporation_date", "gemi_number", "vat_number", "name",
        "legal_type", "prefecture", "city", "email", "website",
    )
    for company in companies[:MAX_EXPORT_ROWS].iterator(chunk_size=1000):
        writer.writerow([company.incorporation_date, company.gemi_number, company.vat_number, company.name, company.legal_type, company.prefecture, company.city, company.email, company.website])
    return response
