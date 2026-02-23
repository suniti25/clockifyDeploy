from __future__ import annotations

from django.db import migrations


def backfill_projects(apps, schema_editor):
    Employee = apps.get_model("user_app", "Employee")
    Project = apps.get_model("user_app", "Project")

    # Collect distinct non-empty project names from existing employees
    names = set()
    for emp in Employee.objects.exclude(current_project__isnull=True).exclude(
        current_project__exact=""
    ):
        name = (emp.current_project or "").strip()
        if name:
            names.add(name)

    for name in sorted(names):
        # Use iexact to avoid duplicates with different casing
        existing = Project.objects.filter(name__iexact=name).first()
        if existing:
            continue
        Project.objects.create(name=name, is_active=True)


def noop_reverse(apps, schema_editor):
    # No reverse migration: do not delete projects.
    return


class Migration(migrations.Migration):
    dependencies = [
        ("user_app", "0007_project"),
    ]

    operations = [
        migrations.RunPython(backfill_projects, reverse_code=noop_reverse),
    ]
