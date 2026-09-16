# tools/autoheal/diagnose.py
"""Черновой разбор отказа: гипотеза, улики и предлагаемая правка.

Ничего не применяет и никуда не выкатывает. Смысл один: к моменту, когда
человек откроет инцидент, за него уже прочитаны стеки потоков, счётчики
контейнера и хвост журнала, а рядом лежит черновик правки — принять или
выбросить.

Почему не «чинит само»: патч можно доверить машине только там, где есть чем
его доказать. У селекторов оракул есть — верификатор на корпусе сохранённых
страниц. У зависшего демона оракула нет, и «модель предложила — машина
выкатила» означало бы выкатывать непроверенное в переписку с живыми людьми.
"""
from __future__ import annotations

import pathlib
import subprocess

from tools.autoheal import config, gateway

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "required": ["hypothesis", "evidence", "suggested_diff", "how_to_verify", "confidence"],
    "additionalProperties": False,
    "properties": {
        "hypothesis": {
            "type": "string",
            "description": "Что именно сломалось и почему, одним абзацем",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Цитаты и ссылки на конкретные строки улик, подтверждающие гипотезу",
        },
        "suggested_diff": {
            "type": "string",
            "description": "Минимальная правка в формате unified diff или пустая строка",
        },
        "how_to_verify": {
            "type": "string",
            "description": "Чем проверить правку, не обращаясь к LinkedIn",
        },
        "confidence": {
            "type": "string",
            "enum": ["высокая", "средняя", "низкая"],
        },
    },
}

SYSTEM_PROMPT = """Ты разбираешь отказ рабочего демона LinkedIn-аутрича.

Тебе дают трассировку падения, счётчики контейнера, стеки всех потоков и хвост
журнала. Твоя задача — не починить, а понять и внятно изложить.

Жёсткие правила:

1. Не выдумывай. Каждый пункт evidence — цитата или ссылка на конкретную строку
   из данных выше. Нет подтверждения — так и напиши.
2. Улик не хватает — оставь suggested_diff пустой строкой и перечисли в
   how_to_verify, каких данных не хватило.
3. Правка минимальная и в одном месте. Не трогай тексты сообщений и промпты
   агентов (это переписка с живыми людьми), миграции, учётные данные и логику
   входа в аккаунт.
4. how_to_verify — способ проверить правку, не обращаясь к LinkedIn: тест,
   воспроизведение на копии, замер.
5. confidence ставь честно: «низкая», если гипотез несколько или улики
   допускают другое объяснение.

Ответ — один JSON по заданной схеме, без пояснений вокруг."""

_REQUIRED = ("hypothesis", "evidence", "suggested_diff", "how_to_verify", "confidence")


def _read(path: pathlib.Path, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _daemon_log(lines: int = 200) -> str:
    """Хвост журнала демона: модуль живёт на хосте, docker ему доступен."""
    try:
        result = subprocess.run(
            ["docker", "logs", "--since", "2h", "--tail", str(lines), config.DAEMON_CONTAINER],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout + result.stderr)[-12000:]


def _git(repo: pathlib.Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def build_prompt(incident, repo: pathlib.Path) -> str:
    parts = [
        f"# Отказ\n{incident.reason}: {incident.data.get('detail', '')}\n"
        f"Повторов с момента заведения: {incident.data.get('repeats', 0)}",
    ]

    for name, title, limit in (
        ("error.txt", "Трассировка падения", 6000),
        ("resources.txt", "Счётчики контейнера в момент отказа", 1000),
        ("threads.txt", "Стеки всех потоков", 14000),
    ):
        text = _read(incident.path / name, limit)
        if text.strip():
            parts.append(f"# {title}\n```\n{text}\n```")

    log = _daemon_log()
    if log.strip():
        parts.append(f"# Хвост журнала демона\n```\n{log}\n```")

    head = _git(repo, "log", "--oneline", "-5")
    if head:
        parts.append(f"# Последние коммиты\n```\n{head}\n```")

    return "\n\n".join(parts)


def _render(incident, answer: dict) -> str:
    evidence = "\n".join(f"- {item}" for item in answer.get("evidence", []))
    patch = (answer.get("suggested_diff") or "").strip()
    return "\n".join([
        f"# Черновой разбор инцидента {incident.id}",
        "",
        "**Это черновик.** Его написала модель по уликам. Никто его не проверял,",
        "ничего по нему не применено и не выкачено.",
        "",
        f"## Гипотеза (уверенность: {answer.get('confidence', '?')})",
        answer.get("hypothesis", ""),
        "",
        "## Чем подтверждается",
        evidence or "— модель не привела подтверждений",
        "",
        "## Чем проверить",
        answer.get("how_to_verify", ""),
        "",
        "## Предлагаемая правка",
        f"```diff\n{patch}\n```" if patch else "— улик не хватило, правка не предложена",
        "",
    ])


def write_draft(incident, repo: pathlib.Path) -> bool:
    """Кладёт разбор в инцидент. True, если черновик появился именно сейчас."""
    if incident.data.get("diagnosed"):
        return False
    if len(incident.attempts) >= config.MAX_MODEL_CALLS_PER_INCIDENT:
        return False

    try:
        answer = gateway.ask(SYSTEM_PROMPT, build_prompt(incident, repo),
                             DIAGNOSIS_SCHEMA, _REQUIRED)
    except gateway.GatewayUnavailable as exc:
        # Недоступный шлюз — не повод считать инцидент разобранным: попробуем
        # на следующем прогоне.
        incident.add_attempt(kind="разбор", verdict="шлюз недоступен",
                             verdict_detail=str(exc)[:500])
        return False

    incident.attach("diagnosis.md", _render(incident, answer))
    patch = (answer.get("suggested_diff") or "").strip()
    if patch:
        incident.attach("suggested.patch", patch + "\n")

    incident.add_attempt(kind="разбор", verdict="черновик готов",
                         verdict_detail=answer["hypothesis"][:500])
    incident.data["diagnosed"] = True
    incident.data["diagnosis"] = {
        "hypothesis": answer["hypothesis"][:1000],
        "confidence": answer.get("confidence", ""),
        "has_patch": bool(patch),
    }
    incident.save()
    return True
