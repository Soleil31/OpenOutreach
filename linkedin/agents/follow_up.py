# linkedin/agents/follow_up.py
"""Follow-up agent: reads conversation, returns a structured decision.

Single LLM call with structured output — no tool-calling loop.
The handler in tasks/follow_up.py executes the decision.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Literal

import jinja2
from pydantic import BaseModel, Field, model_validator
from pydantic_ai import Agent

from linkedin.conf import PROMPTS_DIR
from linkedin.llm import get_llm_model, run_agent

logger = logging.getLogger(__name__)


class FollowUpDecision(BaseModel):
    """Structured output from the follow-up agent."""

    # A member on the existing Literal, never a new field: the codex gateway
    # runs strict structured output (``_strictify_schema`` marks every
    # property required), so a new property would force the model to rule on
    # it every single call and a miss fails the task with no retry.
    action: Literal["send_message", "mark_completed", "wait", "handoff"] = Field(
        description="What to do next for this lead.",
    )
    message: str | None = Field(
        default=None,
        description="The message to send. Required for action='send_message' and 'handoff'.",
    )
    outcome: Literal[
        "converted", "not_interested", "wrong_fit", "no_budget",
        "has_solution", "bad_timing", "unresponsive",
    ] | None = Field(
        default=None,
        description="Why the conversation ended. Required when action='mark_completed'.",
    )
    follow_up_hours: float = Field(
        description="Hours until next follow-up. Always required — you decide the pace.",
    )

    @model_validator(mode="after")
    def _check_required_fields(self):
        if self.action in ("send_message", "handoff") and not self.message:
            raise ValueError("message is required when action='send_message' or 'handoff'")
        if self.action == "mark_completed" and not self.outcome:
            raise ValueError("outcome is required when action='mark_completed'")
        return self


# Number of trailing verbatim messages the agent sees alongside the rolling
# chat_summary. Older turns live in the summary fact list; the recency window
# preserves literal phrasing for the turns that matter most when composing
# the next reply.
RECENT_MESSAGES_WINDOW = 6

# A reply shorter than this is politeness, not an answer — "ок", "спасибо",
# "Добрый день" must not spend the one discovery turn we allow ourselves.
MIN_SUBSTANTIVE_REPLY_WORDS = 4

# How many of our own turns the qualifying window stays open before the
# conversation moves on. Two, so a lead who dodges the question once still
# gets a second, differently-worded attempt.
PIVOT_WINDOW_TURNS = 2

# How many of our own recent openers the prompt is shown, so the agent can
# see the formula it keeps reaching for.
PREVIOUS_OPENERS_WINDOW = 5

_WORDS = re.compile(r"\w+")
_OPENER_END = re.compile(r"[.!?…\n]")

# Leading acknowledgment formulas. The client's complaint was two messages in
# a row opening with "Понял, спасибо за уточнение." — one prompt rule cannot
# be trusted with that, so the formula is also cut structurally. Both the
# comma-joined and the dash-joined forms occur, hence the punctuation class.
_ACK_OPENER = re.compile(
    r"^\s*(?:понял[аи]?|понятно|ясно|хорошо|отлично"
    r"|спасибо(?:\s+за\s+[\w-]+(?:\s+[\w-]+)?)?"
    r"|благодарю(?:\s+за\s+[\w-]+(?:\s+[\w-]+)?)?"
    r"|got\s+it|thanks(?:\s+for\s+[\w-]+(?:\s+[\w-]+)?)?|thank\s+you"
    r"|makes\s+sense|noted|i\s+see)"
    r"[^.!?…\n]{0,30}?[.!?…—–,]+\s+",
    re.IGNORECASE,
)

# What must survive a strip for it to be worth doing.
_MIN_STRIPPED_CHARS = 15
_MIN_STRIPPED_WORDS = 3


def _humanize_age(when: datetime, now: datetime) -> str:
    """Render `when` as a coarse age relative to `now` (e.g. ``3d ago``)."""
    delta = now - when
    if delta < timedelta(hours=1):
        return f"{max(int(delta.total_seconds() // 60), 1)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


def _format_recent_messages(messages: list, now: datetime) -> str:
    """Render the last few ChatMessage rows as a timestamped transcript."""
    if not messages:
        return "No recent messages."
    lines = []
    for m in messages:
        content = (m.content or "").strip()
        if not content:
            continue
        speaker = "Me" if m.is_outgoing else "Lead"
        prefix = f"{speaker} ({_humanize_age(m.creation_date, now)})" if m.creation_date else speaker
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines) or "No recent messages."


def _days_since_last_outgoing(messages: list, now: datetime) -> int | None:
    """Whole days since the most recent outgoing message, or None if there are none."""
    timestamps = [m.creation_date for m in messages if m.is_outgoing and m.creation_date]
    if not timestamps:
        return None
    return max((now - max(timestamps)).days, 0)


def _count_unanswered_outgoing(messages: list) -> int:
    """Trailing run of outgoing messages with no lead reply after them."""
    count = 0
    for m in reversed(messages):
        if m.is_outgoing:
            count += 1
        else:
            break
    return count


def _format_facts(summary: dict | None) -> str:
    """Render a `{facts: [...]}` summary blob as a bullet list."""
    facts = (summary or {}).get("facts") or []
    if not facts:
        return "(none yet)"
    return "\n".join(f"- {f}" for f in facts)


def _log_chat_facts(public_id: str, deal) -> None:
    """Log the mem0 chat facts the agent is working with."""
    chat_facts = (deal.chat_summary or {}).get("facts", [])
    if not chat_facts:
        return
    lines = [f"chat facts for {public_id}:"]
    lines.extend(f"  • {f}" for f in chat_facts)
    logger.info("\n".join(lines))


def _load_recent_messages(deal, limit: int = RECENT_MESSAGES_WINDOW) -> list:
    """Last `limit` ChatMessages for `deal.lead`, in chronological order."""
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(deal.lead.__class__)
    qs = (
        ChatMessage.objects
        .filter(content_type=ct, object_id=deal.lead_id)
        .order_by("-creation_date", "-pk")[:limit]
    )
    return list(reversed(list(qs)))


def _load_deal_messages(deal) -> list:
    """Every ChatMessage for this deal's lead since the deal opened.

    Scoped on ``deal.creation_date`` on purpose: these accounts carry
    months of human conversations that predate the bot, and an old thread
    must not look like engagement the bot earned.
    """
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(deal.lead.__class__)
    qs = (
        ChatMessage.objects
        .filter(
            content_type=ct,
            object_id=deal.lead_id,
            creation_date__gte=deal.creation_date,
        )
        .order_by("creation_date", "pk")
    )
    return list(qs)


def _conversation_stage(messages: list) -> str:
    """Which single strategy the prompt should carry this turn.

    Pure function of the message list — no DB writes, no LLM, nothing
    persisted. The agent cannot compute this itself: it sees only
    ``RECENT_MESSAGES_WINDOW`` messages, and ``chat_summary`` deliberately
    stores no facts about our own turns, so it is blind to how far the
    conversation has actually travelled.

    Returns one of ``opening`` / ``answer_first`` / ``qualify`` / ``advance``.
    """
    anchor = None
    sent_any = False
    for index, message in enumerate(messages):
        if message.is_outgoing:
            sent_any = True
            continue
        # The first real answer, and only after we have spoken — otherwise a
        # lead who wrote first would be met with a commercial question as our
        # opening line.
        if anchor is None and sent_any:
            if len(_WORDS.findall(message.content or "")) >= MIN_SUBSTANTIVE_REPLY_WORDS:
                anchor = index

    if anchor is None:
        return "opening"

    # Checked before everything else: a lead asking us a question outranks
    # any plan we had for this turn.
    last = messages[-1]
    if not last.is_outgoing and "?" in (last.content or ""):
        return "answer_first"

    our_turns_since = sum(1 for m in messages[anchor + 1:] if m.is_outgoing)
    if our_turns_since >= PIVOT_WINDOW_TURNS:
        return "advance"
    return "qualify"


def _previous_openers(messages: list, limit: int = PREVIOUS_OPENERS_WINDOW) -> list:
    """Opening lines of our own recent messages, newest first, deduplicated.

    Shown to the agent so it can see the formula it keeps reaching for. The
    rolling ``chat_summary`` cannot help here — it stores facts about the
    lead only, never about what we said.
    """
    openers: list = []
    seen = set()
    for message in reversed(messages):
        if not message.is_outgoing:
            continue
        content = (message.content or "").strip()
        if not content:
            continue
        opener = (_OPENER_END.split(content, 1)[0].strip() or content)[:80]
        key = opener.casefold()
        if key in seen:
            continue
        seen.add(key)
        openers.append(opener)
        if len(openers) >= limit:
            break
    return openers


def _strip_ack_opener(message: str) -> str:
    """Delete leading acknowledgment formulas from an outgoing message.

    Returns the original when what would remain is a fragment — better a
    formulaic message than a truncated one. Applied repeatedly so a chained
    opener ("Понял, спасибо за уточнение — ...") is fully removed.
    """
    text = message or ""
    for _ in range(3):
        stripped = _ACK_OPENER.sub("", text, count=1).strip()
        if stripped == text.strip():
            break
        if len(stripped) < _MIN_STRIPPED_CHARS:
            break
        if len(_WORDS.findall(stripped)) < _MIN_STRIPPED_WORDS:
            break
        text = stripped

    text = text.strip()
    if text and text[:1].islower() and (message or "").strip()[:1].isupper():
        text = text[0].upper() + text[1:]
    return text or message


def _qualifying_question(campaign) -> str:
    """Campaign override when an operator set one, else the repo default."""
    from linkedin.agents.follow_up_defaults import DEFAULT_QUALIFYING_QUESTION

    return (campaign.qualifying_question or "").strip() or DEFAULT_QUALIFYING_QUESTION


def _render_system_prompt(session, deal, recent_messages: list) -> str:
    """Render the agent system prompt from the Jinja2 template."""
    from django.utils import timezone

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    template = env.get_template("follow_up_agent.j2")

    campaign = deal.campaign
    self_prof = session.self_profile
    self_name = f"{self_prof.get('first_name', '')} {self_prof.get('last_name', '')}".strip() or session.django_user.username

    history = _load_deal_messages(deal)
    stage = _conversation_stage(history)
    logger.info(
        "follow_up stage for %s: %s (%d messages in this deal)",
        deal.lead.public_identifier, stage, len(history),
    )

    now = timezone.now()
    return template.render(
        self_name=self_name,
        conversation_stage=stage,
        qualifying_question=_qualifying_question(campaign),
        previous_openers=_previous_openers(history),
        product_docs=campaign.product_docs or "",
        campaign_objective=campaign.campaign_objective or "",
        booking_link=campaign.booking_link or "",
        profile_summary=_format_facts(deal.profile_summary),
        chat_summary=_format_facts(deal.chat_summary),
        recent_messages=_format_recent_messages(recent_messages, now),
        today=now.strftime("%Y-%m-%d"),
        days_since_last_outgoing=_days_since_last_outgoing(recent_messages, now),
        unanswered_outgoing=_count_unanswered_outgoing(recent_messages),
    )


def run_follow_up_agent(session, deal) -> FollowUpDecision:
    """Read conversation and return a structured follow-up decision.

    Sync chat first (which folds new messages into ``deal.chat_summary``),
    then render the prompt from the Deal's persistent summaries plus a small
    recency window of verbatim messages, and ask the LLM to decide.
    """
    from linkedin.db.chat import sync_conversation

    public_id = deal.lead.public_identifier
    sync_conversation(session, public_id)
    deal.refresh_from_db(fields=["chat_summary", "profile_summary"])
    _log_chat_facts(public_id, deal)

    recent = _load_recent_messages(deal)
    system_prompt = _render_system_prompt(session, deal, recent)

    from linkedin.llm import is_codex_provider
    if is_codex_provider():
        from linkedin.agents.codex_client import get_codex_client
        data = get_codex_client().chat_json(
            system_prompt=system_prompt,
            user_prompt="Respond with a JSON object matching the FollowUpDecision schema.",
            json_schema=FollowUpDecision.model_json_schema(),
        )
        decision = FollowUpDecision.model_validate(data)
    else:
        agent = Agent(
            get_llm_model(),
            output_type=FollowUpDecision,
            model_settings={"temperature": 0.7, "timeout": 60},
        )
        decision = run_agent(agent, system_prompt).output
        if decision is None:
            raise RuntimeError(f"LLM returned unparseable response for follow-up of {public_id}")

    if decision.action in ("send_message", "handoff") and decision.message:
        trimmed = _strip_ack_opener(decision.message)
        if trimmed != decision.message:
            logger.info("follow_up for %s: stripped acknowledgment opener", public_id)
            decision.message = trimmed

    logger.info("follow_up agent for %s: %s", public_id, decision.action)
    return decision


if __name__ == "__main__":
    from crm.models import Deal
    from linkedin.browser.registry import cli_parser, cli_session
    from linkedin.db.summaries import materialize_profile_summary_if_missing
    from linkedin.models import Task

    parser = cli_parser("Run the follow-up agent for a profile")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile", help="Public identifier of the target profile")
    group.add_argument("--task-id", type=int, help="Task ID to run the agent for")
    args = parser.parse_args()
    session = cli_session(args)
    session.ensure_browser()

    if args.task_id:
        task = Task.objects.get(pk=args.task_id)
        public_id = task.payload["public_id"]
        campaign_id = task.payload["campaign_id"]
        from linkedin.models import Campaign
        campaign = Campaign.objects.get(pk=campaign_id)
        session.campaign = campaign
    else:
        public_id = args.profile

    deal = (
        Deal.objects.filter(lead__public_identifier=public_id, campaign=session.campaign)
        .select_related("lead", "campaign")
        .first()
    )
    if not deal:
        logger.error("No Deal found for %s", public_id)
        raise SystemExit(1)

    logger.info("Running follow-up agent as %s for %s", session, public_id)
    logger.info("Campaign: %s", session.campaign)

    materialize_profile_summary_if_missing(deal, session)
    decision = run_follow_up_agent(session, deal)

    logger.info("Chat facts: %s", _format_facts(deal.chat_summary))
    logger.info("Action: %s", decision.action)
    if decision.message:
        logger.info("Message: %s", decision.message)
    if decision.outcome:
        logger.info("Outcome: %s", decision.outcome)
    logger.info("Follow-up in: %sh", decision.follow_up_hours)
