# linkedin/account_state.py
"""Account health state shared with the host monitor.

The daemon runs inside the container and has no Telegram credentials — those
live on the host in ``/root/.monitor-env``.  So instead of notifying directly,
the daemon writes its account state to a JSON file under ``/tmp`` (bind-mounted
to ``/var/openoutreach-tmp`` on the host) and ``fire-monitor.sh`` turns it into
an alert.  Nothing here imports Django, so it is safe to call from anywhere.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

STATE_PATH = os.environ.get("OO_ACCOUNT_STATE_PATH", "/tmp/oo-account-state.json")

# status values
OK = "ok"
DEGRADED = "degraded"
PARKED = "parked"

# reason codes — kept stable, the host monitor renders them
REASON_TEXT = {
    "checkpoint_2fa": "LinkedIn запросил подтверждение входа (2ФА / checkpoint)",
    "captcha": "LinkedIn показал капчу при входе",
    "locator_break": "страница входа отрисовалась, но поля не найдены — сломались локаторы",
    "proxy_blocked": "LinkedIn не отдаёт страницу через прокси (999/403)",
    "bad_credentials": "LinkedIn отклонил логин или пароль",
    "session_expired": "сохранённая сессия протухла",
    "unknown": "вход не удался по неопознанной причине",
}


def describe(reason: str) -> str:
    return REASON_TEXT.get(reason, REASON_TEXT["unknown"])


class LoginBlocked(RuntimeError):
    """A login attempt failed for a reason we could name.

    Carries the classified ``reason`` so the daemon can decide whether another
    attempt makes any sense (a broken locator might fix itself on the next
    deploy; a 2FA challenge never will without a human).
    """

    def __init__(self, reason: str, detail: str = "", url: str = ""):
        self.reason = reason
        self.detail = detail
        self.url = url
        super().__init__(f"{reason}: {detail or describe(reason)}")

    # A challenge/captcha/credential problem cannot be retried by a machine.
    @property
    def needs_human(self) -> bool:
        return self.reason in {"checkpoint_2fa", "captcha", "bad_credentials"}


def write(status: str, *, account: str = "", reason: str = "", detail: str = "",
          attempts: int = 0, extra: dict | None = None) -> None:
    """Atomically write the current account state. Never raises."""
    payload = {
        "status": status,
        "account": account,
        "reason": reason,
        "reason_text": describe(reason) if reason else "",
        "detail": (detail or "")[:2000],
        "attempts": attempts,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    try:
        directory = os.path.dirname(STATE_PATH) or "/tmp"
        os.makedirs(directory, exist_ok=True)
        handle, tmp_path = tempfile.mkstemp(dir=directory, prefix=".oo-state-")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_PATH)
        os.chmod(STATE_PATH, 0o644)
    except Exception as exc:  # pragma: no cover - monitoring must never break the daemon
        logger.debug("Could not write account state: %s", exc)


def clear_ok(account: str = "") -> None:
    """Mark the account healthy again."""
    write(OK, account=account)
