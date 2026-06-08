# Backfills two Campaign fields that the model declares but no committed
# migration ever introduced — same story as 0012_campaign_outreach_enabled.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("linkedin", "0012_campaign_outreach_enabled"),
    ]
    operations = [
        migrations.AddField(
            model_name="campaign",
            name="post_language",
            field=models.CharField(max_length=32, blank=True, default="English"),
        ),
        migrations.AddField(
            model_name="campaign",
            name="post_approval_timeout_hours",
            field=models.PositiveIntegerField(default=24),
        ),
    ]
