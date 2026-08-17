from django.db import migrations


OLD_DEMO_EMAIL = "demo@gemisignal.gr"
NEW_DEMO_EMAIL = "demo@gemileads.gr"


def rename_demo_user(apps, schema_editor):
    User = apps.get_model("auth", "User")
    if User.objects.filter(username=NEW_DEMO_EMAIL).exists():
        return
    User.objects.filter(username=OLD_DEMO_EMAIL).update(
        username=NEW_DEMO_EMAIL,
        email=NEW_DEMO_EMAIL,
    )


def restore_demo_user(apps, schema_editor):
    User = apps.get_model("auth", "User")
    if User.objects.filter(username=OLD_DEMO_EMAIL).exists():
        return
    User.objects.filter(username=NEW_DEMO_EMAIL).update(
        username=OLD_DEMO_EMAIL,
        email=OLD_DEMO_EMAIL,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("gemiapp", "0003_add_gemi_only_activity_codes"),
    ]

    operations = [
        migrations.RunPython(rename_demo_user, restore_demo_user),
    ]
