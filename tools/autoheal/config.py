# tools/autoheal/config.py
"""Параметры модуля автопочинки.

Значения по умолчанию выбраны так, чтобы модуль был осторожнее человека:
мало попыток, короткий поводок, обязательный откат. Меняются переменными
окружения — править код для настройки не нужно.
"""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "да"}


# --- сколько раз пробовать -------------------------------------------------

# Попыток получить рабочий патч на один инцидент. Больше трёх — это уже не
# «модель ошиблась», а «задача ей не по силам», и её надо отдать человеку.
MAX_HEAL_ATTEMPTS = _int("AUTOHEAL_MAX_ATTEMPTS", 3)

# Потолок автоматических выкаток в сутки на сервер. Защита от петли, в которой
# модуль чинит собственную поломку по кругу.
MAX_DEPLOYS_PER_DAY = _int("AUTOHEAL_MAX_DEPLOYS_PER_DAY", 2)

# --- наблюдение после выкатки ---------------------------------------------

# Сколько наблюдать за демоном после выкатки, прежде чем признать успех.
# 20 минут — это несколько полных циклов задач: достаточно, чтобы поломка
# успела повториться, и мало, чтобы не тянуть простой.
OBSERVE_SECONDS = _int("AUTOHEAL_OBSERVE_SECONDS", 1200)

# Сколько успешных действий должно появиться в журнале за это время.
# Одного достаточно: оно доказывает, что цепочка целиком работает.
REQUIRED_SUCCESSFUL_ACTIONS = _int("AUTOHEAL_REQUIRED_ACTIONS", 1)

# --- когда можно катить ----------------------------------------------------

# Ночные выкатки запрещены: если что-то пойдёт не так, разбираться будет некому,
# а откат хоть и автоматический, но последствия видит человек.
ALLOW_NIGHT_DEPLOY = _bool("AUTOHEAL_ALLOW_NIGHT", False)
NIGHT_START_HOUR = _int("AUTOHEAL_NIGHT_START", 23)
NIGHT_END_HOUR = _int("AUTOHEAL_NIGHT_END", 8)

# --- расходы ---------------------------------------------------------------

# Потолок обращений к модели на один инцидент. Считается по попыткам, а не по
# токенам: шлюз не возвращает расход, и врать про цифры не нужно.
MAX_MODEL_CALLS_PER_INCIDENT = _int("AUTOHEAL_MAX_MODEL_CALLS", 4)

# --- что и где ------------------------------------------------------------

# Единственный файл, который модулю разрешено изменять. Всё остальное —
# отклоняется без разбора содержания.
WRITABLE_PATHS = ("linkedin/browser/selectors.py",)

# Классы поломок, которые вообще подлежат автопочинке.
HEALABLE_REASONS = ("locator_break",)

# Классы, при которых немедленно зовут человека и ничего не чинят.
HUMAN_ONLY_REASONS = ("checkpoint_2fa", "captcha", "bad_credentials")

CORPUS_DIR = os.environ.get("AUTOHEAL_CORPUS", "/var/lib/openoutreach-corpus")
INCIDENTS_DIR = os.environ.get("AUTOHEAL_INCIDENTS", "/var/lib/openoutreach-incidents")
DIAGNOSTICS_DIR = os.environ.get(
    "AUTOHEAL_DIAGNOSTICS", "/var/openoutreach-tmp/openoutreach-diagnostics")
ACCOUNT_STATE_FILE = os.environ.get(
    "AUTOHEAL_ACCOUNT_STATE", "/var/openoutreach-tmp/oo-account-state.json")

CODEX_GATEWAY_URL = os.environ.get("CODEX_GATEWAY_URL", "http://127.0.0.1:18789")
CODEX_GATEWAY_TOKEN = os.environ.get("CODEX_GATEWAY_TOKEN", "")
CODEX_MODEL = os.environ.get("AUTOHEAL_MODEL", "gpt-5.5")
CODEX_TIMEOUT_SECONDS = _int("AUTOHEAL_MODEL_TIMEOUT", 300)

# Серверы, на которые модуль имеет право выкатывать. Начинаем с одного:
# расширять список — сознательное решение, а не побочный эффект.
TARGET_SERVERS = tuple(
    s.strip() for s in os.environ.get("AUTOHEAL_SERVERS", "Owlgram2").split(",") if s.strip()
)

CONTAINER_NAME = os.environ.get("AUTOHEAL_CONTAINER", "openoutreach")
REMOTE_PATCH_PATH = os.environ.get(
    "AUTOHEAL_REMOTE_PATH", "/home/conf/app/openoutreach/selectors_patched.py")


def summary() -> str:
    return "\n".join([
        f"попыток на инцидент:      {MAX_HEAL_ATTEMPTS}",
        f"выкаток в сутки:          {MAX_DEPLOYS_PER_DAY}",
        f"наблюдение после выкатки: {OBSERVE_SECONDS} с",
        f"успешных действий нужно:  {REQUIRED_SUCCESSFUL_ACTIONS}",
        f"ночные выкатки:           {'разрешены' if ALLOW_NIGHT_DEPLOY else 'запрещены'}"
        f" (ночь {NIGHT_START_HOUR}:00–{NIGHT_END_HOUR}:00)",
        f"обращений к модели:       не более {MAX_MODEL_CALLS_PER_INCIDENT} на инцидент",
        f"разрешено писать в:       {', '.join(WRITABLE_PATHS)}",
        f"чинится автоматически:    {', '.join(HEALABLE_REASONS)}",
        f"только человек:           {', '.join(HUMAN_ONLY_REASONS)}",
        f"серверы:                  {', '.join(TARGET_SERVERS)}",
    ])


if __name__ == "__main__":
    print(summary())
