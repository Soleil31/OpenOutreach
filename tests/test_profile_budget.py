"""Бюджет на просмотр профилей.

18.09.2026 LinkedIn ограничил аккаунт NL: «it has accessed an unusually high
volume of LinkedIn profile data». Накануне бот открыл 742 профиля — около сотни
в час восемь часов подряд. Лимита не было: конвейер случайно тормозили медленный
фит модели, штормы браузера и зависания, и их починка сняла этот тормоз.
"""
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from linkedin import profile_budget
from linkedin.api.client import PlaywrightLinkedinAPI
from linkedin.exceptions import ProfileViewLimitReached
from linkedin.models import ActionLog, ProfileView


@pytest.fixture
def account(fake_session):
    profile = fake_session.linkedin_profile
    profile.profile_view_daily_limit = 5
    profile.profile_view_hourly_limit = 3
    profile.save()
    return profile


def _views(account, *ages_minutes):
    for age in ages_minutes:
        view = ProfileView.objects.create(linkedin_profile=account)
        ProfileView.objects.filter(pk=view.pk).update(
            created_at=timezone.now() - datetime.timedelta(minutes=age))


class TestBudget:
    def test_fresh_account_may_look(self, account):
        profile_budget.check(account)

    def test_the_hourly_cap_stops_a_burst(self, account):
        _views(account, 50, 20, 5)

        with pytest.raises(ProfileViewLimitReached) as spent:
            profile_budget.check(account)

        # Самый старый просмотр часа выйдет из окна через ~10 минут.
        assert 9 * 60 <= spent.value.retry_after <= 11 * 60
        assert "hourly" in spent.value.reason

    def test_old_views_leave_the_hourly_window(self, account):
        _views(account, 180, 120, 61)

        profile_budget.check(account)

    def test_the_daily_cap_waits_for_tomorrow(self, account):
        type(account).objects.filter(pk=account.pk).update(profile_view_hourly_limit=100)
        _views(account, 0, 0, 0, 0, 0)  # «сейчас» — всегда сегодня, в любое время суток

        with pytest.raises(ProfileViewLimitReached) as spent:
            profile_budget.check(account)

        assert "daily" in spent.value.reason
        now = timezone.now()
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        assert abs(spent.value.retry_after - (tomorrow - now).total_seconds()) < 60

    def test_an_admin_edit_applies_at_once(self, account):
        _views(account, 50, 20, 5)
        type(account).objects.filter(pk=account.pk).update(profile_view_hourly_limit=10)

        profile_budget.check(account)

    def test_views_are_not_outreach_actions(self, account):
        """deadman и отчёт клиенту читают ActionLog как «работа идёт»."""
        profile_budget.record(account)

        assert ProfileView.objects.count() == 1
        assert ActionLog.objects.count() == 0


class TestVoyagerCalls:
    def _api(self, fake_session):
        fake_session.page = MagicMock()
        fake_session.context = MagicMock()
        fake_session.context.cookies.return_value = [{"name": "JSESSIONID", "value": "ajax:1"}]
        return PlaywrightLinkedinAPI(session=fake_session)

    def test_no_request_leaves_once_the_budget_is_spent(self, fake_session, account):
        _views(account, 50, 20, 5)
        api = self._api(fake_session)

        with patch.object(api, "get") as request:
            with pytest.raises(ProfileViewLimitReached):
                api.get_profile(public_identifier="alice")

        request.assert_not_called()

    def test_every_request_is_counted(self, fake_session, account):
        api = self._api(fake_session)
        response = SimpleNamespace(status=200, ok=True, json=lambda: {})

        with patch.object(api, "get", return_value=response), \
                patch.object(api, "_check_profile_response"), \
                patch("linkedin.api.client.parse_linkedin_voyager_response", return_value={}):
            api.get_profile(public_identifier="alice")

        assert ProfileView.objects.filter(linkedin_profile=account).count() == 1

    def test_our_own_profile_is_not_browsing(self, fake_session, account):
        _views(account, 50, 20, 5)
        api = self._api(fake_session)
        response = SimpleNamespace(status=200, ok=True, json=lambda: {})

        with patch.object(api, "get", return_value=response), \
                patch.object(api, "_check_profile_response"), \
                patch("linkedin.api.client.parse_linkedin_voyager_response", return_value={}):
            api.get_profile(public_identifier="me")

        assert ProfileView.objects.filter(linkedin_profile=account).count() == 3
