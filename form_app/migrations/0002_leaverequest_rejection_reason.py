from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('form_app', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='leaverequest',
            name='rejection_reason',
            field=models.TextField(blank=True, null=True),
        ),
    ]
