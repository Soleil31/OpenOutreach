from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('linkedin', '0007_siteconfig_llm_provider'),
    ]

    operations = [
        migrations.AddField(
            model_name='campaign',
            name='posting_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='campaign',
            name='post_system_prompt',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='campaign',
            name='post_timezone',
            field=models.CharField(blank=True, default='UTC', max_length=64),
        ),
        migrations.AddField(
            model_name='campaign',
            name='post_days_of_week',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='campaign',
            name='post_times',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='campaign',
            name='posts_per_week',
            field=models.IntegerField(default=3),
        ),
        migrations.CreateModel(
            name='Post',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('topic', models.TextField()),
                ('text', models.TextField(blank=True)),
                ('image_path', models.CharField(blank=True, max_length=500)),
                ('status', models.CharField(
                    choices=[
                        ('pending_review', 'Pending Review'),
                        ('approved', 'Approved'),
                        ('rejected', 'Rejected'),
                        ('published', 'Published'),
                        ('cancelled', 'Cancelled'),
                        ('failed', 'Failed'),
                    ],
                    default='pending_review',
                    max_length=20,
                )),
                ('scheduled_at', models.DateTimeField(blank=True, null=True)),
                ('published_at', models.DateTimeField(blank=True, null=True)),
                ('approval_deadline', models.DateTimeField()),
                ('generation_attempts', models.IntegerField(default=1)),
                ('fail_reason', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('campaign', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='posts',
                    to='linkedin.campaign',
                )),
            ],
            options={
                'app_label': 'linkedin',
                'ordering': ['-created_at'],
            },
        ),
    ]
