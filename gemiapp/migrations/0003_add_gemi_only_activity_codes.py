import unicodedata
from django.db import migrations


def normalize_text(value):
    decomposed = unicodedata.normalize('NFD', value or '')
    return ' '.join(''.join(ch for ch in decomposed if unicodedata.category(ch) != 'Mn').upper().split())


def display_code(code):
    return '.'.join(code[index:index + 2] for index in range(0, 8, 2)) if len(code) == 8 else code


def add_gemi_only_codes(apps, schema_editor):
    ActivityCode = apps.get_model('gemiapp', 'ActivityCode')
    CompanyActivity = apps.get_model('gemiapp', 'CompanyActivity')
    existing = set(ActivityCode.objects.values_list('normalized_code', flat=True))
    missing = {}
    for code, description in CompanyActivity.objects.values_list('code', 'description').iterator(chunk_size=1000):
        if code not in existing:
            missing.setdefault(code, description)
    ActivityCode.objects.bulk_create([
        ActivityCode(
            code=display_code(code),
            normalized_code=code,
            description=description or 'Δραστηριότητα ΓΕΜΗ',
            source='',
            search_text=normalize_text(f'{display_code(code)} {code} {description}'),
        )
        for code, description in missing.items()
    ], batch_size=1000, ignore_conflicts=True)


def remove_gemi_only_codes(apps, schema_editor):
    apps.get_model('gemiapp', 'ActivityCode').objects.filter(source='').delete()


class Migration(migrations.Migration):
    dependencies = [('gemiapp', '0002_activitycode_digestpreference_activity_codes_and_more')]
    operations = [migrations.RunPython(add_gemi_only_codes, remove_gemi_only_codes)]
