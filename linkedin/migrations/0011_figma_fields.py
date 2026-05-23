from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0010_linkedinprofile_browser_cookies"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfig",
            name="figma_token",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="campaign",
            name="figma_file_key",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
