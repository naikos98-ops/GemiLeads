from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('gemiapp', '0028_add_companyoutreach_sent_at'),
    ]

    operations = [
        # Plain CREATE INDEX. It briefly locks writes on the Company table, but this runs in
        # the deploy's preDeployCommand before traffic shifts to the new release, and the
        # table is small enough that the build takes a couple of seconds. (CONCURRENTLY would
        # avoid the lock but can't run on SQLite, which the test suite uses.)
        migrations.AddIndex(
            model_name='company',
            index=models.Index(
                fields=['-incorporation_date', '-gemi_number'],
                name='company_recent_order_idx',
            ),
        ),
    ]
