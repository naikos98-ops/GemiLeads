import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("gemiapp", "0055_gemi_request_attempt")]

    operations = [
        migrations.CreateModel(
            name="LegacyRadarMigrationMap",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("mapping_version", models.PositiveSmallIntegerField(default=1)),
                ("migrated_at", models.DateTimeField(auto_now_add=True)),
                ("legacy_radar", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                    related_name="+", to="gemiapp.customerradar")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name="+", to="gemiapp.organization")),
                ("organization_radar", models.OneToOneField(blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL, related_name="+",
                    to="gemiapp.organizationradar")),
            ],
            options={"verbose_name": "Legacy radar migration map", "ordering": ["legacy_radar_id"]},
        ),
    ]
