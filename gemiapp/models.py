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

    # --- Gemi Leads 2.0 canonical metadata (A6) ----------------------------------------------------
    # Additive and not read by any current code path: the description fields above still drive the
    # importer, Radars, digests, search and exports. Filled from raw_data by
    # `manage.py backfill_gemi_company_metadata` and, later, by canonical ingestion
    # (gemiapp.ingestion.company_metadata documents the semantics and evidence).
    # Plain GEMI source identifiers, deliberately not foreign keys to the reference tables: a company
    # may carry an id the local reference data does not know yet.
    status_source_id = models.CharField(max_length=32, null=True, blank=True, editable=False)
    legal_type_source_id = models.CharField(max_length=32, null=True, blank=True, editable=False)
    gemi_office_source_id = models.CharField(max_length=32, null=True, blank=True, editable=False)
    prefecture_source_id = models.CharField(max_length=32, null=True, blank=True, editable=False)
    municipality_source_id = models.CharField(max_length=32, null=True, blank=True, editable=False)
    # Quality of the *source* incorporationDate under the A3 DatePolicy; null = not assessed.
    # incorporation_date itself is untouched and may hold a legacy clamped value.
    INCORPORATION_DATE_QUALITIES = [
        ("valid", "Valid"), ("missing", "Missing"), ("invalid", "Invalid"), ("out_of_range", "Out of range"),
    ]
    incorporation_date_quality = models.CharField(
        max_length=16, choices=INCORPORATION_DATE_QUALITIES, null=True, blank=True, editable=False,
    )
    # Earliest time Gemi Leads can establish it observed this company in a GEMI response.
    first_seen_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Most recent time Gemi Leads observed this company in a valid GEMI response.
    last_seen_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Most recent time the canonical GEMI state was synchronised into the structured fields. Null until
    # canonical ingestion exists; the backfill never sets it.
    last_synced_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-incorporation_date", "-gemi_number"]
        verbose_name_plural = "Επιχειρήσεις"
        indexes = [
            # Matches the default ordering exactly. The per-column indexes on
            # incorporation_date and gemi_number don't compose for a two-key sort, so every
            # unfiltered or date-filtered listing (home's [:6], the dashboard paginator) was
            # doing a full sort. This makes those an index scan.
            models.Index(
                fields=["-incorporation_date", "-gemi_number"],
                name="company_recent_order_idx",
            ),
        ]

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
    """One company activity (KAD).

    ``code``, ``description`` and ``activity_type`` (the type exactly as published) are the columns the
    application has always read. The Gemi Leads 2.0 columns (A7) carry canonical GEMI metadata;
    gemiapp.ingestion.activities documents their semantics, the row identity and how everything
    customer-visible keeps its legacy behaviour.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="activity_records")
    code = models.CharField("ΚΑΔ", max_length=16, db_index=True)
    description = models.CharField("Περιγραφή", max_length=1000, blank=True)
    activity_type = models.CharField("Τύπος", max_length=80, blank=True)

    # --- Gemi Leads 2.0 canonical activity metadata (A7) -------------------------------------------
    ACTIVITY_TYPES = [
        ("primary", "Κύρια"), ("secondary", "Δευτερεύουσα"), ("auxiliary", "Βοηθητική"), ("other", "Λοιπή"),
        ("unknown", "Μη αναγνωρισμένος τύπος"),
    ]
    # Null when the entry has no type or the row has not been reconciled; "unknown" for a published type
    # this version does not recognise (still kept as published in activity_type).
    activity_type_normalized = models.CharField(max_length=16, choices=ACTIVITY_TYPES, null=True, blank=True, editable=False)
    # As published ("kad_2008" / "kad_2026"); never inferred from the code.
    kad_version = models.CharField(max_length=32, null=True, blank=True, editable=False)
    # dtFrom / dtTo under the A3 DatePolicy: the date only when VALID, the quality says why it is absent.
    date_from = models.DateField(null=True, blank=True, editable=False)
    date_from_quality = models.CharField(max_length=16, choices=Company.INCORPORATION_DATE_QUALITIES, null=True, blank=True, editable=False)
    date_to = models.DateField(null=True, blank=True, editable=False)
    date_to_quality = models.CharField(max_length=16, choices=Company.INCORPORATION_DATE_QUALITIES, null=True, blank=True, editable=False)
    # A3 currentness evaluated on current_as_of, the day of the GEMI observation it was derived from.
    is_current = models.BooleanField(null=True, blank=True, editable=False)
    current_as_of = models.DateField(null=True, blank=True, editable=False)
    # Canonical identity within the company; null = legacy row not reconciled with a GEMI record.
    source_key = models.CharField(max_length=64, null=True, blank=True, editable=False)
    # Whether the activity was in the company's latest reconciled GEMI observation.
    in_latest_source = models.BooleanField(null=True, blank=True, editable=False)
    # Whether the legacy importer would hold this row for the latest observation (one per code and type).
    # Every legacy read uses only these rows. Every pre-A7 row is one, hence the default.
    legacy_listed = models.BooleanField(default=True, editable=False)

    class Meta:
        ordering = ["company_id", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code", "activity_type"], condition=models.Q(legacy_listed=True),
                name="unique_company_activity_listed",
            ),
            models.UniqueConstraint(
                fields=["company", "source_key"], condition=models.Q(source_key__isnull=False),
                name="unique_company_activity_source_key",
            ),
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
    FREQUENCIES = [("daily", "Καθημερινά"), ("intraday", "3-ωρο / Priority Alerts"), ("manual_yesterday", "Χειροκίνητο (Χθες)"), ("weekly", "Εβδομαδιαία (καταργήθηκε)")]
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
    TIERS = [("free", "Free (Legacy)"), ("pro", "Pro"), ("business", "Business"), ("enterprise", "Enterprise / Priority Alerts"), ("custom", "Custom / Enterprise")]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="subscription")
    tier = models.CharField(max_length=20, choices=TIERS, default="free")
    status = models.CharField(max_length=30, default="inactive")
    complimentary_tier = models.CharField(max_length=20, choices=[("none", "None"), ("pro", "Pro"), ("business", "Business"), ("enterprise", "Enterprise / Priority Alerts"), ("custom", "Custom / Enterprise")], default="none")
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
        """How many Radars this account may run -- and, because every paid feature gates on
        radar_limit > 0, whether it may use paid functionality at all (Radar create / edit /
        activate / preview and all four CSV exports).

        Entitlement is decided first. custom_radar_limit is a CAPACITY OVERRIDE set by the
        Superadmin, not an entitlement grant: it only changes the size of an allowance the
        account already has. Without this ordering a cancelled, lapsed or expired account with a
        stored custom limit kept full paid access, since the tier default already returns 0 for
        an unentitled account but the custom branch returned before entitlement was consulted.

        The stored value is deliberately left in place when entitlement lapses, so it takes
        effect again as soon as entitlement is restored.
        """
        if not self.has_entitlement:
            return 0
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


class CompanyOutreach(models.Model):
    """One prospecting email sent (or attempted) to a newly registered company.

    Used by the Superadmin "Εύρεση Πελατών" tool to remember which companies have
    already been contacted, so the same business is never emailed twice. One row per
    company — the tool only ever sends a single introductory email.
    """

    STATUSES = [
        ("pending", "Σε ουρά"),
        # Claimed by a worker for the duration of one SMTP round-trip, so a second cluster
        # cannot pick the same row up and email the company twice. A row stuck here means the
        # worker died mid-send; requeue_dropped_outreach flips it back to "pending".
        ("sending", "Αποστέλλεται"),
        ("sent", "Εστάλη"),
        ("failed", "Απέτυχε"),
        ("cancelled", "Ακυρώθηκε"),
    ]

    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name="outreach")
    status = models.CharField(max_length=10, choices=STATUSES, default="sent")
    sent_to = models.EmailField(blank=True)
    error_message = models.TextField(blank=True)
    sent_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="company_outreach")
    created_at = models.DateTimeField(auto_now_add=True)
    # When the email actually went out. created_at is the row's birth (it can sit "pending" for
    # a day, or be flipped back to "pending" by requeue_dropped_outreach long after creation),
    # so the daily-cap accounting keys on this instead. Null while pending/failed.
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Επικοινωνία με επιχείρηση"
        verbose_name_plural = "Επικοινωνίες με επιχειρήσεις"

    def __str__(self):
        return f"{self.company.name} → {self.sent_to} ({self.status})"


class OutreachSuppression(models.Model):
    """An email address that asked not to receive prospecting emails.

    Populated by the unsubscribe link in the "Εύρεση Πελατών" outreach email. Matching
    is on the lower-cased, stripped address, so a company is never emailed again through
    this tool once anyone at that address opts out.
    """

    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Απεγγραφή outreach"
        verbose_name_plural = "Απεγγραφές outreach"

    def __str__(self):
        return self.email

    @staticmethod
    def normalize(email):
        return (email or "").strip().lower()

    @classmethod
    def is_suppressed(cls, email):
        return cls.objects.filter(email=cls.normalize(email)).exists()


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


class EmailEngagementEvent(models.Model):
    """Durable, append-only log of Brevo delivery/engagement events (delivered, opened, click,
    unsubscribed, hardBounce, softBounce, blocked, spam, ...) for digest emails.

    Unlike StripeWebhookEvent this is never idempotency-critical: a duplicate "opened" event
    recorded twice has no real-money consequence, so every delivery is simply appended rather
    than deduplicated against a stored event id (Brevo's own webhook docs don't guarantee one is
    always present or stable across retries).

    `tag` is the value we set on `X-Mailin-Tag` at send time (see
    `services._send_digest_email`), formatted as "digest:<user_id>:<digest_date>:<frequency>" --
    it is how an incoming event is matched back to the DigestDelivery row it's about. Brevo
    echoes the tag back verbatim in the "tag" (marketing) or "tags" (transactional, a list)
    field of every webhook payload for a message that was sent with it.
    """

    event_type = models.CharField(max_length=50, db_index=True)
    email = models.EmailField()
    tag = models.CharField(max_length=255, blank=True, db_index=True)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.stripe_event_id} ({self.event_type}) — {self.status}"


class GemiSourceRecord(models.Model):
    """Minimised provenance of one successful, validated ΓΕΜΗ API response.

    Written by gemiapp.ingestion.source_records (only while GEMI_SOURCE_RECORDS_ENABLED is on).
    System data, not tenant data: one search response can serve many customers' Radars, so there is
    no user or organisation link. Holds request metadata, a SHA-256 of the full response and,
    optionally, a sanitised company-level payload -- never the raw response, natural-person data,
    contact data, the API key or request headers. Every row expires by retention class and is removed
    by the purge_gemi_source_records command.
    """

    class Source(models.TextChoices):
        GEMI_OPENDATA = "gemi_opendata", "ΓΕΜΗ Open Data API"

    class Family(models.TextChoices):
        COMPANY_SEARCH = "company_search", "Company search"
        COMPANY_DETAIL = "company_detail", "Company detail"
        REFERENCE_DATA = "reference_data", "Reference data"
        DOCUMENT_METADATA = "document_metadata", "Document metadata"

    class Retention(models.TextChoices):
        SHORT = "short", "Short"
        STANDARD = "standard", "Standard"
        AUDIT = "audit", "Audit"

    source = models.CharField(max_length=32, choices=Source.choices, default=Source.GEMI_OPENDATA)
    family = models.CharField(max_length=32, choices=Family.choices)
    # The A2 response contract the payload was validated against, e.g. "metadata_prefectures".
    response_family = models.CharField(max_length=48)
    endpoint = models.CharField(max_length=255)
    request_params = models.JSONField(default=dict, blank=True)
    # SHA-256 of endpoint + canonical parameters: every observation of the same request shares it.
    request_fingerprint = models.CharField(max_length=64)
    # SHA-256 of source, response family, request, payload hash and observation window. Unique, so a
    # retried request inside one window cannot insert the same observation twice.
    observation_key = models.CharField(max_length=64, unique=True)
    fetched_at = models.DateTimeField()
    http_status = models.PositiveSmallIntegerField()
    gateway_request_id = models.CharField(max_length=80, blank=True)
    payload_hash = models.CharField(max_length=64)
    result_count = models.PositiveIntegerField(null=True, blank=True)
    response_schema_version = models.PositiveSmallIntegerField()
    # Set only when the stored payload was produced by the normaliser.
    normalizer_version = models.PositiveSmallIntegerField(null=True, blank=True)
    record_format_version = models.PositiveSmallIntegerField()
    sanitised_payload = models.JSONField(null=True, blank=True)
    retention_class = models.CharField(max_length=16, choices=Retention.choices)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fetched_at"]
        verbose_name = "GEMI source record"
        verbose_name_plural = "GEMI source records"
        indexes = [
            # "Latest observation of this request" and "did its payload change?" -- change detection.
            models.Index(fields=["request_fingerprint", "-fetched_at"], name="gemisrc_request_fetched_idx"),
            # Inspection by family, newest first.
            models.Index(fields=["family", "-fetched_at"], name="gemisrc_family_fetched_idx"),
            # The purge query.
            models.Index(fields=["expires_at"], name="gemisrc_expires_idx"),
        ]

    def __str__(self):
        return f"{self.family} {self.endpoint} @ {self.fetched_at:%Y-%m-%d %H:%M} ({self.payload_hash[:12]})"


class GemiReferenceEntry(models.Model):
    """Fields shared by the local GEMI reference tables (filled by gemiapp.ingestion.reference_data).

    Each concrete table is a canonical copy of one GEMI metadata endpoint, written only by
    sync_gemi_reference_data. These tables are new and separate from what the application uses today
    (ActivityCode, the description strings on Company and CustomerRadar); nothing reads them yet.

    Rows are never deleted when an item disappears from the source: ``is_present`` becomes False and
    ``retired_at`` is set, so historical company records can still resolve old values, and a returning
    item is revived in place. ``source_is_active`` is what the source says, when it says anything;
    ``is_present`` is whether the item was in the latest successful sync -- two different facts.
    """

    source_id = models.CharField(max_length=32)
    description = models.TextField(null=True, blank=True)
    description_en = models.TextField(null=True, blank=True)
    source_is_active = models.BooleanField(null=True, blank=True)
    # As published (e.g. "2026-02-25 15:56:01"). Kept as text: the source does not state its timezone.
    source_last_updated = models.CharField(max_length=32, null=True, blank=True)
    is_present = models.BooleanField(default=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["source_id"]

    def __str__(self):
        return f"{self.source_id} — {self.description}" if self.description else self.source_id


class GemiKad(GemiReferenceEntry):
    """GET /metadata/activities. A code is unique only together with its KAD version: the same code in
    KAD 2008 and KAD 2026 is two rows, never merged. Not the ActivityCode catalogue the app uses today."""

    kad_version = models.CharField(max_length=16, blank=True)  # "kad_2008" / "kad_2026"; "" when unpublished

    class Meta(GemiReferenceEntry.Meta):
        ordering = ["source_id", "kad_version"]
        verbose_name = "GEMI KAD (reference)"
        verbose_name_plural = "GEMI KAD (reference)"
        constraints = [models.UniqueConstraint(fields=["source_id", "kad_version"], name="unique_gemi_kad_code_version")]
        indexes = [models.Index(fields=["kad_version", "is_present"], name="gemikad_version_present_idx")]


class ActivityCodeKadLink(models.Model):
    """A link between a live ActivityCode entry and a GEMI reference KAD row with exactly the same code digits
    (A8). One entry may link to both a KAD 2008 and a KAD 2026 row, so upstream identities never collapse.

    Derived data, written only by reconcile_gemi_kad_catalogue; ActivityCode itself is unchanged, and nothing
    customer-facing reads these links while GEMI_KAD_PICKER_CURRENT_TAXONOMY_ONLY is off. See
    gemiapp.ingestion.kad_catalogue.
    """

    MATCH_BASES = [("code", "Ίδιος κωδικός ΚΑΔ")]

    activity_code = models.ForeignKey(ActivityCode, on_delete=models.CASCADE, related_name="kad_links")
    gemi_kad = models.ForeignKey(GemiKad, on_delete=models.CASCADE, related_name="activity_code_links")
    match_basis = models.CharField(max_length=16, choices=MATCH_BASES)
    # Evidence only, never identity: whether the descriptions agree (case-, accent- and whitespace-insensitive).
    description_matches = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["activity_code_id", "gemi_kad_id"]
        verbose_name = "ActivityCode ↔ GEMI KAD link"
        verbose_name_plural = "ActivityCode ↔ GEMI KAD links"
        constraints = [models.UniqueConstraint(fields=["activity_code", "gemi_kad"], name="unique_activity_code_kad_link")]


class GemiDiscoveryRun(models.Model):
    """One Discovery v2 run (A10): what it fetched, found and decided. Counts and ids only, no payload.

    Written by gemiapp.ingestion.discovery, which documents the paging, the overlap stop condition and the
    ordering guardrails. Shadow runs never create or change Company rows.
    """

    MODES = [("shadow", "Shadow"), ("ingest", "Ingest"), ("bootstrap", "Bootstrap")]
    STATUSES = [
        ("running", "Σε εξέλιξη"), ("success", "Επιτυχία"), ("incomplete", "Ημιτελής"),
        ("anomaly", "Ανωμαλία"), ("failed", "Αποτυχία"),
    ]

    stream = models.CharField(max_length=32)
    mode = models.CharField(max_length=16, choices=MODES)
    status = models.CharField(max_length=16, choices=STATUSES, default="running")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    pages_fetched = models.PositiveIntegerField(default=0)
    records_examined = models.PositiveIntegerField(default=0)
    known_records = models.PositiveIntegerField(default=0)
    new_records = models.PositiveIntegerField(default=0)
    late_publication_records = models.PositiveIntegerField(default=0)
    invalid_date_records = models.PositiveIntegerField(default=0)
    duplicate_records = models.PositiveIntegerField(default=0)
    invalid_identifier_records = models.PositiveIntegerField(default=0)
    ingested_records = models.PositiveIntegerField(default=0)
    # Highest identifier seen in this run, and the cursor before and after it.
    highest_gemi_number = models.CharField(max_length=24, blank=True)
    previous_high_water_mark = models.CharField(max_length=24, blank=True)
    resulting_high_water_mark = models.CharField(max_length=24, blank=True)
    cursor_advanced = models.BooleanField(default=False)
    overlap_known_records = models.PositiveIntegerField(default=0)
    stop_reason = models.CharField(max_length=32, blank=True)
    anomalies = models.JSONField(default=list, blank=True)
    policy = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "GEMI discovery run"
        indexes = [models.Index(fields=["stream", "-started_at"], name="discovery_run_stream_idx")]

    def __str__(self):
        return f"{self.stream} · {self.mode} · {self.status}"


class GemiDiscoveryCursor(models.Model):
    """Discovery v2 state for one stream (A10): system pipeline state, never billing state.

    ``high_water_mark`` is the highest GEMI number already observed and trusted -- a discovery cursor, not
    a business timestamp. A higher GEMI number is not proof of a later incorporation date. It advances only
    after a run finishes with its stop condition met and no blocking anomaly.
    """

    STATUSES = [("uninitialised", "Χωρίς αρχικοποίηση"), ("ready", "Έτοιμο"), ("anomaly", "Ανωμαλία")]
    STREAM_COMPANIES = "companies_by_gemi_number"

    stream = models.CharField(max_length=32, unique=True)
    status = models.CharField(max_length=16, choices=STATUSES, default="uninitialised")
    high_water_mark = models.CharField(max_length=24, blank=True)
    # The same value as an integer, for ordering comparisons.
    high_water_mark_value = models.BigIntegerField(null=True, blank=True)
    bootstrap_method = models.CharField(max_length=32, blank=True)
    bootstrapped_at = models.DateTimeField(null=True, blank=True)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    anomaly_reason = models.CharField(max_length=64, blank=True)
    anomaly_detected_at = models.DateTimeField(null=True, blank=True)
    last_run = models.ForeignKey(GemiDiscoveryRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    last_statistics = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "GEMI discovery cursor"

    def __str__(self):
        return f"{self.stream} · {self.status} · {self.high_water_mark or 'no frontier'}"


class GemiDiscoveryObservation(models.Model):
    """One newly discovered company in a run: identifier, dates and classification. No payload, no PII."""

    CLASSIFICATIONS = [
        ("new_incorporation", "Νέα σύσταση"), ("late_publication", "Καθυστερημένη δημοσίευση"),
        ("invalid_date", "Μη έγκυρη ημερομηνία"),
        # Seen during the scan and already stored locally: the evidence that tells "Discovery v2 saw this
        # company" apart from "Discovery v2 never reached it" in the legacy comparison.
        ("known", "Ήδη γνωστή"),
    ]

    run = models.ForeignKey(GemiDiscoveryRun, on_delete=models.CASCADE, related_name="observations")
    gemi_number = models.CharField(max_length=24, db_index=True)
    classification = models.CharField(max_length=24, choices=CLASSIFICATIONS)
    # The source incorporation date and its A3 quality; the date is stored only when VALID.
    incorporation_date = models.DateField(null=True, blank=True)
    incorporation_date_quality = models.CharField(max_length=16, choices=Company.INCORPORATION_DATE_QUALITIES)
    company_existed = models.BooleanField()
    page_index = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["run_id", "id"]
        constraints = [models.UniqueConstraint(fields=["run", "gemi_number"], name="unique_discovery_observation")]
        indexes = [models.Index(fields=["classification"], name="discovery_classification_idx")]

    def __str__(self):
        return f"{self.gemi_number} · {self.classification}"


class CompanyMonitoring(models.Model):
    """Monitoring state of one company, shared by every customer (A9).

    Written by gemiapp.ingestion.monitoring, which documents the reasons, priority, cadence and decay.
    Nothing reads it yet and nothing refreshes companies from it: the collector state
    (last_checked_at, last_success_at, last_failure_at, consecutive_failures) belongs to a future
    refresh collector and is never written by the recompute. No personal or contact data.
    """

    STATES = [("active", "Active"), ("decaying", "Decaying"), ("inactive", "Inactive")]
    PRIORITIES = [("critical", "Critical"), ("high", "High"), ("normal", "Normal"), ("low", "Low")]
    REASONS = [
        ("new_company", "New company"), ("active_radar_match", "Active Radar match"),
        ("active_opportunity", "Active opportunity"), ("recent_signal", "Recent signal"), ("manual", "Manual"),
    ]

    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name="monitoring")
    state = models.CharField(max_length=16, choices=STATES)
    # Null only while inactive. Derived from the active reasons by the central policy.
    priority = models.CharField(max_length=16, choices=PRIORITIES, null=True, blank=True)
    # The strongest active reason, for explainability; null while decaying or inactive.
    primary_reason = models.CharField(max_length=32, choices=REASONS, null=True, blank=True)
    policy_version = models.PositiveSmallIntegerField()
    # Start of the current continuous monitored period (active or decaying).
    monitored_since = models.DateTimeField()
    decay_started_at = models.DateTimeField(null=True, blank=True)
    inactive_since = models.DateTimeField(null=True, blank=True)
    # Eligibility for the next refresh, not a promise that it runs then. Null only while inactive.
    next_check_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["company_id"]
        verbose_name = "Company monitoring"
        verbose_name_plural = "Company monitoring"
        indexes = [models.Index(fields=["state", "next_check_at"], name="monitoring_due_idx")]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(state="inactive", priority__isnull=True, next_check_at__isnull=True)
                    | models.Q(state__in=["active", "decaying"], priority__isnull=False, next_check_at__isnull=False)
                ),
                name="monitoring_state_consistent",
            ),
        ]

    def __str__(self):
        return f"monitoring #{self.company_id} · {self.state}"


class CompanyMonitoringReason(models.Model):
    """One reason a company is (or was) monitored, with its activation history (A9). One row per
    monitoring state and reason; a reason is reactivated in place, never duplicated."""

    monitoring = models.ForeignKey(CompanyMonitoring, on_delete=models.CASCADE, related_name="reasons")
    reason = models.CharField(max_length=32, choices=CompanyMonitoring.REASONS)
    active = models.BooleanField()
    first_active_at = models.DateTimeField()
    # Start of the current (or most recent) active period.
    activated_at = models.DateTimeField()
    deactivated_at = models.DateTimeField(null=True, blank=True)
    activation_count = models.PositiveIntegerField()
    # For temporary reasons (a new company's window, a time-limited manual request).
    expires_at = models.DateTimeField(null=True, blank=True)
    # Ids of the underlying objects, e.g. the matching Radars. Ids only.
    source_ids = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["monitoring_id", "reason"]
        indexes = [models.Index(fields=["reason", "active"], name="monitoring_reason_active_idx")]
        constraints = [
            models.UniqueConstraint(fields=["monitoring", "reason"], name="unique_monitoring_reason"),
            models.CheckConstraint(
                condition=models.Q(active=True, deactivated_at__isnull=True) | models.Q(active=False, deactivated_at__isnull=False),
                name="monitoring_reason_active_consistent",
            ),
        ]

    def __str__(self):
        return f"monitoring #{self.monitoring_id} · {self.reason} · {'active' if self.active else 'inactive'}"


class GemiPrefecture(GemiReferenceEntry):
    """GET /metadata/prefectures."""

    class Meta(GemiReferenceEntry.Meta):
        verbose_name = "GEMI prefecture (reference)"
        verbose_name_plural = "GEMI prefectures (reference)"
        constraints = [models.UniqueConstraint(fields=["source_id"], name="unique_gemi_prefecture_source")]


class GemiMunicipality(GemiReferenceEntry):
    """GET /metadata/municipalities.

    ``source_prefecture_id`` is the upstream ``prefectureId`` kept as a plain identifier, deliberately
    not a foreign key: upstream municipalities only reference prefectures 0-51 while the Attica
    sub-units 52-55 exist separately, so the hierarchy is not a clean relation. A later geographic
    mapping layer resolves Attica.
    """

    source_prefecture_id = models.CharField(max_length=32, null=True, blank=True)

    class Meta(GemiReferenceEntry.Meta):
        verbose_name = "GEMI municipality (reference)"
        verbose_name_plural = "GEMI municipalities (reference)"
        constraints = [models.UniqueConstraint(fields=["source_id"], name="unique_gemi_municipality_source")]


class GemiCompanyStatus(GemiReferenceEntry):
    """GET /metadata/companyStatuses. ``source_is_active`` is the source's own active/inactive meaning
    of each status -- what a later task needs to stop treating deleted companies as active."""

    class Meta(GemiReferenceEntry.Meta):
        verbose_name = "GEMI company status (reference)"
        verbose_name_plural = "GEMI company statuses (reference)"
        constraints = [models.UniqueConstraint(fields=["source_id"], name="unique_gemi_status_source")]


class GemiLegalType(GemiReferenceEntry):
    """GET /metadata/legalTypes."""

    class Meta(GemiReferenceEntry.Meta):
        verbose_name = "GEMI legal type (reference)"
        verbose_name_plural = "GEMI legal types (reference)"
        constraints = [models.UniqueConstraint(fields=["source_id"], name="unique_gemi_legal_type_source")]


class GemiOffice(GemiReferenceEntry):
    """GET /metadata/gemiOffices. Identity and descriptions only: the endpoint's address, phone, fax,
    email and url are not stored."""

    class Meta(GemiReferenceEntry.Meta):
        verbose_name = "GEMI office (reference)"
        verbose_name_plural = "GEMI offices (reference)"
        constraints = [models.UniqueConstraint(fields=["source_id"], name="unique_gemi_office_source")]


class GemiDecisionSubject(GemiReferenceEntry):
    """GET /metadata/assemblySubjects: the coded subjects of GEMI announcements."""

    class Meta(GemiReferenceEntry.Meta):
        verbose_name = "GEMI decision subject (reference)"
        verbose_name_plural = "GEMI decision subjects (reference)"
        constraints = [models.UniqueConstraint(fields=["source_id"], name="unique_gemi_decision_subject_source")]


class GemiReferenceSyncRun(models.Model):
    """One non-dry-run execution of sync_gemi_reference_data. Counts and anomaly notes only, no payload."""

    STATUSES = [("running", "Σε εξέλιξη"), ("success", "Επιτυχία"), ("failed", "Αποτυχία")]

    status = models.CharField(max_length=12, choices=STATUSES, default="running")
    families = models.JSONField(default=list, blank=True)
    # {family: {fetched, created, updated, reappeared, unchanged, retired, conflicting_duplicates, retirement_skipped}}
    counts = models.JSONField(default=dict, blank=True)
    anomalies = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "GEMI reference sync run"
        verbose_name_plural = "GEMI reference sync runs"

    @property
    def duration_seconds(self):
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def __str__(self):
        return f"{self.started_at:%Y-%m-%d %H:%M} {self.status}"


from django.db.models.signals import post_save
from django.dispatch import receiver

@receiver(post_save, sender=User)
def create_user_subscription(sender, instance, created, **kwargs):
    if created:
        UserSubscription.objects.get_or_create(user=instance)
        # send_digests iterates DigestPreference, so an account without one is silently
        # unreachable no matter which tier it is on.
        DigestPreference.objects.get_or_create(user=instance)
