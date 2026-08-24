# tests/test_auth_breaker.py
"""Guards against the login treadmill.

On 10.08.2026 the NL account lost its session. The daemon answered every
``AuthenticationError`` by wiping the saved cookies, trying to log in, failing,
marking the task failed and letting ``reconcile`` immediately queue another —
about 1300 login attempts in two days and ~105k failed tasks in two weeks,
which is a good way to lose the account outright.

These tests pin the two properties that stop that happening again:
a counter that gives up, and a saved session that is never destroyed by an
attempt that did not succeed.
"""
import json

import pytest

from linkedin import account_state
from linkedin.account_state import LoginBlocked
from linkedin.daemon import _AuthBreaker


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """Keep the state file out of /tmp so tests never touch the real one."""
    path = tmp_path / "oo-account-state.json"
    monkeypatch.setattr(account_state, "STATE_PATH", str(path))
    return path


class TestAuthBreaker:
    def test_backoff_grows_with_each_failure(self):
        breaker = _AuthBreaker("a@example.com")
        seen = []
        for _ in range(3):
            breaker.record_failure("session_expired", "boom")
            seen.append(breaker.backoff_seconds())
        assert seen == [60, 300, 1800]

    def test_trips_after_three_consecutive_failures(self):
        breaker = _AuthBreaker("a@example.com")
        breaker.record_failure("session_expired", "boom")
        assert not breaker.tripped
        breaker.record_failure("session_expired", "boom")
        assert not breaker.tripped
        breaker.record_failure("session_expired", "boom")
        assert breaker.tripped

    def test_a_success_clears_the_counter(self):
        breaker = _AuthBreaker("a@example.com")
        breaker.record_failure("session_expired", "boom")
        breaker.record_failure("session_expired", "boom")
        breaker.reset()
        assert breaker.consecutive == 0
        assert not breaker.tripped

    def test_backoff_never_runs_off_the_end_of_the_table(self):
        breaker = _AuthBreaker("a@example.com")
        for _ in range(10):
            breaker.record_failure("session_expired", "boom")
        assert breaker.backoff_seconds() == 1800

    def test_failure_is_published_for_the_host_monitor(self, state_file):
        breaker = _AuthBreaker("a@example.com")
        breaker.record_failure("checkpoint_2fa", "challenge url")
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        assert payload["status"] == account_state.DEGRADED
        assert payload["reason"] == "checkpoint_2fa"
        assert payload["account"] == "a@example.com"
        assert payload["attempts"] == 1
        assert payload["reason_text"]          # human-readable text for Telegram

    def test_third_failure_publishes_parked(self, state_file):
        breaker = _AuthBreaker("a@example.com")
        for _ in range(3):
            breaker.record_failure("session_expired", "boom")
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        assert payload["status"] == account_state.PARKED
        assert payload["attempts"] == 3


class TestLoginBlocked:
    @pytest.mark.parametrize("reason", ["checkpoint_2fa", "captcha", "bad_credentials"])
    def test_reasons_a_machine_cannot_fix_demand_a_human(self, reason):
        assert LoginBlocked(reason, "x").needs_human

    @pytest.mark.parametrize("reason", ["locator_break", "proxy_blocked", "session_expired", "unknown"])
    def test_other_reasons_may_be_retried(self, reason):
        assert not LoginBlocked(reason, "x").needs_human


@pytest.mark.django_db
class TestSessionSurvivesAFailedLogin:
    def test_reauthenticate_does_not_clear_cookies_before_trying(self, fake_session, monkeypatch):
        """A failed re-login must leave the stored session untouched.

        This is what made a hand-imported session impossible to keep: the old
        implementation nulled ``cookie_data`` first, so ninety seconds after a
        human logged in, the daemon had already thrown the cookies away.
        """
        from linkedin.browser.session import AccountSession

        profile = fake_session.linkedin_profile
        profile.cookie_data = {"cookies": [{"name": "li_at", "value": "keep-me"}]}
        profile.save(update_fields=["cookie_data"])

        session = AccountSession(profile)

        def explode(session, force_login=False):
            raise LoginBlocked("checkpoint_2fa", "LinkedIn asked for a code")

        monkeypatch.setattr("linkedin.browser.login.start_browser_session", explode)

        with pytest.raises(LoginBlocked):
            session.reauthenticate()

        profile.refresh_from_db(fields=["cookie_data"])
        assert profile.cookie_data == {"cookies": [{"name": "li_at", "value": "keep-me"}]}
