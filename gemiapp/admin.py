from django.contrib import admin
from .models import ActivityCode, Company, CompanyActivity, DigestDelivery, DigestPreference, ImportRun


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
