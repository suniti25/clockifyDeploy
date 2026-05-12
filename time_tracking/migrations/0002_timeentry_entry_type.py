from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("time_tracking", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="timeentry",
            name="entry_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Unspecified"),
                    ("MEETING", "Meeting"),
                    ("DEVELOPMENT", "Development"),
                    ("OVERTIME", "Overtime"),
                ],
                db_index=True,
                default="",
                max_length=20,
            ),
        ),
    ]
