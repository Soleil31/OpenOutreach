"""Сторож против настоящего Chromium.

Юнит-тесты подменяют kill_browser. Здесь то, ради чего всё затевалось:
page.evaluate, который никогда не вернётся, в главном потоке и сторож в потоке
таймера. Сценарий идёт в отдельном процессе: если починка сломается, зависнет
он, а не весь набор тестов.
"""
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCENARIO = textwrap.dedent("""
    import os, sys, time
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")
    import django
    django.setup()

    from types import SimpleNamespace
    from playwright.sync_api import sync_playwright
    from linkedin import daemon
    from linkedin.browser.session import AccountSession

    session = AccountSession(SimpleNamespace(user=None, linkedin_username="watchdog-test"))
    session.playwright = sync_playwright().start()
    try:
        session.browser = session.playwright.chromium.launch(headless=True)
    except Exception as exc:
        print("SKIP", exc)
        session.playwright.stop()
        sys.exit(77)
    session.context = session.browser.new_context()
    session.page = session.context.new_page()
    daemon._hard_exit = lambda code: (print("HARD EXIT", flush=True), os._exit(3))

    started = time.monotonic()
    try:
        with daemon._Watchdog(1, session, "never-resolving evaluate", grace_s=30):
            session.page.evaluate("new Promise(() => {})")
    except Exception as exc:
        print(f"UNBLOCKED {time.monotonic() - started:.1f}s", flush=True)
    session.close()
    print("CLOSED", flush=True)

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    print("RELAUNCHED", browser.new_page().evaluate("1 + 1"), flush=True)
    browser.close()
    playwright.stop()
""")


def test_watchdog_unblocks_a_hung_evaluate_and_the_browser_comes_back():
    root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            [sys.executable, "-c", SCENARIO],
            cwd=root, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"scenario hung — the watchdog did not unblock it:\n{exc.stdout}")
    if result.returncode == 77:
        pytest.skip(result.stdout.strip())

    out = result.stdout
    assert result.returncode == 0, out + result.stderr[-2000:]
    unblocked = re.search(r"UNBLOCKED ([\d.]+)s", out)
    assert unblocked, out
    assert float(unblocked.group(1)) < 15
    assert "HARD EXIT" not in out
    assert "CLOSED" in out
    assert "RELAUNCHED 2" in out
