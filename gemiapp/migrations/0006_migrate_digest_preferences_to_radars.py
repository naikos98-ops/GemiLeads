from django.db import migrations
from django.utils import timezone


MIGRATED_RADAR_NAMES = ("Το πρώτο μου ραντάρ", "Όλες οι νέες επιχειρήσεις")


def create_initial_radars(apps, schema_editor):
    ActivityCode = apps.get_model("gemiapp", "ActivityCode")
    CustomerRadar = apps.get_model("gemiapp", "CustomerRadar")
    DigestPreference = apps.get_model("gemiapp", "DigestPreference")

    for preference in DigestPreference.objects.select_related("user").iterator():
        has_filters = bool(preference.activity_codes or preference.prefectures or preference.legal_types)
        radar = CustomerRadar.objects.create(
            user_id=preference.user_id,
            name="Το πρώτο μου ραντάρ" if has_filters else "Όλες οι νέες επιχειρήσεις",
            is_active=preference.frequency != "off",
            prefectures=list(preference.prefectures or []),
            legal_types=list(preference.legal_types or []),
            only_active=preference.only_active,
            frequency=preference.frequency,
            monitor_from=timezone.now(),
        )
        codes = ActivityCode.objects.filter(normalized_code__in=preference.activity_codes or [])
        radar.activity_codes.add(*codes)


def remove_initial_radars(apps, schema_editor):
    apps.get_model("gemiapp", "CustomerRadar").objects.filter(name__in=MIGRATED_RADAR_NAMES).delete()


class Migration(migrations.Migration):
    dependencies = [("gemiapp", "0005_customerradar_usercompanylead_radarmatch_and_more")]

    operations = [migrations.RunPython(create_initial_radars, remove_initial_radars)]
