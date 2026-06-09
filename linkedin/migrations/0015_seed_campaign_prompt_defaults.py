from django.db import migrations, models


_POST_PROMPT_HELP = (
    "Системный промпт для AI-генератора текста поста. Пишется как "
    "обычный текст с подстановками в фигурных скобках, например "
    "{topic} или {product_docs}.\n\n"
    "Подстановки:\n"
    "  • {self_name} — имя автора (из LinkedIn-профиля бота)\n"
    "  • {product_docs} — описание компании (поле выше)\n"
    "  • {post_system_prompt} — правила стиля (поле выше)\n"
    "  • {topic} — тема конкретного поста (из Post Topic)\n"
    "  • {language} — язык (из настройки Post language)\n"
    "  • {hashtags_instruction} — инструкция «добавь N хэштегов» "
    "(автоматически собирается)\n"
    "  • {cta_instruction} — инструкция «добавь CTA» (если задан)\n\n"
    "Если очистить поле и сохранить — снова появится дефолт."
)

_COVER_PROMPT_HELP = (
    "Промпт для AI, который пишет короткую фразу-подпись (5–9 слов) "
    "поверх Figma-обложки.\n\n"
    "Подстановки:\n"
    "  • {post_text} — уже сгенерированный текст поста\n"
    "  • {topic} — тема (из Post Topic)\n"
    "  • {language} — язык (из настройки Post language)\n\n"
    "Если очистить поле и сохранить — снова появится дефолт."
)


def _seed_existing_campaigns(apps, schema_editor):
    """Backfill the new default prompt into rows created on 0014, which
    landed as blank because the original migration set default=''."""
    from linkedin.agents.post_prompt_defaults import (
        DEFAULT_COVER_TEMPLATE,
        DEFAULT_POST_TEMPLATE,
    )
    Campaign = apps.get_model("linkedin", "Campaign")
    for c in Campaign.objects.all():
        changed = False
        if not c.post_prompt_template:
            c.post_prompt_template = DEFAULT_POST_TEMPLATE
            changed = True
        if not c.cover_text_prompt_template:
            c.cover_text_prompt_template = DEFAULT_COVER_TEMPLATE
            changed = True
        if changed:
            c.save(update_fields=[
                "post_prompt_template", "cover_text_prompt_template",
            ])


def _default_post_prompt_template():
    from linkedin.agents.post_prompt_defaults import DEFAULT_POST_TEMPLATE
    return DEFAULT_POST_TEMPLATE


def _default_cover_text_prompt_template():
    from linkedin.agents.post_prompt_defaults import DEFAULT_COVER_TEMPLATE
    return DEFAULT_COVER_TEMPLATE


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0014_campaign_prompt_templates"),
    ]

    operations = [
        migrations.AlterField(
            model_name="campaign",
            name="post_prompt_template",
            field=models.TextField(
                blank=True,
                default=_default_post_prompt_template,
                help_text=_POST_PROMPT_HELP,
            ),
        ),
        migrations.AlterField(
            model_name="campaign",
            name="cover_text_prompt_template",
            field=models.TextField(
                blank=True,
                default=_default_cover_text_prompt_template,
                help_text=_COVER_PROMPT_HELP,
            ),
        ),
        migrations.RunPython(
            _seed_existing_campaigns,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
