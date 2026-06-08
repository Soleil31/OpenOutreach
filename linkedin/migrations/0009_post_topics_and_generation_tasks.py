# Stub migration: the real one was applied historically but never committed
# to the repo (Soleil31 fork). Servers already have all the schema changes;
# this empty placeholder restores the migration graph so 0011 can depend on it.
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("linkedin", "0008_post_model"),
    ]
    operations: list = []
