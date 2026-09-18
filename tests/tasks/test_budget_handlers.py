"""Как обработчики живут с исчерпанным бюджетом просмотров и с разносом отправок.

Исчерпанный бюджет — не сбой: задача переносится на момент, когда бюджет
освободится, и ничего не портит. Упавшую задачу reconcile пересоздал бы тут же,
и она упёрлась бы в ту же стену.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from crm.models import Deal
from linkedin.agents.follow_up import FollowUpDecision
from linkedin.browser.session import AccountSession
from linkedin.enums import ProfileState
from linkedin.exceptions import ProfileViewLimitReached
from linkedin.models import ActionLog, Campaign, Task
from linkedin.tasks.check_pending import handle_check_pending
from linkedin.tasks.connect import handle_connect
from linkedin.tasks.follow_up import SEND_GAP_SECONDS, handle_follow_up
from tests.tasks.test_tasks import (
    _build_context,
    _make_connected,
    _make_pending,
    _make_task,
)

SPENT = ProfileViewLimitReached(1800, "hourly profile-view budget spent (12)")


def _pending(task_type):
    return Task.objects.filter(task_type=task_type, status=Task.Status.PENDING)


def _delay(task):
    return (task.scheduled_at - timezone.now()).total_seconds()


@pytest.mark.django_db
class TestSpentBudget:
    @patch("linkedin.tasks.connect.strategy_for")
    def test_connect_waits_for_the_budget(self, mock_strategy, fake_session):
        mock_strategy.return_value.find_candidate.side_effect = SPENT
        Task.objects.all().delete()

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        retry = _pending(Task.TaskType.CONNECT).get()
        assert abs(_delay(retry) - 1800) < 60

    @patch("linkedin.actions.status.get_connection_status", side_effect=SPENT)
    def test_check_pending_is_postponed_not_answered(self, mock_status, fake_session):
        _make_pending(fake_session)
        Task.objects.all().delete()

        task = _make_task(
            Task.TaskType.CHECK_PENDING,
            {"campaign_id": fake_session.campaign.pk, "public_id": "alice", "backoff_hours": 48},
        )
        handle_check_pending(task, fake_session, _build_context(fake_session))

        retry = _pending(Task.TaskType.CHECK_PENDING).get()
        assert abs(_delay(retry) - 1800) < 60
        assert retry.payload["backoff_hours"] == 48  # шаг проверки не сбит
        deal = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        assert deal.state == ProfileState.PENDING

    @patch("linkedin.db.summaries.materialize_profile_summary_if_missing", side_effect=SPENT)
    @patch("linkedin.actions.message.send_raw_message", return_value=True)
    @patch("linkedin.agents.follow_up.run_follow_up_agent")
    def test_a_reply_does_not_wait_for_the_profile_budget(
            self, mock_agent, mock_send, mock_materialize, fake_session):
        mock_agent.return_value = FollowUpDecision(action="send_message", message="Hi", follow_up_hours=24)
        _make_connected(fake_session)

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk, "public_id": "alice"})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        mock_agent.assert_called_once()
        mock_send.assert_called_once()


@pytest.mark.django_db
class TestSendSpacing:
    @patch("linkedin.db.summaries.materialize_profile_summary_if_missing")
    @patch("linkedin.actions.message.send_raw_message", return_value=True)
    @patch("linkedin.agents.follow_up.run_follow_up_agent")
    def test_a_message_right_after_another_waits(self, mock_agent, mock_send, mock_materialize, fake_session):
        """Раньше 25 сообщений уходили за 18–30 минут каждое утро."""
        _make_connected(fake_session)
        sent = ActionLog.objects.create(
            linkedin_profile=fake_session.linkedin_profile, campaign=fake_session.campaign,
            action_type=ActionLog.ActionType.FOLLOW_UP,
        )
        ActionLog.objects.filter(pk=sent.pk).update(created_at=timezone.now() - timedelta(minutes=2))

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk, "public_id": "alice"})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        mock_agent.assert_not_called()  # ни токенов, ни черновика впустую
        mock_send.assert_not_called()
        retry = _pending(Task.TaskType.FOLLOW_UP).get()
        low, high = SEND_GAP_SECONDS
        assert low - 2 * 60 - 60 <= _delay(retry) <= high - 2 * 60 + 60

    @patch("linkedin.db.summaries.materialize_profile_summary_if_missing")
    @patch("linkedin.actions.message.send_raw_message", return_value=True)
    @patch("linkedin.agents.follow_up.run_follow_up_agent")
    def test_after_the_gap_it_goes(self, mock_agent, mock_send, mock_materialize, fake_session):
        mock_agent.return_value = FollowUpDecision(action="send_message", message="Hi", follow_up_hours=24)
        _make_connected(fake_session)
        sent = ActionLog.objects.create(
            linkedin_profile=fake_session.linkedin_profile, campaign=fake_session.campaign,
            action_type=ActionLog.ActionType.FOLLOW_UP,
        )
        ActionLog.objects.filter(pk=sent.pk).update(created_at=timezone.now() - timedelta(minutes=25))

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk, "public_id": "alice"})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        mock_send.assert_called_once()


@pytest.mark.django_db
def test_a_switched_off_campaign_is_invisible_to_the_daemon(fake_session):
    """Импорт Freemium при каждом старте заново добавляет пользователя в кампанию —
    поэтому выключают её флагом, а не членством."""
    off = Campaign.objects.create(name="Freemium Outreach", is_freemium=True, active=False)
    off.users.add(fake_session.django_user)

    session = AccountSession(fake_session.linkedin_profile)

    names = [campaign.name for campaign in session.campaigns]
    assert fake_session.campaign.name in names
    assert "Freemium Outreach" not in names
