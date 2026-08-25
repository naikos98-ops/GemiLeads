from django.contrib import admin
from .models import (
    ActivityCode,
    Company,
    CompanyActivity,
    CustomerRadar,
    DigestDelivery,
    DigestPreference,
    ImportRun,
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
