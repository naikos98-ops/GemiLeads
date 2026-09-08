from django.db import migrations, models


def cancel_pending_and_sending_outreach(apps, schema_editor):
    CompanyOutreach = apps.get_model("gemiapp", "CompanyOutreach")
    CompanyOutreach.objects.filter(status__in=["pending", "sending"]).update(
        status="cancelled",
        error_message="Αποστολή ακυρώθηκε λόγω οριστικής διακοπής cold outreach (compliance shutdown).",
    )


def reverse_cancel_pending_and_sending_outreach(apps, schema_editor):
    # Data migration reverse is a no-op: cancelled records stay cancelled.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("gemiapp", "0030_alter_companyoutreach_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="companyoutreach",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Σε ουρά"),
                    ("sending", "Αποστέλλεται"),
                    ("sent", "Εστάλη"),
                    ("failed", "Απέτυχε"),
                    ("cancelled", "Ακυρώθηκε"),
                ],
                default="sent",
                max_length=10,
            ),
        ),
        migrations.RunPython(
            cancel_pending_and_sending_outreach,
            reverse_code=reverse_cancel_pending_and_sending_outreach,
        ),
    ]
