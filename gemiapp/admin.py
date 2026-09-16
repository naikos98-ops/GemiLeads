from django.contrib import admin
from .models import (
    ActivityCode,
    ActivityCodeKadLink,
    Company,
    CompanyActivity,
    CompanyMonitoring,
    CompanyMonitoringReason,
    CompanyOutreach,
    CompanySignal,
    CompanySignalDiscoveryEvidence,
    CompanySnapshot,
    CustomerRadar,
    DigestDelivery,
    DigestPreference,
    EmailEngagementEvent,
    GemiCompanyStatus,
    GemiDecisionSubject,
    GemiDiscoveryCursor,
    GemiDiscoveryObservation,
    GemiDiscoveryRun,
    GemiKad,
    GemiLegalType,
    GemiMunicipality,
    GemiOffice,
    GemiPrefecture,
    GemiRefreshRun,
    GemiReferenceSyncRun,
    GemiSourceRecord,
    ImportRun,
    OutreachSuppression,
    PersonSuppression,
    RadarMatch,
    StripeWebhookEvent,
    UserCompanyLead,
)


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "gemi_number", "incorporation_date", "legal_type", "prefecture", "is_active")
    list_filter = ("incorporation_date", "is_active", "legal_type", "prefecture")
    search_fields = ("name", "gemi_number", "vat_number")
    date_hierarchy = "incorporation_date"
    # Gemi Leads 2.0 metadata (A6), shown for inspection only; filled by backfill_gemi_company_metadata.
    readonly_fields = (
        "status_source_id", "legal_type_source_id", "gemi_office_source_id", "prefecture_source_id",
        "municipality_source_id", "incorporation_date_quality", "first_seen_at", "last_seen_at", "last_synced_at",
    )


@admin.register(CompanyOutreach)
class CompanyOutreachAdmin(admin.ModelAdmin):
    list_display = ("company", "status", "sent_to", "sent_by", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("company__name", "company__gemi_number", "sent_to")
    readonly_fields = ("created_at",)


@admin.register(OutreachSuppression)
class OutreachSuppressionAdmin(admin.ModelAdmin):
    list_display = ("email", "created_at")
    search_fields = ("email",)
    readonly_fields = ("created_at",)


admin.site.register(DigestPreference)
admin.site.register(DigestDelivery)
admin.site.register(ImportRun)


@admin.register(ActivityCode)
class ActivityCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "description")
    search_fields = ("code", "normalized_code", "description")


@admin.register(CompanyActivity)
class CompanyActivityAdmin(admin.ModelAdmin):
    list_display = ("company", "code", "activity_type")
    search_fields = ("company__name", "company__gemi_number", "code", "description")
    # Gemi Leads 2.0 canonical metadata (A7), shown for inspection only.
    readonly_fields = (
        "activity_type_normalized", "kad_version", "date_from", "date_from_quality", "date_to", "date_to_quality",
        "is_current", "current_as_of", "source_key", "in_latest_source", "legacy_listed",
    )


@admin.register(CustomerRadar)
class CustomerRadarAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "is_active", "frequency", "only_active", "monitor_from", "deleted_at")
    list_filter = ("is_active", "frequency", "only_active")
    search_fields = ("name", "user__email", "name_query")
    filter_horizontal = ("activity_codes",)


@admin.register(UserCompanyLead)
class UserCompanyLeadAdmin(admin.ModelAdmin):
    list_display = ("company", "user", "status", "is_favorite", "first_seen_at")
    list_filter = ("status", "is_favorite")
    search_fields = ("company__name", "company__gemi_number", "user__email")


@admin.register(RadarMatch)
class RadarMatchAdmin(admin.ModelAdmin):
    list_display = ("radar", "company", "matched_on", "import_run")
    list_filter = ("matched_on",)
    search_fields = ("radar__name", "company__name", "company__gemi_number", "radar__user__email")
    readonly_fields = ("matched_activity_codes", "match_reason", "created_at")


@admin.register(PersonSuppression)
class PersonSuppressionAdmin(admin.ModelAdmin):
    """Article 21 GDPR objections. Adding a row hides that person immediately.

    Requests carry a one-month deadline, so requested_at is recorded and shown first:
    it is the date the clock started, which is not the date the row was created.
    """

    list_display = ("full_name", "scope", "requested_at", "created_at")
    list_filter = ("requested_at",)
    search_fields = ("full_name", "normalized_name", "reason", "company__name")
    autocomplete_fields = ("company",)
    readonly_fields = ("normalized_name", "created_at")

    @admin.display(description="Εμβέλεια")
    def scope(self, obj):
        return obj.company.name if obj.company_id else "Όλες οι επιχειρήσεις"


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):
    """Inspection only. Rows are written exclusively by stripe_webhook; hand-editing one would
    misrepresent what actually happened during a Stripe delivery, so add/change are disabled."""

    list_display = ("stripe_event_id", "event_type", "status", "received_at", "processed_at")
    list_filter = ("status", "event_type")
    search_fields = ("stripe_event_id", "event_type")
    readonly_fields = ("stripe_event_id", "event_type", "payload", "status", "error_message", "received_at", "processed_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class GemiReferenceAdmin(admin.ModelAdmin):
    """Inspection only. Reference rows are written exclusively by sync_gemi_reference_data and hold no
    personal data; editing one by hand would misrepresent the source."""

    list_display = ("source_id", "description", "is_present", "source_is_active", "last_seen_at", "retired_at")
    list_filter = ("is_present", "source_is_active")
    search_fields = ("source_id", "description", "description_en")

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(GemiKad)
class GemiKadAdmin(GemiReferenceAdmin):
    list_display = ("source_id", "kad_version", "description", "is_present", "last_seen_at", "retired_at")
    list_filter = ("kad_version", "is_present")


@admin.register(GemiMunicipality)
class GemiMunicipalityAdmin(GemiReferenceAdmin):
    list_display = ("source_id", "description", "source_prefecture_id", "is_present", "last_seen_at", "retired_at")


admin.site.register([GemiPrefecture, GemiCompanyStatus, GemiLegalType, GemiOffice, GemiDecisionSubject], GemiReferenceAdmin)


@admin.register(ActivityCodeKadLink)
class ActivityCodeKadLinkAdmin(admin.ModelAdmin):
    """Inspection only. Links are derived by reconcile_gemi_kad_catalogue from ActivityCode and GemiKad."""

    list_display = ("activity_code", "gemi_kad", "match_basis", "description_matches", "updated_at")
    list_filter = ("description_matches", "gemi_kad__kad_version", "gemi_kad__is_present")
    list_select_related = ("activity_code", "gemi_kad")
    search_fields = ("activity_code__normalized_code", "gemi_kad__source_id")
    readonly_fields = ("activity_code", "gemi_kad", "match_basis", "description_matches", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class CompanyMonitoringReasonInline(admin.TabularInline):
    model = CompanyMonitoringReason
    extra = 0
    can_delete = False
    readonly_fields = ("reason", "active", "first_active_at", "activated_at", "deactivated_at", "activation_count", "expires_at", "source_ids")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(CompanyMonitoring)
class CompanyMonitoringAdmin(admin.ModelAdmin):
    """Inspection only. Written by recompute_gemi_company_monitoring (and, later, the refresh collector)."""

    list_display = ("company_id", "state", "priority", "primary_reason", "next_check_at", "last_checked_at")
    list_filter = ("state", "priority", "primary_reason")
    search_fields = ("company__gemi_number",)
    inlines = (CompanyMonitoringReasonInline,)
    readonly_fields = (
        "company", "state", "priority", "primary_reason", "policy_version", "monitored_since", "decay_started_at",
        "inactive_since", "next_check_at", "last_checked_at", "last_success_at", "last_failure_at",
        "consecutive_failures", "created_at", "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(GemiReferenceSyncRun)
class GemiReferenceSyncRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "status", "families", "finished_at")
    list_filter = ("status",)
    readonly_fields = ("status", "families", "counts", "anomalies", "error_message", "started_at", "finished_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(CompanySignal)
class CompanySignalAdmin(admin.ModelAdmin):
    """Inspection only. Signals are detected events: they are written by detectors (from B2) and must
    never be added or edited by hand, which would misrepresent what was observed."""

    list_display = ("gemi_number", "signal_type", "mode", "confidence", "effective", "detected_at", "rule_version", "source_type")
    list_filter = ("signal_type", "mode", "source_type", "effective_precision")
    list_select_related = ("company",)
    search_fields = ("company__gemi_number", "dedupe_key")
    date_hierarchy = "detected_at"
    readonly_fields = (
        "company", "signal_type", "source_type", "rule_version", "dedupe_key", "confidence", "mode",
        "effective_date", "effective_at", "effective_precision", "detected_at", "created_at", "updated_at",
    )

    @admin.display(description="Αρ. ΓΕΜΗ", ordering="company__gemi_number")
    def gemi_number(self, obj):
        return obj.company.gemi_number

    @admin.display(description="Χρόνος γεγονότος")
    def effective(self, obj):
        return obj.effective_at or obj.effective_date or "—"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(CompanySnapshot)
class CompanySnapshotAdmin(admin.ModelAdmin):
    """Inspection only. Snapshots are observed history written by the B3 writer; editing one by hand would
    misrepresent what the source said at that observation."""

    list_display = (
        "gemi_number", "observed_at", "last_observed_at", "is_baseline", "short_state_hash", "status_source_id",
        "legal_type_source_id", "municipality_source_id", "activity_count", "schema_version", "normalizer_version",
    )
    list_filter = ("is_baseline", "schema_version", "normalizer_version")
    list_select_related = ("company",)
    search_fields = ("company__gemi_number", "state_hash")
    date_hierarchy = "observed_at"
    readonly_fields = tuple(field.name for field in CompanySnapshot._meta.fields)

    @admin.display(description="Αρ. ΓΕΜΗ", ordering="company__gemi_number")
    def gemi_number(self, obj):
        return obj.company.gemi_number

    @admin.display(description="State hash")
    def short_state_hash(self, obj):
        return obj.state_hash[:12]

    @admin.display(description="Δραστηριότητες")
    def activity_count(self, obj):
        return len(obj.activities_state or [])

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(CompanySignalDiscoveryEvidence)
class CompanySignalDiscoveryEvidenceAdmin(admin.ModelAdmin):
    """Inspection only. Written by the NEW_COMPANY producer; the link to the first discovery observation
    is what makes a signal auditable and must never be edited by hand."""

    list_display = ("signal", "discovery_observation", "created_at")
    list_select_related = ("signal", "discovery_observation")
    search_fields = ("signal__company__gemi_number", "discovery_observation__gemi_number")
    readonly_fields = ("signal", "discovery_observation", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class DiscoveryReadOnlyAdmin(admin.ModelAdmin):
    """Inspection only. Discovery v2 state is written by run_gemi_discovery_v2 and its bootstrap."""

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(GemiDiscoveryCursor)
class GemiDiscoveryCursorAdmin(DiscoveryReadOnlyAdmin):
    list_display = ("stream", "status", "high_water_mark", "last_success_at", "consecutive_failures", "anomaly_reason")


@admin.register(GemiDiscoveryRun)
class GemiDiscoveryRunAdmin(DiscoveryReadOnlyAdmin):
    list_display = ("started_at", "stream", "mode", "status", "pages_fetched", "new_records", "late_publication_records", "cursor_advanced")
    list_filter = ("mode", "status", "stream")


@admin.register(GemiRefreshRun)
class GemiRefreshRunAdmin(DiscoveryReadOnlyAdmin):
    """Inspection only. Refresh runs are written by run_gemi_company_refresh; aggregate counts, no payload."""

    list_display = (
        "started_at", "status", "due_companies", "planned_companies", "deferred_no_strategy", "request_count",
        "target_records_observed", "baselines_created", "changed_snapshots_created", "unchanged_snapshots",
        "search_misses", "company_failures", "incomplete_groups",
    )
    list_filter = ("status",)
    date_hierarchy = "started_at"


@admin.register(GemiDiscoveryObservation)
class GemiDiscoveryObservationAdmin(DiscoveryReadOnlyAdmin):
    list_display = ("gemi_number", "classification", "incorporation_date", "incorporation_date_quality", "run")
    list_filter = ("classification", "incorporation_date_quality")
    search_fields = ("gemi_number",)


@admin.register(GemiSourceRecord)
class GemiSourceRecordAdmin(admin.ModelAdmin):
    """Inspection only. Rows are written by the GEMI client and hold request metadata, a payload hash
    and the sanitised company-level payload -- no raw response, personal data or credentials. Add and
    change are disabled for the same reason as StripeWebhookEventAdmin."""

    list_display = ("fetched_at", "family", "endpoint", "http_status", "result_count", "short_payload_hash", "retention_class", "expires_at")
    list_filter = ("family", "retention_class", "response_schema_version", "normalizer_version")
    search_fields = ("endpoint", "payload_hash", "request_fingerprint", "gateway_request_id")
    readonly_fields = (
        "source", "family", "response_family", "endpoint", "request_params", "request_fingerprint", "observation_key",
        "fetched_at", "http_status", "gateway_request_id", "payload_hash", "result_count", "response_schema_version",
        "normalizer_version", "record_format_version", "sanitised_payload", "retention_class", "expires_at", "created_at",
    )

    @admin.display(description="Payload hash")
    def short_payload_hash(self, obj):
        return obj.payload_hash[:12]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(EmailEngagementEvent)
class EmailEngagementEventAdmin(admin.ModelAdmin):
    """Inspection only, same reasoning as StripeWebhookEventAdmin -- rows are written
    exclusively by the Brevo webhook and must reflect what Brevo actually reported."""

    list_display = ("email", "event_type", "tag", "received_at")
    list_filter = ("event_type",)
    search_fields = ("email", "tag")
    readonly_fields = ("event_type", "email", "tag", "payload", "received_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
