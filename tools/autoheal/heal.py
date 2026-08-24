# tools/autoheal/heal.py
"""Хилер: по уликам просит у модели новый слой селекторов.

Модель не получает ни доступа к базе, ни учётных данных, ни сети LinkedIn —
только трассировку, текущий слой селекторов и разметку страницы. Ответ обязан
прийти строго по схеме: свободный текст здесь не примут, потому что дальше его
проверяет машина, а не человек.

Возвращённый патч НЕ считается исправлением. Он считается предположением,
которое ещё должен подтвердить верификатор.
"""
from __future__ import annotations

import json
import pathlib
import re
import urllib.error
import urllib.request

from tools.autoheal import config

PATCH_SCHEMA = {
    "type": "object",
    "required": ["selectors_py", "changed_chains", "reasoning"],
    "additionalProperties": False,
    "properties": {
        "selectors_py": {
            "type": "string",
            "description": "Полное новое содержимое файла linkedin/browser/selectors.py",
        },
        "changed_chains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Имена изменённых наборов: email, password, submit, comply",
        },
        "reasoning": {
            "type": "string",
            "description": "Почему именно так, со ссылкой на разметку страницы",
        },
    },
}

SYSTEM_PROMPT = """Ты сопровождаешь слой селекторов для автоматизации LinkedIn.

LinkedIn сменил вёрстку, и правила поиска элементов перестали работать. Тебе
дают текущий файл селекторов, трассировку падения и разметку страницы, на
которой всё сломалось. Верни новое содержимое файла целиком.

Жёсткие правила:

1. Меняй ТОЛЬКО правила поиска и маркеры. Структура файла, имена переменных и
   набор экспортируемых имён должны остаться прежними — их импортирует другой код.
2. Ничего, кроме селекторов: ни обращений к сети, ни к файлам, ни к базе,
   ни учётных данных, ни импортов сверх уже имеющихся.
3. Правила упорядочены от точных к широким. Широкие обязаны остаться
   последними, иначе точные перестанут срабатывать.
4. Не удаляй существующие правила без нужды: они держат работу со старыми
   вёрстками, которые тоже проверяются.
5. Правила — функции от страницы (lambda p: ...), а не строки.
6. Учитывай, что в разметке может быть НЕСКОЛЬКО копий одной формы, из которых
   видна только одна. Правило, попадающее в скрытую копию, считается негодным.

Ответ — один JSON по заданной схеме, без пояснений вокруг."""


class HealerUnavailable(RuntimeError):
    """Шлюз недоступен или отказал — это не вина патча."""


def _trim_html(html: str, limit: int = 120_000) -> str:
    """Убирает стили и скрипты: модели нужна структура, а не мегабайт CSS."""
    html = re.sub(r"<style\b[^>]*>.*?</style>", "<style/>", html, flags=re.S | re.I)
    html = re.sub(r"<script\b[^>]*>.*?</script>", "<script/>", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    if len(html) > limit:
        head = html[: limit // 2]
        tail = html[-limit // 2:]
        html = head + "\n<!-- ВЫРЕЗАНА СЕРЕДИНА -->\n" + tail
    return html


def build_prompt(incident, selectors_source: str) -> str:
    page = incident.path / "page.html"
    error = incident.path / "error.txt"
    parts = [
        f"# Причина отказа\n{incident.reason}: {incident.data.get('detail', '')}",
        f"# Текущий файл linkedin/browser/selectors.py\n```python\n{selectors_source}\n```",
    ]
    if error.exists():
        trace = error.read_text(encoding="utf-8", errors="replace")
        parts.append(f"# Трассировка падения\n```\n{trace[-4000:]}\n```")
    if page.exists():
        markup = _trim_html(page.read_text(encoding="utf-8", errors="replace"))
        parts.append(f"# Разметка страницы, на которой сломалось\n```html\n{markup}\n```")
    previous = [a for a in incident.attempts if a.get("verdict") == "отклонён"]
    if previous:
        rejected = "\n".join(
            f"- попытка {a['n']}: {a.get('verdict_detail', 'не прошла проверку')}"
            for a in previous)
        parts.append(
            "# Уже отклонённые варианты — не повторяй их\n" + rejected)
    return "\n\n".join(parts)


def request_patch(incident, selectors_source: str) -> dict:
    """Просит патч у шлюза. Бросает HealerUnavailable, если шлюз недоступен."""
    payload = {
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt": build_prompt(incident, selectors_source),
        "json_response": True,
        "json_schema": PATCH_SCHEMA,
        "model": config.CODEX_MODEL,
    }
    request = urllib.request.Request(
        config.CODEX_GATEWAY_URL.rstrip("/") + "/v1/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if config.CODEX_GATEWAY_TOKEN:
        request.add_header("Authorization", "Bearer " + config.CODEX_GATEWAY_TOKEN)

    try:
        with urllib.request.urlopen(request, timeout=config.CODEX_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise HealerUnavailable(f"шлюз недоступен: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HealerUnavailable(f"шлюз ответил неожиданно: {exc}") from exc

    content = body.get("content", "")
    try:
        patch = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HealerUnavailable(f"ответ не разобрался как JSON: {exc}") from exc

    for field in ("selectors_py", "changed_chains", "reasoning"):
        if field not in patch:
            raise HealerUnavailable(f"в ответе нет поля {field}")
    return patch


def sanity_check(patch: dict) -> tuple[bool, str]:
    """Дешёвые проверки до запуска браузера.

    Верификатор дорогой, поэтому заведомо негодное отсекается здесь: файл,
    который не компилируется, потерял экспортируемые имена или притащил
    посторонние импорты, проверять незачем.
    """
    source = patch.get("selectors_py", "")
    if not source.strip():
        return False, "пустой файл"

    try:
        compile(source, "selectors.py", "exec")
    except SyntaxError as exc:
        return False, f"не компилируется: {exc}"

    required = ["EMAIL_LOCATORS", "PASSWORD_LOCATORS", "SUBMIT_LOCATORS",
                "COMPLY_LOCATORS", "CAPTCHA_MARKERS", "CHALLENGE_URL_MARKERS",
                "BLOCKED_MARKERS", "CREDENTIAL_MARKERS", "COMPLY_PROBE_TIMEOUT_MS"]
    missing = [name for name in required if f"{name} " not in source and f"{name}=" not in source]
    if missing:
        return False, f"пропали экспортируемые имена: {', '.join(missing)}"

    forbidden = re.findall(
        r"^\s*(?:import|from)\s+(os|sys|subprocess|socket|requests|urllib|pathlib|django)\b",
        source, flags=re.M)
    if forbidden:
        return False, f"посторонние импорты: {', '.join(sorted(set(forbidden)))}"

    for pattern, what in [(r"\bopen\s*\(", "работа с файлами"),
                          (r"\beval\s*\(", "eval"),
                          (r"\bexec\s*\(", "exec"),
                          (r"__import__", "динамический импорт")]:
        if re.search(pattern, source):
            return False, f"в слое селекторов недопустима {what}"

    return True, "предварительные проверки пройдены"


def load_current_selectors(repo: pathlib.Path) -> str:
    return (repo / config.WRITABLE_PATHS[0]).read_text(encoding="utf-8")
