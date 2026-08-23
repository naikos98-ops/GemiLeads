from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    """Create the DatabaseCache table.

    Done as a migration rather than a deploy command so it cannot be forgotten on a new
    environment: without the table every cached view raises, and the rate limiter, which
    fails closed by design, would lock everyone out. createcachetable is idempotent and
    skips a table that already exists.
    """
    call_command("createcachetable", "gemi_cache", database=schema_editor.connection.alias, verbosity=0)


def drop_cache_table(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS gemi_cache")


class Migration(migrations.Migration):

    dependencies = [("gemiapp", "0018_personsuppression")]

    operations = [migrations.RunPython(create_cache_table, drop_cache_table)]
