from django.db import migrations, models

from gemiapp.kad import normalize_kad_search

BATCH_SIZE = 2000


def backfill_search_name(apps, schema_editor):
    Company = apps.get_model("gemiapp", "Company")
    batch = []
    for company in Company.objects.only("id", "name").iterator(chunk_size=BATCH_SIZE):
        company.search_name = normalize_kad_search(company.name)
        batch.append(company)
        if len(batch) >= BATCH_SIZE:
            Company.objects.bulk_update(batch, ["search_name"])
            batch = []
    if batch:
        Company.objects.bulk_update(batch, ["search_name"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('gemiapp', '0014_alter_digestdelivery_frequency'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='search_name',
            field=models.CharField(blank=True, db_index=True, editable=False, max_length=500),
        ),
        migrations.RunPython(backfill_search_name, noop),
    ]
