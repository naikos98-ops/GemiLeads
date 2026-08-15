from django.contrib.auth.models import User
from django.db import models


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


class DigestDelivery(models.Model):
    STATUSES = [("sent", "Εστάλη"), ("skipped", "Παραλείφθηκε"), ("failed", "Απέτυχε")]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="digest_deliveries")
    digest_date = models.DateField(db_index=True)
    company_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, choices=STATUSES)
    error_message = models.TextField(blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "digest_date"], name="unique_user_daily_digest")]
        ordering = ["-sent_at"]
