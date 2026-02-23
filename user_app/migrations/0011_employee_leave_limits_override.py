from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("user_app", "0010_remove_employee_leave_limits_override"),
    ]

    operations = [
        migrations.AddField(
            model_name="employee",
            name="leave_limits_override",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
