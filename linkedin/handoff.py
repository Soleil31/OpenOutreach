# linkedin/handoff.py
"""Hot-lead handoff alerts, spooled for the host to deliver.

Same reasoning as ``account_state``: the daemon runs inside the container
and has no Telegram credentials — those live on the host in
``/root/.monitor-env``. So a handoff writes a ready-to-send message into a
spool directory under ``/tmp`` (bind-mounted to ``/var/openoutreach-tmp``
on the host) and ``fire-monitor.sh`` posts it.

The file is plain text, not JSON, on purpose: the host side is shell, and
Russian free text full of quotes and newlines is exactly what breaks a
``sed``/``grep`` JSON reader.
"""
from __future__ import annotations

import logging
import os
import tempfile

from linkedin import account_state

logger = logging.getLogger(__name__)

SPOOL_DIR = os.environ.get("OO_HANDOFF_SPOOL_DIR") or os.path.join(
    os.path.dirname(account_state.STATE_PATH) or "/tmp", "handoff",
)

# Telegram rejects a sendMessage body over 4096 characters outright.
MAX_ALERT_CHARS = 3500

# How much of the conversation a human needs to pick the thread up cold.
ALERT_MESSAGE_WINDOW = 6
ALERT_PROFILE_FACTS = 3


def profile_url(public_identifier: str) -> str:
    return f"https://www.linkedin.com/in/{public_identifier}/"


def _last_inbound(messages: list) -> str:
    for message in reversed(messages):
        if not message.is_outgoing and (message.content or "").strip():
            return message.content.strip()
    return ""


def build_alert(deal, messages: list, *, account: str = "") -> str:
    """Render the alert a human reads on their phone.

    ``Lead`` stores no human name — the profile facts and the URL are the
    only way to know who this is, so both go in.
    """
    public_id = deal.lead.public_identifier or "?"
    lines = [
        "🔥 Лид готов к разговору",
        f"Аккаунт: {account or '—'} · кампания: {deal.campaign.name}",
        "",
        f"Профиль: {profile_url(public_id)}",
    ]

    facts = (deal.profile_summary or {}).get("facts") or []
    for fact in facts[:ALERT_PROFILE_FACTS]:
        lines.append(f"• {fact}")

    inbound = _last_inbound(messages)
    if inbound:
        lines += ["", "Его слова:", f"«{inbound}»"]

    tail = [m for m in messages if (m.content or "").strip()][-ALERT_MESSAGE_WINDOW:]
    if tail:
        lines += ["", "Переписка:"]
        for message in tail:
            speaker = "Я" if message.is_outgoing else "Лид"
            lines.append(f"{speaker}: {message.content.strip()}")

    admin_base = os.environ.get("OO_ADMIN_BASE_URL", "").strip().rstrip("/")
    if admin_base:
        lines += ["", f"В админке: {admin_base}/admin/crm/deal/{deal.pk}/change/"]

    return "\n".join(lines)[:MAX_ALERT_CHARS]


def spool(deal, text: str) -> bool:
    """Atomically drop one alert file for the host to pick up.

    Never raises — a hot lead is already in the HANDOFF state by the time
    this runs, and losing the daemon over a full disk would be worse than
    losing the notification.
    """
    try:
        os.makedirs(SPOOL_DIR, exist_ok=True)
        path = os.path.join(SPOOL_DIR, f"deal-{deal.pk}.txt")
        handle, tmp_path = tempfile.mkstemp(dir=SPOOL_DIR, prefix=".oo-handoff-")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
        os.chmod(path, 0o644)
        logger.info("handoff alert spooled: %s", path)
        return True
    except Exception as exc:  # pragma: no cover - alerting must never break the daemon
        logger.warning("Could not spool handoff alert for deal %s: %s", deal.pk, exc)
        return False


def notify_handoff(deal, messages: list, *, account: str = "") -> bool:
    """Build and spool the alert for a deal that just entered HANDOFF."""
    return spool(deal, build_alert(deal, messages, account=account))
