from django.contrib import admin
from .models import (
    ActivityCode,
    Company,
    CompanyActivity,
    CustomerRadar,
    DigestDelivery,
    DigestPreference,
    ImportRun,
    RadarMatch,
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
