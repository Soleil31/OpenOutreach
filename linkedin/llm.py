"""LLM model factory: build a pydantic-ai `Model` from `SiteConfig`.

Single boundary for LLM construction. Call sites import `get_llm_model()` and
hand the result to `pydantic_ai.Agent(...)`. Provider-specific routing lives
here so the rest of the codebase stays provider-agnostic.

Agents are run through :func:`run_agent`, never through ``Agent.run_sync``.

Why: ``run_sync`` wraps the async ``run`` in ``loop.run_until_complete``, and
something in pydantic-ai's internals (anyio task group / portal) leaves the
calling thread's running-loop slot populated afterwards. Every subsequent
``run_sync`` then trips the re-entrancy guard in
``BaseEventLoop._check_running``.

This module used to paper over that with ``nest_asyncio.apply()``. That fixed
the LLM calls and silently broke the browser: Playwright's sync API refuses to
start when the thread has a running loop, so the daemon died with

    It looks like you are using Playwright Sync API inside the asyncio loop.

on every ``launch_browser`` that happened after any LLM call. In practice the
account stopped collecting leads entirely while the container looked healthy —
tasks were not even marked failed, so the metrics showed nothing wrong.

Instead of patching the caller's loop, LLM calls now run on their own event
loop in a dedicated thread. The calling thread is never touched, so Playwright
keeps working. The loop is long-lived on purpose: provider clients
(AsyncOpenAI, AsyncAnthropic, ...) cache connections bound to the loop that
created them, and a fresh ``asyncio.run`` per call would invalidate them.
"""
from __future__ import annotations

import asyncio
import threading


class _AgentLoop:
    """A private event loop living on its own thread.

    Started lazily so importing this module stays free for code paths that
    never touch an LLM.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def _ensure_running(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(
                    target=loop.run_forever,
                    name="llm-agent-loop",
                    daemon=True,
                ).start()
                self._loop = loop
            return self._loop

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_running()).result()


_AGENT_LOOP = _AgentLoop()


def run_agent(agent, *args, **kwargs):
    """Run a pydantic-ai agent from synchronous code.

    Drop-in replacement for ``agent.run_sync(...)``. Blocks until the agent
    finishes and returns the same result object.
    """
    return _AGENT_LOOP.run(agent.run(*args, **kwargs))


# Override the SDK default of 2. Each retry uses the SDK's built-in jittered
# exponential backoff and honors `Retry-After`, so 8 attempts ride through
# typical 429/529 capacity blips (~1–2 minutes) instead of failing in ~1.5s.
_MAX_RETRIES = 8


# ── Per-provider builders ──

def _build_openai(cfg):
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIModel
    from pydantic_ai.providers.openai import OpenAIProvider
    client = AsyncOpenAI(api_key=cfg.llm_api_key, max_retries=_MAX_RETRIES)
    return OpenAIModel(cfg.ai_model, provider=OpenAIProvider(openai_client=client))


def _build_anthropic(cfg):
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider
    client = AsyncAnthropic(api_key=cfg.llm_api_key, max_retries=_MAX_RETRIES)
    return AnthropicModel(cfg.ai_model, provider=AnthropicProvider(anthropic_client=client))


def _build_google(cfg):
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider
    return GoogleModel(cfg.ai_model, provider=GoogleProvider(api_key=cfg.llm_api_key))


def _build_groq(cfg):
    from groq import AsyncGroq
    from pydantic_ai.models.groq import GroqModel
    from pydantic_ai.providers.groq import GroqProvider
    client = AsyncGroq(api_key=cfg.llm_api_key, max_retries=_MAX_RETRIES)
    return GroqModel(cfg.ai_model, provider=GroqProvider(groq_client=client))


def _build_mistral(cfg):
    from pydantic_ai.models.mistral import MistralModel
    from pydantic_ai.providers.mistral import MistralProvider
    return MistralModel(cfg.ai_model, provider=MistralProvider(api_key=cfg.llm_api_key))


def _build_cohere(cfg):
    from pydantic_ai.models.cohere import CohereModel
    from pydantic_ai.providers.cohere import CohereProvider
    return CohereModel(cfg.ai_model, provider=CohereProvider(api_key=cfg.llm_api_key))


def _build_openai_compatible(cfg):
    if not cfg.llm_api_base:
        raise ValueError("LLM_API_BASE is required for the openai_compatible provider.")
    from pydantic_ai.models.openai import OpenAIModel
    from pydantic_ai.providers.openai import OpenAIProvider
    return OpenAIModel(cfg.ai_model, provider=OpenAIProvider(
        base_url=cfg.llm_api_base, api_key=cfg.llm_api_key,
    ))


_PROVIDER_BUILDERS = {
    "openai": _build_openai,
    "anthropic": _build_anthropic,
    "google": _build_google,
    "groq": _build_groq,
    "mistral": _build_mistral,
    "cohere": _build_cohere,
    "openai_compatible": _build_openai_compatible,
}


# ── Public API ──

def _validated_site_config():
    """Load `SiteConfig` and assert the required LLM fields are populated.

    ``llm_api_key`` is only required for API-key based providers; codex
    authenticates via ChatGPT OAuth (``auth.json``) and has no api_key.
    """
    from linkedin.models import SiteConfig

    cfg = SiteConfig.load()
    if cfg.llm_provider != SiteConfig.LLMProvider.CODEX and not cfg.llm_api_key:
        raise ValueError("LLM_API_KEY is not set in Site Configuration.")
    if not cfg.ai_model:
        raise ValueError("AI_MODEL is not set in Site Configuration.")
    return cfg


def get_llm_model():
    """Return a configured pydantic-ai `Model` for the current `SiteConfig`."""
    cfg = _validated_site_config()
    builder = _PROVIDER_BUILDERS.get(cfg.llm_provider)
    if builder is None:
        raise ValueError(f"Unknown LLM provider: {cfg.llm_provider!r}")
    return builder(cfg)


def is_codex_provider() -> bool:
    """Return True when SiteConfig.llm_provider == 'codex'.

    Reads SiteConfig directly — does NOT go through ``_validated_site_config``
    because that one used to raise on missing api_key, hiding codex behind
    its own guard.
    """
    from linkedin.models import SiteConfig

    try:
        return SiteConfig.load().llm_provider == SiteConfig.LLMProvider.CODEX
    except Exception:
        return False
