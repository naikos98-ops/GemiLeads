from django.db import migrations


def unwrap_json_array_tags(apps, schema_editor):
    """Existing rows written before the webhook unwrapped the JSON-array tag form the SMTP
    relay sends: '["outreach:17"]' -> 'outreach:17'. Without this the superadmin dashboard's
    tag__in lookup never matches any historical engagement event.
    """
    import json

    EmailEngagementEvent = apps.get_model("gemiapp", "EmailEngagementEvent")
    for event in EmailEngagementEvent.objects.filter(tag__startswith="[").iterator():
        try:
            parsed = json.loads(event.tag)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list) and parsed:
            new_tag = parsed[0] or ""
        elif isinstance(parsed, list):
            new_tag = ""
        else:
            new_tag = str(parsed)
        if new_tag != event.tag:
            event.tag = new_tag
            event.save(update_fields=["tag"])


class Migration(migrations.Migration):

    dependencies = [
        ("gemiapp", "0026_emailengagementevent"),
    ]

    operations = [
        migrations.RunPython(unwrap_json_array_tags, migrations.RunPython.noop),
    ]
