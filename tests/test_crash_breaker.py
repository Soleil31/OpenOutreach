"""Предохранитель на падения браузера.

10.09.2026 Chromium начал отвечать `Page.evaluate: Target crashed`, каждая
задача connect падала, reconcile тут же создавал её заново, и демон повторял
одного и того же лида примерно раз в секунду: 15 881 упавшая задача за одно
рабочее окно, 97 559 за 4 сентября. Ничего не тормозило цикл, потому что
generic-except помечал задачу failed и шёл дальше без паузы.
"""
import pytest

from linkedin.daemon import _CrashBreaker


def _playwright_crash():
    return RuntimeError("Page.evaluate: Target crashed")


class TestCrashDetection:
    @pytest.mark.parametrize("message", [
        "Page.evaluate: Target crashed",
        "Target closed",
        "Browser has been closed",
        "Connection closed while reading from the driver",
        "Protocol error: Session closed",
    ])
    def test_browser_deaths_are_recognised(self, message):
        assert _CrashBreaker.looks_like_a_crash(RuntimeError(message)) is True

    @pytest.mark.parametrize("message", [
        "No locator matched the selector",
        "Lead has no public identifier",
        "rate limit reached",
    ])
    def test_ordinary_failures_are_not(self, message):
        assert _CrashBreaker.looks_like_a_crash(RuntimeError(message)) is False


class TestBackoff:
    def test_isolated_failures_cost_nothing(self):
        """Одиночные падения нормальны и не должны замедлять очередь."""
        breaker = _CrashBreaker("acc")
        for _ in range(_CrashBreaker.PATIENCE - 1):
            breaker.record_failure(_playwright_crash())
            assert breaker.backoff_seconds() == 0

    def test_a_storm_starts_costing_time(self):
        breaker = _CrashBreaker("acc")
        waits = []
        for _ in range(6):
            breaker.record_failure(_playwright_crash())
            waits.append(breaker.backoff_seconds())

        assert waits[: _CrashBreaker.PATIENCE - 1] == [0] * (_CrashBreaker.PATIENCE - 1)
        assert waits[-1] > 0
        # Растёт, а не топчется на месте.
        assert waits[-1] >= waits[_CrashBreaker.PATIENCE - 1]

    def test_backoff_is_capped(self):
        breaker = _CrashBreaker("acc")
        for _ in range(50):
            breaker.record_failure(_playwright_crash())
        assert breaker.backoff_seconds() == _CrashBreaker.BACKOFF_SECONDS[-1]

    def test_a_success_clears_the_counter(self):
        breaker = _CrashBreaker("acc")
        for _ in range(4):
            breaker.record_failure(_playwright_crash())
        assert breaker.backoff_seconds() > 0

        breaker.reset()

        assert breaker.consecutive == 0
        assert breaker.backoff_seconds() == 0


class TestParking:
    def test_does_not_park_on_a_handful(self):
        breaker = _CrashBreaker("acc")
        for _ in range(_CrashBreaker.MAX_CONSECUTIVE - 1):
            breaker.record_failure(_playwright_crash())
        assert breaker.tripped is False

    def test_parks_when_the_box_will_not_recover(self):
        """Без этого 15 881 падение за день проходило молча."""
        breaker = _CrashBreaker("acc")
        for _ in range(_CrashBreaker.MAX_CONSECUTIVE):
            breaker.record_failure(_playwright_crash())
        assert breaker.tripped is True

    def test_reason_is_named_for_the_monitor(self):
        breaker = _CrashBreaker("acc")
        breaker.record_failure(_playwright_crash())
        assert breaker.reason == "browser_crash"

        other = _CrashBreaker("acc")
        other.record_failure(RuntimeError("no locator matched"))
        assert other.reason == "task_failures"


class TestStormCost:
    def test_a_full_storm_is_minutes_not_a_day(self):
        """Считаем, во что обошёлся бы вчерашний шторм с предохранителем."""
        breaker = _CrashBreaker("acc")
        elapsed = 0
        attempts = 0
        # Шторм длился всё окно — 8 часов.
        while elapsed < 8 * 3600 and not breaker.tripped:
            breaker.record_failure(_playwright_crash())
            attempts += 1
            elapsed += breaker.backoff_seconds() + 1

        # Вместо 15 881 попытки — десяток, и дальше парковка с алертом.
        assert attempts <= _CrashBreaker.MAX_CONSECUTIVE
        assert breaker.tripped is True
