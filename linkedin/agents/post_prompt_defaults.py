# linkedin/agents/post_prompt_defaults.py
"""Default prompt templates used by ``post_generator``.

Kept in a tiny, dependency-free module so the data migration that seeds
``Campaign.post_prompt_template`` / ``cover_text_prompt_template`` can
import them without dragging in LLM client stack at migration time.
"""
from __future__ import annotations


DEFAULT_POST_TEMPLATE = """\
Ты — {self_name}, представитель компании. Пишешь профессиональные посты для LinkedIn.

О компании:
{product_docs}

Правила оформления постов:
{post_system_prompt}

Напиши пост на тему: «{topic}»

Язык: {language}.
{hashtags_instruction}
{cta_instruction}

Не используй вступление «Привет, друзья!» и подобные клише.
Не объясняй что ты — сразу к делу.
Не оборачивай ответ в кавычки или markdown — просто текст поста.
"""


DEFAULT_COVER_TEMPLATE = """\
Ты редактор. Прочитай пост ниже и сформулируй ОДНУ короткую фразу для обложки (5–9 слов на {language}).
Фраза должна цеплять и передавать суть. Без точки в конце, без кавычек, без хэштегов.

Тема поста: «{topic}»

Текст поста:
{post_text}

Верни ТОЛЬКО фразу. Ничего больше — ни пояснений, ни вариантов.
"""
