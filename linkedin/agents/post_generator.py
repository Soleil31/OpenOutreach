# linkedin/agents/post_generator.py
"""LLM agent that generates LinkedIn post text from a topic prompt."""
from __future__ import annotations

import logging

import jinja2

from linkedin.conf import PROMPTS_DIR
from linkedin.llm import is_codex_provider

logger = logging.getLogger(__name__)


def generate_post_text(
    session,
    campaign,
    topic: str,
    include_hashtags: bool,
    cta: str,
    language: str,
) -> str:
    """Generate post text via LLM. Returns the raw post string."""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    template = env.get_template("post_generator.j2")

    self_prof = session.self_profile
    self_name = (
        f"{self_prof.get('first_name', '')} {self_prof.get('last_name', '')}".strip()
        or session.django_user.username
    )

    system_prompt = template.render(
        self_name=self_name,
        product_docs=campaign.product_docs or "",
        post_system_prompt=campaign.post_system_prompt or "",
        topic=topic,
        include_hashtags="yes" if include_hashtags else "no",
        cta=cta or "",
        language=language or "English",
    )

    if is_codex_provider():
        from linkedin.agents.codex_client import get_codex_client
        text: str = get_codex_client().chat(
            system_prompt=system_prompt,
            user_prompt="Write the post now.",
        )
    else:
        from pydantic_ai import Agent
        from linkedin.llm import get_llm_model
        agent = Agent(
            get_llm_model(),
            system_prompt=system_prompt,
            output_type=str,
            model_settings={"temperature": 0.8, "timeout": 60},
        )
        text = agent.run_sync("Write the post now.").output.strip()

    logger.info("Generated post (%d chars) for campaign %s", len(text), campaign.name)
    return text
