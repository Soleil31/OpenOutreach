# linkedin/tasks/follow_up.py
"""Follow-up task — runs the agentic follow-up for one CONNECTED profile."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from termcolor import colored

from linkedin.models import ActionLog

logger = logging.getLogger(__name__)

# Required silence between nudges scales with unanswered count:
# 1 unanswered → 3d, 2 → 6d, 3 → 9d. Skips the LLM call while open.
MIN_DAYS_PER_UNANSWERED = 3

# Ceiling on the agent's own pacing while the lead is the one waiting. The
# model sometimes picks 72h for a live exchange, and a hot lead that sits
# unread for three days is a lost one.
LIVE_CONVERSATION_MAX_HOURS = 8


def _build_send_profile(deal) -> dict:
    """Minimal profile dict for ``send_raw_message`` and its fallbacks.

    Populated from the Lead row — all three send strategies (popup,
    direct-thread, API) now navigate by URN so no human-readable name
    is required.
    """
    lead = deal.lead
    return {
        "public_identifier": lead.public_identifier,
        "urn": lead.urn or "",
    }


def _too_soon_to_nudge(deal) -> bool:
    """Wait `unanswered_count * MIN_DAYS_PER_UNANSWERED` days between nudges."""
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(type(deal.lead))
    messages = ChatMessage.objects.filter(content_type=ct, object_id=deal.lead_id)

    last = messages.order_by("-creation_date").first()
    if last is None or not last.is_outgoing:
        return False

    last_reply = messages.filter(is_outgoing=False).order_by("-creation_date").first()
    nudges = messages.filter(is_outgoing=True)
    if last_reply:
        nudges = nudges.filter(creation_date__gt=last_reply.creation_date)

    required = timedelta(days=nudges.count() * MIN_DAYS_PER_UNANSWERED)
    return timezone.now() - last.creation_date < required


def _lead_is_waiting(deal) -> bool:
    """True when the newest stored message came from the lead."""
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(type(deal.lead))
    last = (
        ChatMessage.objects
        .filter(content_type=ct, object_id=deal.lead_id)
        .order_by("-creation_date", "-pk")
        .first()
    )
    return last is not None and not last.is_outgoing


def _follow_up_delay(deal, hours: float) -> float:
    """Seconds until the next check, capped while the lead is waiting.

    Cannot fight ``_too_soon_to_nudge``, which only trips when the last
    message is ours — the exact case this clamp ignores.
    """
    if _lead_is_waiting(deal):
        hours = min(hours, LIVE_CONVERSATION_MAX_HOURS)
    return hours * 3600


def _deal_messages(deal) -> list:
    """Whole stored conversation for this lead, oldest first — for the alert."""
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(type(deal.lead))
    return list(
        ChatMessage.objects
        .filter(content_type=ct, object_id=deal.lead_id)
        .order_by("creation_date", "pk")
    )


def handle_follow_up(task, session, qualifiers):
    from crm.models import Deal
    from linkedin.actions.message import send_raw_message
    from linkedin.agents.follow_up import run_follow_up_agent
    from linkedin.db.deals import set_profile_state
    from linkedin.db.summaries import materialize_profile_summary_if_missing
    from linkedin.enums import ProfileState
    from linkedin.handoff import notify_handoff
    from linkedin.tasks.scheduler import enqueue_follow_up

    payload = task.payload
    public_id = payload["public_id"]
    campaign_id = payload["campaign_id"]

    logger.info(
        "[%s] %s %s",
        session.campaign, colored("\u25b6 follow_up", "green", attrs=["bold"]), public_id,
    )

    deal = (
        Deal.objects.filter(lead__public_identifier=public_id, campaign=session.campaign)
        .select_related("lead", "campaign")
        .first()
    )
    if deal is None:
        logger.warning("follow_up: no Deal for %s — skipping", public_id)
        return

    # Deliberately above the rate-limit check: a stale task on a closed or
    # handed-over deal must die here. Below it, a rate-limited day would
    # re-enqueue it at +1h forever, and a HANDOFF lead would get the bot
    # writing over the salesperson who took the conversation.
    if deal.state != ProfileState.CONNECTED:
        logger.info(
            "[%s] follow_up %s: deal is %s, not CONNECTED — dropping task",
            session.campaign, public_id, deal.state,
        )
        return

    # Rate limit check
    if not session.linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP):
        enqueue_follow_up(campaign_id, public_id, delay_seconds=3600)
        return

    if _too_soon_to_nudge(deal):
        logger.info("[%s] follow_up %s: too soon to nudge — re-enqueuing", session.campaign, public_id)
        enqueue_follow_up(campaign_id, public_id, delay_seconds=24 * 3600)
        return

    materialize_profile_summary_if_missing(deal, session)
    decision = run_follow_up_agent(session, deal)

    profile = _build_send_profile(deal)

    if decision.action == "handoff":
        # A hot lead reaches a human even when LinkedIn refuses the holding
        # message — so this deliberately does NOT fall back to QUALIFIED the
        # way send_message does.
        logger.info("[%s] follow_up handoff for %s: %s", session.campaign, public_id, decision.message)
        sent = send_raw_message(session, profile, decision.message)
        if sent:
            session.linkedin_profile.record_action(
                ActionLog.ActionType.FOLLOW_UP, session.campaign,
            )
        reason = (decision.message or "")[:300] if sent else "handoff (holding message not sent)"
        set_profile_state(session, public_id, ProfileState.HANDOFF.value, reason=reason)
        deal.refresh_from_db()
        notify_handoff(deal, _deal_messages(deal), account=str(session.linkedin_profile))
        logger.info(
            "[%s] %s HANDOFF — bot stays silent until a human acts",
            session.campaign, public_id,
        )
        # No enqueue_follow_up on purpose: the missing task is the feature.
        return

    if decision.action == "send_message":
        logger.info("[%s] follow_up message for %s: %s", session.campaign, public_id, decision.message)
        sent = send_raw_message(session, profile, decision.message)
        if not sent:
            set_profile_state(session, public_id, ProfileState.QUALIFIED.value)
            logger.warning("follow_up for %s: send failed — moving to QUALIFIED for re-connection", public_id)
            return
        session.linkedin_profile.record_action(
            ActionLog.ActionType.FOLLOW_UP, session.campaign,
        )
        enqueue_follow_up(
            campaign_id, public_id,
            delay_seconds=_follow_up_delay(deal, decision.follow_up_hours),
        )

    elif decision.action == "mark_completed":
        set_profile_state(session, public_id, ProfileState.COMPLETED.value, outcome=decision.outcome)
        logger.info("[%s] follow_up completed for %s: outcome=%s", session.campaign, public_id, decision.outcome)

    elif decision.action == "wait":
        enqueue_follow_up(
            campaign_id, public_id,
            delay_seconds=_follow_up_delay(deal, decision.follow_up_hours),
        )
