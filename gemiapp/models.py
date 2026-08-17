from django.contrib.auth.models import User
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class ActivityCode(models.Model):
    code = models.CharField("ΚΑΔ", max_length=20, unique=True)
    normalized_code = models.CharField(max_length=16, unique=True, db_index=True)
    description = models.CharField("Περιγραφή δραστηριότητας", max_length=1000)
    source = models.URLField("Πηγή", max_length=500, blank=True)
    search_text = models.TextField(editable=False, db_index=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "ΚΑΔ"
        verbose_name_plural = "Κατάλογος ΚΑΔ"

    def __str__(self):
        return f"{self.code} — {self.description}"


class Company(models.Model):
    gemi_number = models.CharField("Αριθμός ΓΕΜΗ", max_length=24, unique=True, db_index=True)
    vat_number = models.CharField("ΑΦΜ", max_length=12, blank=True, db_index=True)
    name = models.CharField("Επωνυμία", max_length=500, db_index=True)
    trade_names = models.TextField("Διακριτικοί τίτλοι", blank=True)
    legal_type = models.CharField("Νομική μορφή", max_length=200, blank=True, db_index=True)
    status = models.CharField("Κατάσταση", max_length=120, blank=True)
    is_active = models.BooleanField("Ενεργή", default=True, db_index=True)
    incorporation_date = models.DateField("Ημερομηνία σύστασης", db_index=True)
    gemi_office = models.CharField("Υπηρεσία ΓΕΜΗ", max_length=300, blank=True)
    prefecture = models.CharField("Περιφερειακή ενότητα", max_length=120, blank=True, db_index=True)
    municipality = models.CharField("Δήμος", max_length=160, blank=True, db_index=True)
    city = models.CharField("Πόλη", max_length=160, blank=True)
    address = models.CharField("Διεύθυνση", max_length=500, blank=True)
    postal_code = models.CharField("ΤΚ", max_length=12, blank=True)
    email = models.EmailField("Email", blank=True)
    website = models.URLField("Ιστοσελίδα", blank=True)
    activities = models.JSONField("Δραστηριότητες", default=list, blank=True)
    raw_data = models.JSONField(default=dict, blank=True, editable=False)
    imported_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-incorporation_date", "-gemi_number"]
        verbose_name_plural = "Επιχειρήσεις"

    def __str__(self):
        return self.name

    @property
    def source_url(self):
        return f"https://publicity.businessportal.gr/company/{self.gemi_number}"


class CompanyActivity(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="activity_records")
    code = models.CharField("ΚΑΔ", max_length=16, db_index=True)
    description = models.CharField("Περιγραφή", max_length=1000, blank=True)
    activity_type = models.CharField("Τύπος", max_length=80, blank=True)

    class Meta:
        ordering = ["company_id", "code"]
        constraints = [
            models.UniqueConstraint(fields=["company", "code", "activity_type"], name="unique_company_activity")
        ]

    def __str__(self):
        return f"{self.company} · {self.code}"


class DigestPreference(models.Model):
    FREQUENCIES = [("daily", "Καθημερινά"), ("weekly", "Εβδομαδιαία"), ("off", "Ανενεργό")]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="digest_preference")
    frequency = models.CharField(max_length=10, choices=FREQUENCIES, default="daily")
    legal_types = models.JSONField(default=list, blank=True)
    prefectures = models.JSONField(default=list, blank=True)
    activity_codes = models.JSONField(default=list, blank=True)
    only_active = models.BooleanField(default=True)
    include_empty_digest = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.email or self.user.username} · {self.get_frequency_display()}"


class CustomerRadar(models.Model):
    FREQUENCIES = [("daily", "Καθημερινά"), ("weekly", "Εβδομαδιαία"), ("off", "Χωρίς email")]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="customer_radars")
    name = models.CharField("Όνομα Radar", max_length=80)
    is_active = models.BooleanField("Ενεργό", default=True)
    name_query = models.CharField("Λέξη στην επωνυμία", max_length=200, blank=True)
    prefectures = models.JSONField("Περιφερειακές ενότητες", default=list, blank=True)
    legal_types = models.JSONField("Νομικές μορφές", default=list, blank=True)
    only_active = models.BooleanField("Μόνο ενεργές επιχειρήσεις", default=True)
    frequency = models.CharField("Συχνότητα ενημέρωσης", max_length=10, choices=FREQUENCIES, default="daily")
    monitor_from = models.DateTimeField("Παρακολούθηση από", default=timezone.now)
    activity_codes = models.ManyToManyField(ActivityCode, blank=True, related_name="customer_radars")
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_active", "name"]
        indexes = [
            models.Index(fields=["user", "is_active"], name="radar_user_active_idx"),
            models.Index(fields=["frequency", "is_active"], name="radar_freq_active_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "user",
                condition=models.Q(deleted_at__isnull=True),
                name="unique_active_user_radar_name_ci",
            )
        ]

    def __str__(self):
        return f"{self.user.email or self.user.username} · {self.name}"


class UserCompanyLead(models.Model):
    STATUSES = [
        ("new", "Νέο"),
        ("viewed", "Προβλήθηκε"),
        ("contacted", "Επικοινώνησα"),
        ("interested", "Ενδιαφέρεται"),
        ("not_interested", "Δεν ενδιαφέρεται"),
        ("archived", "Αρχείο"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="company_leads")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="user_leads")
    status = models.CharField(max_length=20, choices=STATUSES, default="new")
    is_favorite = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-first_seen_at"]
        constraints = [models.UniqueConstraint(fields=["user", "company"], name="unique_user_company_lead")]
        indexes = [
            models.Index(fields=["user", "status", "-first_seen_at"], name="lead_user_status_seen_idx"),
            models.Index(fields=["user", "is_favorite"], name="lead_user_favorite_idx"),
        ]

    def __str__(self):
        return f"{self.user.email or self.user.username} · {self.company}"


class ImportRun(models.Model):
    STATUSES = [("running", "Σε εξέλιξη"), ("success", "Επιτυχία"), ("failed", "Αποτυχία")]
    target_date = models.DateField(db_index=True)
    status = models.CharField(max_length=12, choices=STATUSES, default="running")
    fetched_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]


class RadarMatch(models.Model):
    radar = models.ForeignKey(CustomerRadar, on_delete=models.CASCADE, related_name="matches")
    lead = models.ForeignKey(UserCompanyLead, on_delete=models.CASCADE, related_name="radar_matches")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="radar_matches")
    import_run = models.ForeignKey(ImportRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="radar_matches")
    matched_on = models.DateField(db_index=True)
    matched_activity_codes = models.JSONField(default=list, blank=True)
    match_reason = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-matched_on", "-created_at"]
        constraints = [models.UniqueConstraint(fields=["radar", "company"], name="unique_radar_company_match")]
        indexes = [
            models.Index(fields=["radar", "-matched_on"], name="match_radar_date_idx"),
            models.Index(fields=["lead", "-matched_on"], name="match_lead_date_idx"),
        ]

    def __str__(self):
        return f"{self.radar.name} · {self.company}"


class DigestDelivery(models.Model):
    STATUSES = [("sent", "Εστάλη"), ("skipped", "Παραλείφθηκε"), ("failed", "Απέτυχε")]
    FREQUENCIES = [("daily", "Καθημερινά"), ("weekly", "Εβδομαδιαία")]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="digest_deliveries")
    frequency = models.CharField(max_length=10, choices=FREQUENCIES, default="daily")
    digest_date = models.DateField(db_index=True)
    company_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, choices=STATUSES)
    error_message = models.TextField(blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "digest_date", "frequency"], name="unique_user_digest_delivery")]
        ordering = ["-sent_at"]


RADAR_LIMITS = {
    "free": 1,
    "pro": 5,
    "business": 25,
}

def get_user_radar_limit(user):
    try:
        return RADAR_LIMITS.get(user.subscription.tier, 1)
    except UserSubscription.DoesNotExist:
        return RADAR_LIMITS["free"]

class UserSubscription(models.Model):
    TIERS = [("free", "Free"), ("pro", "Pro"), ("business", "Business")]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="subscription")
    tier = models.CharField(max_length=20, choices=TIERS, default="free")
    stripe_customer_id = models.CharField(max_length=100, blank=True)
    stripe_subscription_id = models.CharField(max_length=100, blank=True)
    active_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} - {self.get_tier_display()}"
