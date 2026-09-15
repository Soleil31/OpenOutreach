"""Сторож задач: убить браузер, а если не помогло — выйти из процесса.

14 и 15.09.2026 демон NL молчал 18,5 и 13 часов. Сторож писал в лог «closing
browser», но закрывал браузер из потока таймера: синхронный Playwright на это
отвечает greenlet.error, ошибка глушилась на DEBUG, а главный поток так и ждал
ответа от page.evaluate. В контейнере при этом крутились три Chromium.
"""
import os
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from linkedin import daemon, diagnostics
from linkedin.browser import reaper
from linkedin.browser.session import AccountSession
from linkedin.exceptions import BrowserUnresponsiveError
from linkedin.models import Task


def _playwright(pid=None, returncode=None, dead=False, stop=None):
    proc = SimpleNamespace(pid=pid, returncode=returncode) if pid else None
    transport = SimpleNamespace(_proc=proc, on_error_future=SimpleNamespace(done=lambda: dead))
    return SimpleNamespace(
        _impl_obj=SimpleNamespace(_connection=SimpleNamespace(_transport=transport)),
        stop=stop or (lambda: None),
    )


def _wait_gone(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class TestReaper:
    def test_kill_reaches_the_whole_tree(self):
        """Chromium — внуки драйвера. Убить только драйвер — оставить их крутиться."""
        proc = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60 & wait"])
        try:
            deadline = time.monotonic() + 5
            while len(reaper.process_tree(proc.pid)) < 3 and time.monotonic() < deadline:
                time.sleep(0.05)
            tree = reaper.process_tree(proc.pid)
            assert len(tree) == 3

            assert reaper.kill_browser(_playwright(pid=proc.pid)) == 3
            proc.wait(timeout=5)
            assert all(_wait_gone(pid) for pid in tree[1:])
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_a_reaped_driver_is_left_alone(self):
        """Pid уже отпущенного драйвера может принадлежать чужому процессу."""
        bystander = subprocess.Popen(["sleep", "60"])
        try:
            assert reaper.kill_browser(_playwright(pid=bystander.pid, returncode=0)) == 0
            assert bystander.poll() is None
        finally:
            bystander.kill()

    def test_nothing_launched(self):
        assert reaper.kill_browser(None) == 0

    def test_driver_gone(self):
        assert reaper.driver_gone(None) is False
        assert reaper.driver_gone(_playwright(pid=1)) is False
        assert reaper.driver_gone(_playwright(pid=1, dead=True)) is True


def _session(playwright, context=None, browser=None):
    session = AccountSession(SimpleNamespace(user=None, linkedin_username="acc"))
    session.page = object()
    session.context, session.browser, session.playwright = context, browser, playwright
    return session


class TestSessionClose:
    def test_dead_driver_is_only_stopped(self):
        """context.close() на мёртвом драйвере ждёт вечно — проверено на живом Chromium."""
        calls = []

        def forbidden():
            raise AssertionError("context.close() must not be called on a dead driver")

        session = _session(
            _playwright(pid=1, dead=True, stop=lambda: calls.append("stop")),
            context=SimpleNamespace(close=forbidden),
            browser=SimpleNamespace(close=forbidden),
        )
        session.close()

        assert calls == ["stop"]
        assert session.page is session.context is session.browser is session.playwright is None

    def test_a_failing_step_does_not_skip_stop(self):
        """Пропущенный stop() — это вечное «Sync API inside the asyncio loop»."""
        calls = []

        def broken():
            calls.append("context")
            raise RuntimeError("Target closed")

        session = _session(
            _playwright(pid=1, stop=lambda: calls.append("stop")),
            context=SimpleNamespace(close=broken),
            browser=SimpleNamespace(close=lambda: calls.append("browser")),
        )
        session.close()

        assert calls == ["context", "browser", "stop"]


@pytest.fixture
def stages(monkeypatch):
    fired = SimpleNamespace(kills=[], exits=[])
    monkeypatch.setattr(reaper, "kill_browser", lambda playwright: fired.kills.append(playwright) or 5)
    monkeypatch.setattr(daemon, "_hard_exit", fired.exits.append)
    return fired


class TestWatchdog:
    def test_quick_work_triggers_nothing(self, stages):
        with daemon._Watchdog(0.2, SimpleNamespace(playwright=None), "quick", grace_s=0.2):
            pass
        time.sleep(0.6)
        assert stages.kills == [] and stages.exits == []

    def test_stuck_work_kills_the_browser_then_exits(self, stages):
        with daemon._Watchdog(0.05, SimpleNamespace(playwright=None), "stuck", grace_s=0.1) as watchdog:
            time.sleep(0.5)

        assert watchdog.fired.is_set()
        assert len(stages.kills) == 1
        assert stages.exits == [daemon.EXIT_WEDGED]


class TestRunTaskWithWatchdog:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch, tmp_path):
        monkeypatch.setitem(daemon.TASK_WATCHDOG_SECONDS, Task.TaskType.CONNECT, 0.1)
        monkeypatch.setattr(daemon, "WATCHDOG_EXIT_GRACE_SECONDS", 30)
        monkeypatch.setattr(diagnostics, "DIAGNOSTICS_DIR", tmp_path)
        diagnostics._recent_dumps.clear()

    def _task_and_session(self):
        closed_in = []
        session = SimpleNamespace(
            page=None, playwright=None,
            close=lambda: closed_in.append(threading.current_thread().name),
        )
        return SimpleNamespace(task_type=Task.TaskType.CONNECT), session, closed_in

    def test_a_blocked_handler_is_unwedged_and_torn_down_in_its_own_thread(self, monkeypatch):
        released = threading.Event()
        monkeypatch.setattr(reaper, "kill_browser", lambda playwright: released.set() or 7)

        def handler(task, session, qualifiers):
            assert released.wait(5), "watchdog never killed the browser"
            raise RuntimeError("Page.evaluate: Connection closed while reading from the driver")

        task, session, closed_in = self._task_and_session()
        with pytest.raises(BrowserUnresponsiveError):
            daemon.run_task_with_watchdog(handler, task, session, {})

        assert closed_in == [threading.current_thread().name]

    def test_ordinary_failures_pass_through(self, stages):
        def handler(task, session, qualifiers):
            raise ValueError("No locator matched")

        task, session, closed_in = self._task_and_session()
        with pytest.raises(ValueError):
            daemon.run_task_with_watchdog(handler, task, session, {})

        assert closed_in == [] and stages.kills == []
