# linkedin/agents/post_generator.py
"""LLM agents that generate the post text and the cover overlay phrase.

Both prompt templates live on the Campaign row (``post_prompt_template``,
``cover_text_prompt_template``) so a non-developer can tweak them in the
Django Admin. New campaigns are seeded with sensible defaults from
``post_prompt_defaults`` (via the ``Campaign.*.default`` Django field +
the seed data migration), so an editor sees a working template the
first time they open the form — no "what do I write here" problem.

Substitution is plain ``str.format(**ctx)`` — no Jinja, no engine, no
``j2`` file on disk. The Admin's ``help_text`` lists every placeholder.
"""
from __future__ import annotations

import logging

from linkedin.agents.post_prompt_defaults import (
    DEFAULT_COVER_TEMPLATE,
    DEFAULT_POST_TEMPLATE,
)
from linkedin.llm import is_codex_provider, run_agent

logger = logging.getLogger(__name__)


# ── Public API ───────────────────────────────────────────────────────


def generate_post_text(
    session,
    campaign,
    topic: str,
    include_hashtags: bool,
    cta: str,
    language: str,
    hashtags_count: int = 3,
) -> str:
    """Generate post body text via LLM. Returns the raw post string."""
    self_prof = session.self_profile
    self_name = (
        f"{self_prof.get('first_name', '')} {self_prof.get('last_name', '')}".strip()
        or session.django_user.username
    )

    hashtags_instruction = (
        f"В конце добавь {hashtags_count} тематических хэштега."
        if include_hashtags else ""
    )
    cta_instruction = f"В конце добавь призыв к действию: {cta}" if cta else ""

    template = (campaign.post_prompt_template or DEFAULT_POST_TEMPLATE).strip()
    system_prompt = template.format(
        self_name=self_name,
        product_docs=(campaign.product_docs or "").strip(),
        post_system_prompt=(campaign.post_system_prompt or "").strip(),
        topic=topic,
        language=language or campaign.post_language or "English",
        hashtags_instruction=hashtags_instruction,
        cta_instruction=cta_instruction,
    )

    text = _ask_llm(system_prompt, user_prompt="Напиши пост.")
    logger.info("Generated post (%d chars) for campaign %s", len(text), campaign.name)
    return text


def generate_cover_text(
    campaign,
    topic: str,
    post_text: str,
    language: str,
) -> str:
    """Generate a short (5–9 word) overlay phrase for the cover image."""
    template = (campaign.cover_text_prompt_template or DEFAULT_COVER_TEMPLATE).strip()
    system_prompt = template.format(
        topic=topic,
        post_text=post_text,
        language=language or campaign.post_language or "English",
    )
    phrase = _ask_llm(system_prompt, user_prompt="Верни фразу.").strip()
    # Strip stray quotes / trailing periods the model sometimes adds.
    phrase = phrase.strip("\"'«»").rstrip(".!?")
    logger.info(
        "Generated cover text for campaign %s: %r", campaign.name, phrase[:80],
    )
    return phrase


# ── LLM call (shared) ────────────────────────────────────────────────


def _ask_llm(system_prompt: str, user_prompt: str) -> str:
    if is_codex_provider():
        from linkedin.agents.codex_client import get_codex_client
        return get_codex_client().chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    from pydantic_ai import Agent
    from linkedin.llm import get_llm_model
    agent = Agent(
        get_llm_model(),
        system_prompt=system_prompt,
        output_type=str,
        model_settings={"temperature": 0.8, "timeout": 60},
    )
    return run_agent(agent, user_prompt).output.strip()
