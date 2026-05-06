from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("user_app", "0011_employee_leave_limits_override"),
    ]

    operations = [
        migrations.AlterField(
            model_name="profile",
            name="role",
            field=models.CharField(
                choices=[
                    ("EMPLOYEE", "Employee"),
                    ("MANAGER", "Manager"),
                    ("ADMIN", "Admin"),
                ],
                default="EMPLOYEE",
                max_length=20,
            ),
        ),
    ]
