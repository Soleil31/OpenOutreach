# tests/tasks/test_handoff.py
"""Handing a hot lead to a human: state, silence and the spooled alert."""
import pytest
from unittest.mock import patch

from crm.models import Deal
from linkedin import handoff
from linkedin.agents.follow_up import FollowUpDecision
from linkedin.db.deals import set_profile_state
from linkedin.enums import ProfileState
from linkedin.models import ActionLog, Task
from linkedin.tasks.follow_up import handle_follow_up

from tests.tasks.test_tasks import _build_context, _make_connected, _make_task


def _deal(session, public_id="alice"):
    return Deal.objects.get(lead__public_identifier=public_id, campaign=session.campaign)


def _store(session, deal, *pairs):
    from datetime import timedelta

    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(deal.lead)
    for index, (content, is_outgoing) in enumerate(pairs):
        ChatMessage.objects.create(
            content_type=ct, object_id=deal.lead_id,
            content=content, is_outgoing=is_outgoing,
            owner=session.django_user, linkedin_urn=f"urn:handoff:{index}",
            creation_date=deal.creation_date + timedelta(minutes=index + 1),
        )


def _run(session, decision):
    task = _make_task(
        Task.TaskType.FOLLOW_UP,
        {"campaign_id": session.campaign.pk, "public_id": "alice"},
    )
    with patch("linkedin.agents.follow_up.run_follow_up_agent", return_value=decision), \
            patch("linkedin.db.summaries.materialize_profile_summary_if_missing"):
        handle_follow_up(task, session, _build_context(session))


HOLDING = "Отлично, давайте созвонимся — уточню слоты и вернусь к вам."


@pytest.mark.django_db
class TestHandoffDecision:
    def test_handoff_requires_a_holding_message(self):
        FollowUpDecision(action="handoff", message=HOLDING, follow_up_hours=0)
        with pytest.raises(ValueError):
            FollowUpDecision(action="handoff", follow_up_hours=0)

    def test_existing_validator_clauses_still_hold(self):
        with pytest.raises(ValueError):
            FollowUpDecision(action="send_message", follow_up_hours=1)
        with pytest.raises(ValueError):
            FollowUpDecision(action="mark_completed", follow_up_hours=1)


@pytest.mark.django_db
class TestHandleHandoff:
    @patch("linkedin.actions.message.send_raw_message", return_value=True)
    def test_sets_state_and_leaves_no_follow_up_task(self, mock_send, fake_session, tmp_path):
        _make_connected(fake_session)
        with patch.object(handoff, "SPOOL_DIR", str(tmp_path)):
            _run(fake_session, FollowUpDecision(
                action="handoff", message=HOLDING, follow_up_hours=24,
            ))

        assert _deal(fake_session).state == ProfileState.HANDOFF
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 1
        # The absent task IS the feature — the bot must not write again.
        assert not Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP, status=Task.Status.PENDING,
        ).exists()

    @patch("linkedin.actions.message.send_raw_message", return_value=False)
    def test_a_failed_send_still_reaches_a_human(self, mock_send, fake_session, tmp_path):
        _make_connected(fake_session)
        with patch.object(handoff, "SPOOL_DIR", str(tmp_path)):
            _run(fake_session, FollowUpDecision(
                action="handoff", message=HOLDING, follow_up_hours=24,
            ))

        deal = _deal(fake_session)
        # Never the send_message fallback to QUALIFIED — a hot lead is not
        # something to re-connect to.
        assert deal.state == ProfileState.HANDOFF
        assert "not sent" in deal.reason
        assert len(list(tmp_path.glob("deal-*.txt"))) == 1

    @pytest.mark.parametrize("state", [
        ProfileState.HANDOFF, ProfileState.COMPLETED, ProfileState.FAILED,
    ])
    @pytest.mark.parametrize("can_execute", [True, False])
    def test_a_stale_task_never_wakes_the_agent(self, state, can_execute, fake_session):
        """The guard sits above the rate limiter, so it cannot re-enqueue."""
        _make_connected(fake_session)
        set_profile_state(fake_session, "alice", state.value)
        Task.objects.all().delete()

        task = _make_task(
            Task.TaskType.FOLLOW_UP,
            {"campaign_id": fake_session.campaign.pk, "public_id": "alice"},
        )
        with patch("linkedin.agents.follow_up.run_follow_up_agent") as mock_agent, \
                patch.object(
                    type(fake_session.linkedin_profile), "can_execute",
                    return_value=can_execute,
                ):
            handle_follow_up(task, fake_session, _build_context(fake_session))

        mock_agent.assert_not_called()
        assert not Task.objects.filter(status=Task.Status.PENDING).exists()

    @patch("linkedin.actions.message.send_raw_message", return_value=True)
    def test_reconcile_never_revives_a_handed_over_deal(self, mock_send, fake_session, tmp_path):
        from linkedin.tasks.scheduler import reconcile

        _make_connected(fake_session)
        with patch.object(handoff, "SPOOL_DIR", str(tmp_path)):
            _run(fake_session, FollowUpDecision(
                action="handoff", message=HOLDING, follow_up_hours=24,
            ))
        Task.objects.all().delete()

        reconcile(fake_session)

        assert not Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP).exists()


@pytest.mark.django_db
class TestLiveConversationClamp:
    """A lead who just wrote must not wait three days for an answer."""

    @patch("linkedin.actions.message.send_raw_message", return_value=True)
    def test_pace_is_capped_while_the_lead_waits(self, mock_send, fake_session):
        from django.utils import timezone

        from linkedin.tasks.follow_up import LIVE_CONVERSATION_MAX_HOURS

        _make_connected(fake_session)
        _store(fake_session, _deal(fake_session), ("привет", True), ("Лучше созвониться", False))

        _run(fake_session, FollowUpDecision(
            action="send_message", message="Хорошо, когда удобно?", follow_up_hours=72,
        ))

        task = Task.objects.get(task_type=Task.TaskType.FOLLOW_UP, status=Task.Status.PENDING)
        ahead = (task.scheduled_at - timezone.now()).total_seconds()
        assert ahead <= LIVE_CONVERSATION_MAX_HOURS * 3600 + 60

    def test_the_agents_own_pace_is_kept_when_we_spoke_last(self, fake_session):
        """Nothing to answer, so the agent's chosen pace stands untouched."""
        from linkedin.tasks.follow_up import _follow_up_delay

        _make_connected(fake_session)
        deal = _deal(fake_session)
        _store(fake_session, deal, ("Лучше созвониться", False), ("привет", True))

        assert _follow_up_delay(deal, 72) == 72 * 3600


@pytest.mark.django_db
class TestAlertContents:
    def test_carries_what_a_human_needs_to_act(self, fake_session, tmp_path):
        _make_connected(fake_session)
        deal = _deal(fake_session)
        deal.profile_summary = {"facts": ["Закупщик в промышленном холдинге.", "Базируется в Алматы."]}
        deal.save()
        _store(
            fake_session, deal,
            ("А как вы сейчас возите оборудование?", True),
            ("Лучше, полагаю, созвониться", False),
        )

        with patch.object(handoff, "SPOOL_DIR", str(tmp_path)):
            assert handoff.notify_handoff(deal, list(_messages(deal)), account="NL")

        files = list(tmp_path.glob("deal-*.txt"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert "Лучше, полагаю, созвониться" in text
        assert "https://www.linkedin.com/in/alice/" in text
        assert "Закупщик в промышленном холдинге." in text
        assert "NL" in text
        assert len(text) <= handoff.MAX_ALERT_CHARS

    def test_survives_a_lead_with_no_facts_and_no_messages(self, fake_session, tmp_path):
        _make_connected(fake_session)
        deal = _deal(fake_session)

        with patch.object(handoff, "SPOOL_DIR", str(tmp_path)):
            assert handoff.notify_handoff(deal, [], account="")

        text = list(tmp_path.glob("deal-*.txt"))[0].read_text(encoding="utf-8")
        assert "https://www.linkedin.com/in/alice/" in text

    def test_an_unwritable_spool_does_not_break_the_daemon(self, fake_session, tmp_path):
        _make_connected(fake_session)
        deal = _deal(fake_session)

        blocked = tmp_path / "file-not-a-dir"
        blocked.write_text("x")
        with patch.object(handoff, "SPOOL_DIR", str(blocked / "spool")):
            assert handoff.notify_handoff(deal, [], account="NL") is False


def _messages(deal):
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(deal.lead)
    return ChatMessage.objects.filter(
        content_type=ct, object_id=deal.lead_id,
    ).order_by("creation_date", "pk")
