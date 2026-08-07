import csv
from datetime import timedelta
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from .forms import DigestPreferenceForm, SignupForm
from .models import Company, DigestPreference, DigestDelivery, ImportRun


def home(request):
    today = timezone.localdate()
    context = {
        "today_count": Company.objects.filter(incorporation_date=today).count(),
        "latest_companies": Company.objects.all()[:6],
        "recent_count": Company.objects.filter(incorporation_date__gte=today - timedelta(days=7)).count(),
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
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()
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
    return qs


@login_required
def dashboard(request):
    today = timezone.localdate()
    companies = _filtered_companies(request)
    preference, _ = DigestPreference.objects.get_or_create(user=request.user)
    context = {
        "companies": companies[:100],
        "result_count": companies.count(),
        "today_count": Company.objects.filter(incorporation_date=today).count(),
        "week_count": Company.objects.filter(incorporation_date__gte=today - timedelta(days=7)).count(),
        "prefectures": Company.objects.exclude(prefecture="").values_list("prefecture", flat=True).distinct().order_by("prefecture"),
        "legal_types": Company.objects.exclude(legal_type="").values_list("legal_type", flat=True).distinct().order_by("legal_type"),
        "preference": preference,
        "latest_run": ImportRun.objects.first(),
        "latest_delivery": DigestDelivery.objects.filter(user=request.user).first(),
        "chart_data": list(Company.objects.filter(incorporation_date__gte=today - timedelta(days=6)).values("incorporation_date").annotate(total=Count("id")).order_by("incorporation_date")),
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
def export_csv(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="gemi-signal-export.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["Ημερομηνία", "Αρ. ΓΕΜΗ", "ΑΦΜ", "Επωνυμία", "Νομική μορφή", "Νομός", "Πόλη", "Email", "Website"])
    for company in _filtered_companies(request):
        writer.writerow([company.incorporation_date, company.gemi_number, company.vat_number, company.name, company.legal_type, company.prefecture, company.city, company.email, company.website])
    return response
