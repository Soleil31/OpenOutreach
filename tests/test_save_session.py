"""Ручное восстановление сессии: когда считать, что человек уже вошёл.

18.09.2026 первый вариант скрипта ждал входа через time.sleep и page.url. В
синхронном Playwright адрес страницы обновляется только внутри вызовов
Playwright, поэтому человек уже сидел в ленте, а скрипт полчаса «ждал» и
закрыл бы окно с единственной живой сессией.
"""
from types import SimpleNamespace

import pytest

from linkedin.management.commands import save_session
from linkedin.management.commands.save_session import Command, _logged_in


@pytest.mark.parametrize("url, logged_in", [
    ("https://www.linkedin.com/feed/", True),
    ("https://www.linkedin.com/feed/foryou/", True),
    ("https://www.linkedin.com/in/eduard-fedoseenko/", True),
    ("https://www.linkedin.com/login/?session_redirect=/feed/", False),
    ("https://www.linkedin.com/login/?session_redirect=https%3A%2F%2Fwww.linkedin.com%2Ffeed%2F", False),
    ("https://www.linkedin.com/checkpoint/challenge/AgF?ut=1", False),
])
def test_what_counts_as_logged_in(url, logged_in):
    assert _logged_in([url]) is logged_in


class _Tabs:
    """Вкладки, чей адрес обновляется ТОЛЬКО внутри вызова Playwright —
    ровно так ведёт себя синхронный API."""

    def __init__(self, *urls):
        self._urls = list(urls)
        self.tab = SimpleNamespace(url=self._urls.pop(0))
        self.pages = [self.tab]
        self.polls = 0

    def wait_for_timeout(self, ms):
        self.polls += 1
        if self._urls:
            self.tab.url = self._urls.pop(0)


def test_the_wait_notices_the_feed(monkeypatch):
    monkeypatch.setattr(save_session, "POLL_SECONDS", 0)
    tabs = _Tabs(
        "https://www.linkedin.com/checkpoint/challenge/AgF",
        "https://www.linkedin.com/checkpoint/challenge/AgF",
        "https://www.linkedin.com/feed/foryou/",
    )

    assert Command._wait_for_feed(tabs, tabs, seconds=5) is True
    assert tabs.polls == 2


def test_a_tab_the_human_opened_counts_too(monkeypatch):
    monkeypatch.setattr(save_session, "POLL_SECONDS", 0)
    tabs = _Tabs("https://www.linkedin.com/checkpoint/challenge/AgF")
    tabs.pages.append(SimpleNamespace(url="https://www.linkedin.com/feed/"))

    assert Command._wait_for_feed(tabs, tabs, seconds=5) is True


def test_a_closed_window_ends_the_wait(monkeypatch):
    monkeypatch.setattr(save_session, "POLL_SECONDS", 0)

    class Closed:
        pages = []

        def wait_for_timeout(self, ms):
            raise RuntimeError("Target page, context or browser has been closed")

    assert Command._wait_for_feed(Closed(), Closed(), seconds=5) is False
