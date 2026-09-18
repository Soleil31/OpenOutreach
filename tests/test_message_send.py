"""Отправка сообщения: отказ сети — не приговор лиду.

16.09.2026 прокси перестал грузить страницы, каждая отправка падала, и
обработчик follow_up откатил 34 живые сделки в Qualified «для
переподключения». Теперь «страница переписки не загрузилась» — отдельный исход,
который не путается с «этому человеку написать нельзя».
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from playwright.sync_api import Error as PlaywrightError

from linkedin.actions import message
from linkedin.exceptions import MessagingNetworkError

PROFILE = {"public_identifier": "alice", "urn": "urn:li:fsd_profile:ACoAAAlice"}


def _session():
    return SimpleNamespace(page=MagicMock(), wait=lambda *args, **kwargs: None)


def _page_does_not_load(*args, **kwargs):
    raise PlaywrightError("Page.goto: Timeout 30000ms exceeded.")


@pytest.fixture
def api_fallback():
    with patch.object(message, "_send_message_via_api", return_value=False) as fallback:
        yield fallback


@pytest.fixture
def dump():
    with patch.object(message, "dump_page_html") as dumped:
        yield dumped


class TestPageDidNotLoad:
    def test_it_is_reported_as_the_network_not_as_the_lead(self, api_fallback, dump):
        with patch.object(message, "goto_page", side_effect=_page_does_not_load):
            with pytest.raises(MessagingNetworkError):
                message.send_raw_message(_session(), PROFILE, "Добрый день")

        api_fallback.assert_called_once()
        dump.assert_not_called()  # страницы нет — сохранять нечего

    def test_the_api_fallback_still_gets_its_chance(self, api_fallback, dump):
        """16.09 мелкие вызовы Voyager проходили через прокси, страницы — нет."""
        api_fallback.return_value = True

        with patch.object(message, "goto_page", side_effect=_page_does_not_load):
            assert message.send_raw_message(_session(), PROFILE, "Добрый день") is True


class TestPageLoadedButNoComposeBox:
    def test_it_stays_an_ordinary_failure(self, api_fallback, dump):
        """Страница открылась, а написать некуда — это может быть сам лид, как и раньше."""
        no_compose = PlaywrightError("No selector matched for 'compose_input'")

        with patch.object(message, "goto_page"), \
                patch.object(message, "_find", side_effect=no_compose):
            assert message.send_raw_message(_session(), PROFILE, "Добрый день") is False

        dump.assert_called_once()
