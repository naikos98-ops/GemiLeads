from django.db import migrations


def backfill_digest_preferences(apps, schema_editor):
    """Accounts created outside the signup flow never got a DigestPreference and were therefore
    invisible to send_digests, even on a paid tier."""
    User = apps.get_model("auth", "User")
    DigestPreference = apps.get_model("gemiapp", "DigestPreference")

    existing = set(DigestPreference.objects.values_list("user_id", flat=True))
    missing = [
        DigestPreference(user_id=user_id)
        for user_id in User.objects.exclude(id__in=existing).values_list("id", flat=True)
    ]
    DigestPreference.objects.bulk_create(missing, batch_size=500)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('gemiapp', '0015_company_search_name'),
    ]

    operations = [
        migrations.RunPython(backfill_digest_preferences, noop),
    ]
