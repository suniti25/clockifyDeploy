from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("user_app", "0008_backfill_projects_from_employees"),
    ]

    operations = [
        migrations.AddField(
            model_name="employee",
            name="leave_limits_override",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
