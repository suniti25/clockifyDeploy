from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("form_app", "0009_remove_leaverequest_notified_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="leaverequest",
            name="google_event_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
