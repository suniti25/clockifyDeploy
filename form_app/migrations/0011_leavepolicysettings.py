from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("form_app", "0010_leaverequest_google_event_id"),
    ]

    operations = [
        migrations.CreateModel(
            name="LeavePolicySettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("global_renewal_date", models.DateField(blank=True, null=True)),
                ("carryover_percentage", models.IntegerField(default=50)),
                ("probation_period_days", models.IntegerField(default=90)),
                ("vacation_days", models.IntegerField(default=14)),
                ("sick_days", models.IntegerField(default=12)),
                ("maternity_days", models.IntegerField(default=60)),
                ("paternity_days", models.IntegerField(default=10)),
                ("bereavement_days", models.IntegerField(default=3)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
