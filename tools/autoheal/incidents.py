# tools/autoheal/incidents.py
"""Журнал инцидентов: что сломалось, что пробовали, чем кончилось.

Один инцидент — один каталог с JSON и приложенными уликами. Файлы, а не база:
журнал должен читаться человеком в момент разбора, без инструментов.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import uuid

from tools.autoheal import config

# состояния инцидента
DETECTED = "обнаружен"
HEALING = "чинится"
VERIFYING = "проверяется"
DEPLOYED = "выкачен"
OBSERVING = "наблюдается"
RESOLVED = "успешно"
ROLLED_BACK = "откачен"
NEEDS_HUMAN = "требует человека"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Incident:
    def __init__(self, data: dict, path: pathlib.Path):
        self.data = data
        self.path = path

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def state(self) -> str:
        return self.data["state"]

    @property
    def reason(self) -> str:
        return self.data.get("reason", "")

    @property
    def attempts(self) -> list:
        return self.data.setdefault("attempts", [])

    def set_state(self, state: str, note: str = "") -> None:
        self.data["state"] = state
        self.data.setdefault("history", []).append(
            {"at": _now(), "state": state, "note": note})
        self.save()

    def add_attempt(self, **fields) -> dict:
        attempt = {"n": len(self.attempts) + 1, "at": _now(), **fields}
        self.attempts.append(attempt)
        self.save()
        return attempt

    def save(self) -> None:
        self.data["updated_at"] = _now()
        (self.path / "incident.json").write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def attach(self, name: str, content: str) -> pathlib.Path:
        target = self.path / name
        target.write_text(content, encoding="utf-8")
        return target


def _root() -> pathlib.Path:
    root = pathlib.Path(config.INCIDENTS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def open_incident(server: str, account: str, reason: str, detail: str,
                  fingerprint: str = "") -> Incident:
    """Заводит инцидент. Если такой уже открыт — возвращает его, а не плодит новый."""
    existing = find_open(server, reason, fingerprint)
    if existing is not None:
        existing.data.setdefault("repeats", 0)
        existing.data["repeats"] += 1
        existing.save()
        return existing

    incident_id = f"{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    path = _root() / incident_id
    path.mkdir(parents=True, exist_ok=True)
    data = {
        "id": incident_id,
        "server": server,
        "account": account,
        "reason": reason,
        "detail": detail,
        "fingerprint": fingerprint,
        "state": DETECTED,
        "created_at": _now(),
        "repeats": 0,
        "attempts": [],
        "history": [{"at": _now(), "state": DETECTED, "note": detail}],
    }
    incident = Incident(data, path)
    incident.save()
    return incident


def find_open(server: str, reason: str = "", fingerprint: str = "") -> Incident | None:
    closed = {RESOLVED, ROLLED_BACK, NEEDS_HUMAN}
    for path in sorted(_root().glob("*/incident.json"), reverse=True):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["server"] != server or data["state"] in closed:
            continue
        if reason and data.get("reason") != reason:
            continue
        if fingerprint and data.get("fingerprint") != fingerprint:
            continue
        return Incident(data, path.parent)
    return None


def load(incident_id: str) -> Incident | None:
    path = _root() / incident_id / "incident.json"
    if not path.exists():
        return None
    return Incident(json.loads(path.read_text(encoding="utf-8")), path.parent)


def recent(limit: int = 20) -> list[Incident]:
    out = []
    for path in sorted(_root().glob("*/incident.json"), reverse=True)[:limit]:
        out.append(Incident(json.loads(path.read_text(encoding="utf-8")), path.parent))
    return out


def deploys_today(server: str) -> int:
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    count = 0
    for incident in recent(limit=200):
        if incident.data.get("server") != server:
            continue
        for event in incident.data.get("history", []):
            if event["state"] == DEPLOYED and event["at"].startswith(today):
                count += 1
    return count
