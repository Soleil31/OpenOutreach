from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0013_campaign_post_extras"),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="post_prompt_template",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Шаблон системного промпта для генерации текста поста. "
                    "Доступные подстановки: {self_name}, {product_docs}, "
                    "{post_system_prompt}, {topic}, {language}, "
                    "{hashtags_instruction}, {cta_instruction}. "
                    "Если оставить пустым — используется встроенный дефолт."
                ),
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="cover_text_prompt_template",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Шаблон промпта для генерации короткой фразы-overlay на "
                    "обложке (5–9 слов). Подстановки: {post_text}, {topic}, "
                    "{language}. Если оставить пустым — используется встроенный "
                    "дефолт."
                ),
            ),
        ),
    ]
