# Backfills Campaign.outreach_enabled — declared on the model since
# the post-publishing rework but never made it into a committed migration
# (Soleil31 fork didn't ship the 0009/0010 source files; only their effects
# in the DB schema, and `outreach_enabled` slipped through).
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("linkedin", "0011_figma_fields"),
    ]
    operations = [
        migrations.AddField(
            model_name="campaign",
            name="outreach_enabled",
            field=models.BooleanField(default=True),
        ),
    ]
