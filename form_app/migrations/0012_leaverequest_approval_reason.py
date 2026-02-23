from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("form_app", "0011_leavepolicysettings"),
    ]

    operations = [
        migrations.AddField(
            model_name="leaverequest",
            name="approval_reason",
            field=models.TextField(blank=True, null=True),
        ),
    ]
