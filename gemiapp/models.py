from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
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
    search_name = models.CharField(max_length=500, blank=True, editable=False, db_index=True)
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

    def save(self, *args, **kwargs):
        from .kad import normalize_kad_search

        search_name = normalize_kad_search(self.name)
        if search_name != self.search_name:
            self.search_name = search_name
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "search_name" not in update_fields:
                kwargs["update_fields"] = list(update_fields) + ["search_name"]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    @property
    def source_url(self):
        return f"https://publicity.businessportal.gr/company/{self.gemi_number}"

    @property
    def people(self):
        """Partners, managers and legal representatives, as published by ΓΕΜΗ.

        The API returns these under ``persons`` and the importer already stores the whole
        payload, so nothing extra is fetched. Historical entries carry a ``dtTo`` end date
        and are dropped: showing someone who has left as a current manager would be worse
        than showing nobody. Sole traders legitimately have no entry here, their name is
        the company name, so an empty list is normal rather than missing data.

        People who have objected under Article 21 GDPR are removed here, so no caller can
        display them by accident. See :class:`PersonSuppression`.
        """
        from .kad import normalize_kad_search

        entries = (self.raw_data or {}).get("persons") or []
        if not entries:
            return []
        # Filtered here rather than at the call sites: this property is the only place
        # people are produced, so an objection cannot be missed by a new caller.
        suppressed = PersonSuppression.names_for(self)
        people = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("dtTo"):
                continue
            name = str(entry.get("personName") or entry.get("businessName") or "").strip()
            if not name or normalize_kad_search(name) in suppressed:
                continue
            people.append({
                "name": name,
                "role": str(entry.get("role") or "").strip(),
                "category": str(entry.get("category") or "").strip(),
                "percentage": str(entry.get("percentage") or "").strip(),
                "represents_alone": bool(entry.get("isRepresentativeAlone")),
            })
        # Representatives first, then by stake, so the person worth contacting leads.
        people.sort(key=lambda p: (not p["represents_alone"], "Διαχειριστ" not in p["role"], p["name"]))
        return people


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
    FREQUENCIES = [("daily", "Καθημερινά"), ("off", "Ανενεργό")]
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
    FREQUENCIES = [("daily", "Καθημερινά"), ("off", "Χωρίς email")]

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
    FREQUENCIES = [("daily", "Καθημερινά"), ("intraday", "3-ωρο / Real-Time"), ("manual_yesterday", "Χειροκίνητο (Χθες)"), ("weekly", "Εβδομαδιαία (καταργήθηκε)")]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="digest_deliveries")
    frequency = models.CharField(max_length=30, choices=FREQUENCIES, default="daily")
    digest_date = models.DateField(db_index=True)
    company_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, choices=STATUSES)
    error_message = models.TextField(blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "digest_date", "frequency"], name="unique_user_digest_delivery")]
        ordering = ["-sent_at"]


# Single source of truth for how many radars each tier may keep active.
# Keep in sync with templates/pricing.html and README.md.
RADAR_LIMITS = {
    "free": 0,
    "pro": 5,
    "business": 10,
    "enterprise": 15,
    "custom": 15,
}


PAID_TIERS = ("pro", "business", "enterprise", "custom")


def paid_subscription_q(prefix="subscription__"):
    """Database equivalent of ``UserSubscription.has_active_paid_subscription``."""
    return models.Q(**{f"{prefix}tier__in": PAID_TIERS, f"{prefix}status__in": UserSubscription.ALLOWED_PAID_STATUSES})


def complimentary_q(prefix="subscription__", now=None):
    """Database equivalent of ``UserSubscription.has_valid_complimentary_access``."""
    now = now or timezone.now()
    return models.Q(**{f"{prefix}complimentary_tier__in": PAID_TIERS}) & (
        models.Q(**{f"{prefix}complimentary_until__isnull": True})
        | models.Q(**{f"{prefix}complimentary_until__gte": now})
    )


def entitlement_q(prefix="subscription__", now=None):
    """Database equivalent of ``UserSubscription.has_entitlement``."""
    return paid_subscription_q(prefix) | complimentary_q(prefix, now)


def effective_tier_q(tier, prefix="subscription__", now=None):
    """Database equivalent of ``UserSubscription.effective_tier == tier``."""
    paid = paid_subscription_q(prefix)
    complimentary = complimentary_q(prefix, now)
    if tier == "free":
        return ~paid & ~complimentary
    return (paid & models.Q(**{f"{prefix}tier": tier})) | (
        ~paid & complimentary & models.Q(**{f"{prefix}complimentary_tier": tier})
    )


def get_user_radar_limit(user):
    try:
        return user.subscription.radar_limit
    except (UserSubscription.DoesNotExist, AttributeError):
        return 0


class UserSubscription(models.Model):
    TIERS = [("free", "Free (Legacy)"), ("pro", "Pro"), ("business", "Business"), ("enterprise", "Enterprise / Real-Time"), ("custom", "Custom / Enterprise")]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="subscription")
    tier = models.CharField(max_length=20, choices=TIERS, default="free")
    status = models.CharField(max_length=30, default="inactive")
    complimentary_tier = models.CharField(max_length=20, choices=[("none", "None"), ("pro", "Pro"), ("business", "Business"), ("enterprise", "Enterprise / Real-Time"), ("custom", "Custom / Enterprise")], default="none")
    complimentary_until = models.DateTimeField(null=True, blank=True)
    custom_radar_limit = models.IntegerField(null=True, blank=True, help_text="Custom radar limit set by Superadmin")
    last_sent_company_id = models.IntegerField(default=0, help_text="ID of last sent intraday GEMI company")
    stripe_customer_id = models.CharField(max_length=100, blank=True)
    stripe_subscription_id = models.CharField(max_length=100, blank=True)
    # Server-side reference to a Stripe Subscription Schedule managing a future plan change
    # (Phase 5b+). Blank, not null, for consistency with stripe_customer_id/stripe_subscription_id
    # above: "no schedule yet" is already expressed the same way every other Stripe id on this
    # model expresses "not linked yet," so every existing `if sub.stripe_..._id:` style check
    # keeps working the same way for this field too. Never set from client input.
    stripe_schedule_id = models.CharField(max_length=100, blank=True)
    # Tiers a Stripe Subscription Schedule can target as a future phase. "custom" has no Stripe
    # price id (quote-based) and "free" means no subscription at all -- neither is ever a
    # *scheduled tier change*, only a genuinely priced plan is.
    scheduled_tier = models.CharField(
        max_length=20,
        choices=[choice for choice in TIERS if choice[0] in ("pro", "business", "enterprise")],
        # null=True (not just blank=True) is deliberate here, unlike the CharFields above: None
        # is the explicit "no scheduled change" sentinel this field is specified to have, kept
        # distinct from any tier string -- including one that could otherwise collide with a
        # real tier value the way an empty string would not for a *tier* field.
        null=True, blank=True, default=None,
    )
    # Projection of the Stripe schedule's next phase start, kept only for UX ("Business μέχρι
    # X, μετά Pro"). Read-only cache: nothing in this codebase may branch entitlement or
    # authorization on this value -- see has_entitlement/effective_tier below, which never
    # reference it, and gemiapp/tests.py::ScheduledTierProjectionTests which pins that down.
    scheduled_change_at = models.DateTimeField(null=True, blank=True)
    # Known future date when the CURRENT entitlement will end because a termination is already
    # scheduled at Stripe -- i.e. cancel_at_period_end=True, not a plan change. None for a
    # normal recurring subscription with nothing scheduled to end: current_period_end is a
    # renewal boundary, not an expiry, so it must never be written here. A scheduled downgrade
    # (Business -> Pro) does NOT set this field either -- entitlement continues past that date,
    # it just changes tier; that case is expressed by scheduled_tier/scheduled_change_at above,
    # not by active_until. Wiring the webhook to actually populate this is Phase 5c+; this
    # field's column already existed before Phase 5, only this semantics is new/authoritative
    # as of Phase 5a.
    active_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    ALLOWED_PAID_STATUSES = ("active",)

    def clean(self):
        super().clean()
        # Soft, application-level invariant only (not a DB constraint, so it costs nothing in
        # portability and doesn't run on every plain .save() the way the rest of this codebase
        # already calls it -- Django never calls clean()/full_clean() automatically on save(),
        # and nothing here changes that). A schedule reference or a projected change time with
        # no scheduled_tier to go with it is inconsistent data, not a valid "no schedule" state.
        if self.scheduled_tier is None and (self.scheduled_change_at or self.stripe_schedule_id):
            raise ValidationError(
                "scheduled_change_at and stripe_schedule_id must be empty when scheduled_tier is None."
            )

    @property
    def has_active_paid_subscription(self):
        return self.tier in ("pro", "business", "enterprise", "custom") and self.status in self.ALLOWED_PAID_STATUSES

    @property
    def has_valid_complimentary_access(self):
        if self.complimentary_tier not in ("pro", "business", "enterprise", "custom"):
            return False
        if self.complimentary_until and self.complimentary_until < timezone.now():
            return False
        return True

    @property
    def effective_tier(self):
        if self.has_active_paid_subscription:
            return self.tier
        if self.has_valid_complimentary_access:
            return self.complimentary_tier
        return "free"

    @property
    def has_entitlement(self):
        return self.has_active_paid_subscription or self.has_valid_complimentary_access

    @property
    def radar_limit(self):
        if self.custom_radar_limit is not None and self.custom_radar_limit > 0:
            return self.custom_radar_limit
        return RADAR_LIMITS.get(self.effective_tier, 0)

    def __str__(self):
        return f"{self.user.username} - {self.get_tier_display()} ({self.status})"


class PersonSuppression(models.Model):
    """A person who exercised their right to object under Article 21 GDPR.

    Kept in its own table rather than in Company.raw_data, because the importer
    overwrites raw_data wholesale on every run and the suppression must outlive that.

    Matching is on the accent-folded, uppercased name. ΓΕΜΗ publishes no stable
    identifier for individuals, so the name is all there is to match on. That makes the
    match deliberately broad: an objection is about a person, and 559 people in the
    current data appear in more than one company (one in 22), so a per-company flag
    would leave the other entries visible and the objection unhonoured. A company can
    still be named to scope the suppression when the request is only about one role.
    """

    full_name = models.CharField("Ονοματεπώνυμο", max_length=300)
    normalized_name = models.CharField(max_length=300, editable=False, db_index=True)
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, null=True, blank=True, related_name="person_suppressions",
        verbose_name="Μόνο για την επιχείρηση", help_text="Κενό = απόκρυψη σε όλες τις επιχειρήσεις.",
    )
    reason = models.TextField("Σημείωση", blank=True, help_text="Εσωτερική τεκμηρίωση του αιτήματος.")
    requested_at = models.DateTimeField("Ημερομηνία αιτήματος", default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Εναντίωση προσώπου"
        verbose_name_plural = "Εναντιώσεις προσώπων (άρθρο 21 ΓΚΠΔ)"
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(fields=["normalized_name", "company"], name="unique_person_suppression"),
            models.UniqueConstraint(
                fields=["normalized_name"], condition=models.Q(company__isnull=True),
                name="unique_global_person_suppression",
            ),
        ]

    def save(self, *args, **kwargs):
        from .kad import normalize_kad_search

        normalized = normalize_kad_search(self.full_name)
        if normalized != self.normalized_name:
            self.normalized_name = normalized
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "normalized_name" not in update_fields:
                kwargs["update_fields"] = list(update_fields) + ["normalized_name"]
        super().save(*args, **kwargs)

    def __str__(self):
        scope = self.company.name if self.company_id else "όλες τις επιχειρήσεις"
        return f"{self.full_name} — {scope}"

    @classmethod
    def names_for(cls, company):
        """Normalised names to hide for this company: global objections plus its own.

        An unsaved Company cannot be the target of a scoped objection and cannot be used
        in a related filter, so only the global rows apply to it.
        """
        scope = models.Q(company__isnull=True)
        if getattr(company, "pk", None) is not None:
            scope |= models.Q(company=company)
        return set(cls.objects.filter(scope).values_list("normalized_name", flat=True))


class AdminAuditLog(models.Model):
    admin_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="admin_audit_logs")
    action = models.CharField(max_length=100, db_index=True)
    target_type = models.CharField(max_length=50, blank=True)
    target_id = models.CharField(max_length=100, blank=True)
    target_repr = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"

    def __str__(self):
        return f"[{self.timestamp.strftime('%Y-%m-%d %H:%M')}] {self.admin_user} -> {self.action} ({self.target_repr})"


class StripeWebhookEvent(models.Model):
    """Durable record of every Stripe webhook delivery, keyed by Stripe's own event id.

    Stripe guarantees at-least-once delivery, never exactly-once, so this table is what makes
    webhook processing idempotent: a row is written with status="received" before any business
    logic runs, and a duplicate delivery of the same event id is recognised and skipped by
    looking this table up first. Stripe remains the source of truth for billing state;
    UserSubscription stays a projection kept in sync by the processing this table gates.
    """

    STATUS_CHOICES = [
        ("received", "Received"),
        ("processed", "Processed"),
        ("failed", "Failed"),
        ("ignored", "Ignored"),
    ]

    stripe_event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=255, db_index=True)
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="received", db_index=True)
    error_message = models.TextField(blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    # Set whenever we finish an attempt to handle this event, regardless of outcome: on success
    # (processed), on a recognised-but-unhandled type (ignored), and on a caught exception
    # (failed) alike. Stays null only while status="received" -- i.e. no attempt has finished
    # yet, either because processing is still in flight or because a previous attempt crashed
    # before reaching any of the three terminal states.
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]
        verbose_name = "Stripe Webhook Event"
        verbose_name_plural = "Stripe Webhook Events"

    def __str__(self):
        return f"{self.stripe_event_id} ({self.event_type}) — {self.status}"


from django.db.models.signals import post_save
from django.dispatch import receiver

@receiver(post_save, sender=User)
def create_user_subscription(sender, instance, created, **kwargs):
    if created:
        UserSubscription.objects.get_or_create(user=instance)
        # send_digests iterates DigestPreference, so an account without one is silently
        # unreachable no matter which tier it is on.
        DigestPreference.objects.get_or_create(user=instance)
