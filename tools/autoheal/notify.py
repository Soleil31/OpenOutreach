# tools/autoheal/notify.py
"""Уведомления об инцидентах.

Пишет в файл, который читает хостовый мониторинг, — тем же приёмом, что и
демон. Токены Telegram остаются на хосте и в этот модуль не попадают.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import tempfile

from tools.autoheal import config

STATE_PATH = os.environ.get("AUTOHEAL_NOTIFY_PATH", "/var/openoutreach-tmp/oo-autoheal-state.json")


def _write(kind: str, incident, text: str, **extra) -> None:
    payload = {
        "kind": kind,
        "incident": incident.id,
        "server": incident.data.get("server", ""),
        "account": incident.data.get("account", ""),
        "reason": incident.reason,
        "state": incident.state,
        "text": text,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        **extra,
    }
    try:
        directory = os.path.dirname(STATE_PATH) or "/tmp"
        os.makedirs(directory, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=directory, prefix=".autoheal-")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
        os.chmod(STATE_PATH, 0o644)
    except Exception:  # уведомление не должно ронять починку
        pass
    # дублируем в журнал инцидента, чтобы история была самодостаточной
    try:
        log = pathlib.Path(incident.path) / "notifications.log"
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{payload['at']}  {kind}: {text}\n")
    except Exception:
        pass


def incident_opened(incident) -> None:
    _write("обнаружено", incident,
           f"Сломался аутрич: {incident.reason}. {incident.data.get('detail','')}\n"
           f"Запускаю автопочинку.")


def incident_needs_human(incident) -> None:
    # Про куки — только когда дело во входе. 15.09.2026 зависший сторож дал
    # `unknown`, и совет импортировать куки отправил бы человека не туда.
    if incident.reason in config.HUMAN_ONLY_REASONS or incident.reason == "session_expired":
        action = "Демон дальше пробовать не будет. Нужен ручной вход и импорт кук."
    else:
        action = f"Это не вход и не вёрстка — разобрать по журналу инцидента: {incident.path}"

    # Черновая гипотеза едет прямо в сообщение: лежащий на диске разбор, о
    # котором никто не знает, ничем не лучше его отсутствия.
    diagnosis = incident.data.get("diagnosis") or {}
    draft = ""
    if diagnosis:
        draft = (f"\n\nЧерновая гипотеза (уверенность: {diagnosis.get('confidence', '?')}):\n"
                 f"{diagnosis.get('hypothesis', '')[:500]}\n"
                 f"Разбор целиком: {incident.path}/diagnosis.md")
        if diagnosis.get("has_patch"):
            draft += f"\nЧерновик правки (НЕ применён): {incident.path}/suggested.patch"

    _write("нужен человек", incident,
           f"Автопочинка неприменима: {incident.reason}.\n"
           f"{incident.data.get('detail','')}\n"
           f"{action}{draft}")


def repair_ready(incident, report: dict) -> None:
    """Готовая ветка с патчем, подтверждённым красным тестом."""
    _write("готова ветка", incident,
           f"Поломка {incident.reason} воспроизведена тестом, патч её закрывает,\n"
           f"остальной набор зелёный.\n\n"
           f"Ветка: {report.get('branch', '?')}\n"
           f"{report.get('detail', '')}\n\n"
           f"В main НЕ слито — смотреть и сливать руками.",
           branch=report.get("branch", ""))


def repair_failed(incident, report: dict) -> None:
    """Починка не вышла. Это нормальный исход, и о нём тоже надо знать."""
    _write("починка не вышла", incident,
           f"Поломка {incident.reason}: {report.get('state', '')}.\n"
           f"{report.get('detail', '')}\n\n"
           f"Разбор и улики: {incident.path}")


def verdict(incident, green: bool, detail: str) -> None:
    _write("вердикт", incident,
           ("Патч подтверждён верификатором.\n" if green
            else "Патч отклонён верификатором.\n") + detail[:700],
           green=green)


def deploy_refused(incident, why: str) -> None:
    _write("выкатка запрещена", incident,
           f"Патч готов и проверен, но выкатка не разрешена: {why}\n"
           f"Выкатить вручную или дождаться снятия ограничения.")


def deployed(incident, commit: str, server: str) -> None:
    _write("выкачено", incident,
           f"Патч {commit} выкачен на {server}. Наблюдаю за результатом.",
           commit=commit)


def resolved(incident, why: str) -> None:
    _write("починено", incident,
           f"Аутрич снова работает. {why}\n"
           f"Инцидент закрыт, откат не потребовался.")


def rolled_back(incident, why: str) -> None:
    _write("откат", incident,
           f"Патч не помог и откачен: {why}\n"
           f"Сервер и репозиторий возвращены к прежнему состоянию. Нужен человек.")
