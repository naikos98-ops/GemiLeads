from django.contrib import admin
from .models import Company, DigestDelivery, DigestPreference, ImportRun


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "gemi_number", "incorporation_date", "legal_type", "prefecture", "is_active")
    list_filter = ("incorporation_date", "is_active", "legal_type", "prefecture")
    search_fields = ("name", "gemi_number", "vat_number")
    date_hierarchy = "incorporation_date"


admin.site.register(DigestPreference)
admin.site.register(DigestDelivery)
admin.site.register(ImportRun)
