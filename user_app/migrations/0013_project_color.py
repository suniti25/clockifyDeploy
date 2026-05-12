from __future__ import annotations

from django.core.validators import RegexValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("user_app", "0012_alter_profile_role"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="color",
            field=models.CharField(
                default="#6366f1",
                max_length=7,
                validators=[
                    RegexValidator(
                        regex=r"^#[0-9A-Fa-f]{6}$",
                        message="Color must be a hex value like #6366f1.",
                    )
                ],
            ),
        ),
    ]
