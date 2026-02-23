from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("user_app", "0005_employee_reset_leave_balance"),
    ]

    operations = [
        migrations.AddField(
            model_name="employee",
            name="leave_renewal_date_override",
            field=models.DateField(blank=True, null=True),
        ),
    ]
