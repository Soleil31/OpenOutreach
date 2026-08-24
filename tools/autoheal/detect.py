# tools/autoheal/detect.py
"""Детектор: замечает поломку и решает, чинить её кодом или звать человека.

Читает то, что демон и так пишет: файл состояния аккаунта и пакеты диагностики.
Собственных обращений к LinkedIn не делает.

Главное правило — раздел 8.3 спеки: автопочинка запускается ТОЛЬКО для класса
«смена вёрстки». Всё прочее, включая неопознанное, идёт к человеку. Попытка
«починить кодом» отказ аккаунта означает продолжение автоматических действий
против площадки, которая уже выразила недоверие.
"""
from __future__ import annotations

import json
import pathlib
import re

from tools.autoheal import config, incidents


def read_account_state() -> dict:
    path = pathlib.Path(config.ACCOUNT_STATE_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def latest_evidence() -> pathlib.Path | None:
    """Свежайший пакет улик, в котором есть страница."""
    root = pathlib.Path(config.DIAGNOSTICS_DIR)
    if not root.exists():
        return None
    packages = sorted((p.parent for p in root.rglob("page.html")), reverse=True)
    return packages[0] if packages else None


def fingerprint_of(package: pathlib.Path) -> str:
    page = package / "page.html"
    if not page.exists():
        return ""
    # тот же отпечаток, что у архиватора — чтобы инцидент и образец в корпусе
    # можно было связать
    from tools.selector_verifier.archive_evidence import fingerprint
    return fingerprint(page.read_text(encoding="utf-8", errors="replace"))


def reason_from_evidence(package: pathlib.Path) -> tuple[str, str]:
    """Причина по тексту ошибки, если файл состояния молчит."""
    error_file = package / "error.txt"
    if not error_file.exists():
        return "unknown", "пакет улик без error.txt"
    text = error_file.read_text(encoding="utf-8", errors="replace")
    if "No locator matched" in text:
        match = re.search(r"No locator matched on (\S+)", text)
        where = match.group(1) if match else "неизвестной странице"
        return "locator_break", f"элементы не находятся на {where}"
    if "AuthenticationError" in text:
        return "session_expired", "LinkedIn ответил 401"
    last = [line for line in text.strip().splitlines() if line.strip()]
    return "unknown", (last[-1][:300] if last else "пустая трассировка")


def detect(server: str) -> incidents.Incident | None:
    """Возвращает инцидент, если есть что разбирать. Иначе None."""
    state = read_account_state()
    account = state.get("account", "")
    reason = state.get("reason", "")
    detail = state.get("detail", "")

    package = latest_evidence()
    if not reason and package is not None:
        reason, detail = reason_from_evidence(package)

    if not reason:
        return None

    # Здоровый аккаунт — закрывать нечего
    if state.get("status") == "ok":
        return None

    fingerprint = fingerprint_of(package) if package else ""
    incident = incidents.open_incident(server, account, reason, detail, fingerprint)

    if package is not None and not (incident.path / "page.html").exists():
        for name in ("page.html", "error.txt"):
            source = package / name
            if source.exists():
                incident.attach(name, source.read_text(encoding="utf-8", errors="replace"))
        incident.data["evidence_package"] = package.name
        incident.save()

    if reason in config.HUMAN_ONLY_REASONS:
        incident.set_state(
            incidents.NEEDS_HUMAN,
            f"класс «{reason}» не чинится кодом — нужен человек")
        return incident

    if reason not in config.HEALABLE_REASONS:
        incident.set_state(
            incidents.NEEDS_HUMAN,
            f"класс «{reason}» не входит в перечень чинимых автоматически")
        return incident

    return incident


if __name__ == "__main__":
    import sys
    server = sys.argv[1] if len(sys.argv) > 1 else config.TARGET_SERVERS[0]
    found = detect(server)
    if found is None:
        print("поломок не обнаружено")
    else:
        print(f"инцидент {found.id}: {found.reason} — {found.state}")
        print(f"  {found.data.get('detail','')}")
        print(f"  улики: {found.path}")
