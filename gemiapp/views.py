import csv
from datetime import timedelta
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from .forms import DigestPreferenceForm, SignupForm
from .kad import normalize_kad_code, normalize_kad_search
from .models import ActivityCode, Company, DigestPreference, DigestDelivery, ImportRun


MAX_SELECTED_KADS = 25


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


def home(request):
    today = timezone.localdate()
    context = {
        "today_count": Company.objects.filter(incorporation_date=today).count(),
        "latest_companies": Company.objects.all()[:6],
        "recent_count": Company.objects.filter(incorporation_date__gte=today - timedelta(days=6)).count(),
    }
    return render(request, "home.html", context)


def signup(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Καλώς ήρθες! Το καθημερινό digest είναι ενεργό.")
        return redirect("dashboard")
    return render(request, "registration/signup.html", {"form": form})


def _filtered_companies(request):
    qs = Company.objects.all()
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
        qs = qs.filter(incorporation_date__lte=date_to)
    if activity_codes:
        qs = qs.filter(activity_records__code__in=activity_codes).distinct()
    return qs


@login_required
def dashboard(request):
    today = timezone.localdate()
    companies = _filtered_companies(request)
    preference, _ = DigestPreference.objects.get_or_create(user=request.user)
    selected_codes = _requested_kad_codes(request)
    date_from = parse_date(request.GET.get("date_from", "").strip())
    date_to = parse_date(request.GET.get("date_to", "").strip())
    context = {
        "companies": companies,
        "result_count": companies.count(),
        "today_count": Company.objects.filter(incorporation_date=today).count(),
        "week_count": Company.objects.filter(incorporation_date__gte=today - timedelta(days=6)).count(),
        "prefectures": Company.objects.exclude(prefecture="").values_list("prefecture", flat=True).distinct().order_by("prefecture"),
        "legal_types": Company.objects.exclude(legal_type="").values_list("legal_type", flat=True).distinct().order_by("legal_type"),
        "preference": preference,
        "latest_run": ImportRun.objects.first(),
        "latest_delivery": DigestDelivery.objects.filter(user=request.user).first(),
        "chart_data": list(Company.objects.filter(incorporation_date__gte=today - timedelta(days=6)).values("incorporation_date").annotate(total=Count("id")).order_by("incorporation_date")),
        "selected_kads": _catalog_entries(selected_codes),
        "date_range_error": bool(date_from and date_to and date_from > date_to),
    }
    return render(request, "dashboard.html", context)


@login_required
def settings_view(request):
    preference, _ = DigestPreference.objects.get_or_create(user=request.user)
    form = DigestPreferenceForm(request.POST or None, instance=preference)
    if request.method == "POST" and form.is_valid():
        selected_codes = _requested_kad_codes(request, "activity_codes")
        preference = form.save(commit=False)
        preference.activity_codes = [item.normalized_code for item in _catalog_entries(selected_codes)]
        preference.save()
        messages.success(request, "Οι προτιμήσεις email αποθηκεύτηκαν.")
        return redirect("settings")
    selected_codes = _requested_kad_codes(request, "activity_codes") if request.method == "POST" else preference.activity_codes
    return render(request, "settings.html", {"form": form, "preference": preference, "selected_kads": _catalog_entries(selected_codes)})


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
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="gemi-signal-export.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["Ημερομηνία", "Αρ. ΓΕΜΗ", "ΑΦΜ", "Επωνυμία", "Νομική μορφή", "Νομός", "Πόλη", "Email", "Website"])
    for company in _filtered_companies(request):
        writer.writerow([company.incorporation_date, company.gemi_number, company.vat_number, company.name, company.legal_type, company.prefecture, company.city, company.email, company.website])
    return response
