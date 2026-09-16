# tools/autoheal/repair.py
"""Починка кода по красному тесту.

Патч без доказательства — это не починка, а надежда. Доказательство здесь одно:
тест, который ДО правки падает, а ПОСЛЕ проходит, при зелёном остальном наборе.

Отсюда главное свойство петли: она обязана уметь сказать «не воспроизвелось».
Если предложенный моделью тест зелёный сразу, значит поломку он не описывает, и
дальше идти нельзя — никакой патч после этого ничем не подтверждён.

Результат — ВЕТКА и сообщение человеку. В main ничего не уезжает: цена ошибки в
демоне, который пишет живым людям, выше стоимости одного слияния руками.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

from tools.autoheal import config, gateway

TEST_SCHEMA = {
    "type": "object",
    "required": ["test_source", "explanation"],
    "additionalProperties": False,
    "properties": {
        "test_source": {
            "type": "string",
            "description": "Полный текст файла с тестом на pytest, воспроизводящим поломку",
        },
        "explanation": {
            "type": "string",
            "description": "Что именно тест воспроизводит и почему он обязан упасть на текущем коде",
        },
    },
}

PATCH_SCHEMA = {
    "type": "object",
    "required": ["diff", "explanation"],
    "additionalProperties": False,
    "properties": {
        "diff": {
            "type": "string",
            "description": "Минимальная правка в формате unified diff, применимая git apply",
        },
        "explanation": {"type": "string"},
    },
}

TEST_PROMPT = """Ты воспроизводишь поломку рабочего демона тестом на pytest.

Тебе дают улики отказа и кусок кода, в котором он случился. Верни ОДИН файл с
тестом, который падает на текущем коде именно из-за этой поломки.

Жёсткие правила:

1. Тест обязан падать на текущем коде. Если поломку нельзя воспроизвести без
   LinkedIn, сети или живого браузера — верни пустой test_source и объясни, чего
   не хватает. Пустой ответ здесь честнее выдуманного теста.
2. Никаких обращений к сети, к LinkedIn и к рабочей базе. Только модули проекта,
   подмены через monkeypatch и временные каталоги pytest.
3. Тест должен проверять поведение, а не повторять реализацию: он останется в
   наборе навсегда и обязан ловить возврат этой же поломки.
4. Не трогай существующие тесты — верни только новый файл.
5. Имена и сообщения — по-русски, как в остальном наборе; assert — без лишних слов."""

PATCH_PROMPT = """Ты чинишь поломку рабочего демона.

Есть тест, который её воспроизводит и сейчас падает. Верни минимальную правку в
формате unified diff, после которой он пройдёт, а остальной набор останется
зелёным.

Жёсткие правила:

1. Минимальность. Правка в одном месте, без попутных улучшений и переименований.
2. Тест не трогать: доказательство нельзя править под патч.
3. Запрещено менять тексты сообщений и промпты агентов, миграции, настройки
   доступа и логику входа в аккаунт.
4. diff обязан применяться `git apply` от корня репозитория: заголовки
   `--- a/путь` и `+++ b/путь`, настоящие номера строк.
5. Не уверен — верни пустой diff и объясни, чего не хватает."""

# Куда патчу можно и куда нельзя. Запреты важнее разрешений: слева живые люди,
# которым демон пишет, справа — доступы и миграции, где цена ошибки не в тестах.
FORBIDDEN_PREFIXES = (
    "linkedin/templates/prompts/",
    "linkedin/agents/follow_up_defaults.py",
    "linkedin/django_settings",
    "tools/autoheal/",          # сам себя не правит
    ".github/",
)
FORBIDDEN_PATTERNS = (re.compile(r"(^|/)migrations/"),)
ALLOWED_PREFIXES = ("linkedin/", "crm/", "chat/")
MAX_DIFF_LINES = 80

REPRO_DIR = "tests/repro"


def _run(args, cwd=None, timeout=900, stdin=None) -> subprocess.CompletedProcess:
    """Единственная точка запуска внешних команд — её и подменяют тесты."""
    return subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, text=True,
                          timeout=timeout, check=False)


def run_suite(worktree: pathlib.Path, extra: str = "") -> tuple[bool, str]:
    """Гоняет набор в одноразовом контейнере из боевого образа.

    На хосте нет ни venv, ни зависимостей для тестов, зато есть образ, в котором
    лежит ровно тот же питон и те же библиотеки, что в бою. pytest ставится во
    временный каталог внутрь контейнера и умирает вместе с ним.
    """
    command = (
        "python -m pip install --quiet --target /tmp/pydeps "
        "pytest pytest-django pytest-mock factory-boy >/dev/null 2>&1; "
        f"PYTHONPATH=/tmp/pydeps python -m pytest {extra} -q -p no:cacheprovider"
    )
    result = _run([
        "docker", "run", "--rm", "--cpus", "1.5", "--memory", "2500m",
        "-u", "1000:1000", "-e", "HOME=/tmp", "-w", "/app",
        "-v", f"{worktree}:/app", "--entrypoint", "bash",
        config.TEST_IMAGE, "-lc", command,
    ], timeout=config.TEST_TIMEOUT_SECONDS)
    output = (result.stdout + result.stderr)[-6000:]
    return result.returncode == 0, output


def check_diff(diff: str) -> tuple[bool, str]:
    """Дешёвые запреты до применения патча."""
    if not diff.strip():
        return False, "пустой diff"

    touched = re.findall(r"^\+\+\+ b/(\S+)", diff, flags=re.M)
    if not touched:
        return False, "в diff не видно заголовков +++ b/путь"

    for path in touched:
        if path.startswith(FORBIDDEN_PREFIXES) or any(p.search(path) for p in FORBIDDEN_PATTERNS):
            return False, f"запрещённый путь: {path}"
        if not path.startswith(ALLOWED_PREFIXES):
            return False, f"путь вне разрешённой зоны: {path}"

    changed = [line for line in diff.splitlines()
               if (line.startswith("+") or line.startswith("-"))
               and not line.startswith(("+++", "---"))]
    if len(changed) > MAX_DIFF_LINES:
        return False, f"слишком большая правка: {len(changed)} строк при потолке {MAX_DIFF_LINES}"

    if re.search(r"^--- a/tests/", diff, flags=re.M):
        return False, "патч трогает тесты — доказательство нельзя править под патч"

    return True, f"затронуто файлов: {len(set(touched))}, строк: {len(changed)}"


def _worktree(repo: pathlib.Path, branch: str) -> pathlib.Path:
    path = pathlib.Path(config.WORKTREE_DIR) / branch.replace("/", "-")
    shutil.rmtree(path, ignore_errors=True)
    _run(["git", "worktree", "prune"], cwd=repo)
    created = _run(["git", "worktree", "add", "-b", branch, str(path), "HEAD"], cwd=repo)
    if created.returncode != 0:
        raise RuntimeError(f"не удалось создать рабочую копию: {created.stderr.strip()}")
    return path


def _context(incident, repo: pathlib.Path) -> str:
    from tools.autoheal import diagnose
    parts = [diagnose.build_prompt(incident, repo)]
    diagnosis = incident.data.get("diagnosis") or {}
    if diagnosis.get("hypothesis"):
        parts.append(f"# Рабочая гипотеза\n{diagnosis['hypothesis']}")
    return "\n\n".join(parts)


def attempt(incident, repo: pathlib.Path) -> dict:
    """Одна попытка починки. Возвращает отчёт; ветку создаёт только при успехе."""
    if not config.REPAIR_ENABLED:
        return {"state": "выключено", "detail": "AUTOHEAL_REPAIR=0"}
    if incident.data.get("repair_done"):
        return {"state": "уже пробовали", "detail": incident.data.get("repair_state", "")}

    branch = f"autoheal/{incident.id}"
    try:
        work = _worktree(repo, branch)
    except RuntimeError as exc:
        return {"state": "сорвалось", "detail": str(exc)}

    try:
        return _attempt_in(incident, repo, work, branch)
    finally:
        incident.data["repair_done"] = True
        incident.save()


def _attempt_in(incident, repo: pathlib.Path, work: pathlib.Path, branch: str) -> dict:
    context = _context(incident, repo)

    try:
        proposal = gateway.ask(TEST_PROMPT, context, TEST_SCHEMA,
                               ("test_source", "explanation"))
    except gateway.GatewayUnavailable as exc:
        return _finish(incident, "сорвалось", f"шлюз недоступен: {exc}")

    source = (proposal.get("test_source") or "").strip()
    if not source:
        return _finish(incident, "не воспроизводится",
                       proposal.get("explanation", "")[:500])

    test_path = pathlib.Path(REPRO_DIR) / f"test_{incident.id.replace('-', '_')}.py"
    (work / test_path).parent.mkdir(parents=True, exist_ok=True)
    (work / test_path.parent / "__init__.py").touch()
    (work / test_path).write_text(source + "\n", encoding="utf-8")
    incident.attach("repro_test.py", source)

    red, output = run_suite(work, extra=str(test_path))
    if red:
        # Тест прошёл на сломанном коде — значит, описывает не ту поломку.
        return _finish(incident, "не воспроизводится",
                       "предложенный тест зелёный на текущем коде — доказательства нет")

    for attempt_no in range(1, config.REPAIR_MAX_ATTEMPTS + 1):
        try:
            patch = gateway.ask(
                PATCH_PROMPT,
                f"{context}\n\n# Тест, который сейчас падает\n```python\n{source}\n```"
                f"\n\n# Вывод pytest\n```\n{output}\n```",
                PATCH_SCHEMA, ("diff", "explanation"))
        except gateway.GatewayUnavailable as exc:
            return _finish(incident, "сорвалось", f"шлюз недоступен: {exc}")

        diff = patch.get("diff") or ""
        ok, why = check_diff(diff)
        if not ok:
            output = f"патч отклонён до применения: {why}"
            incident.add_attempt(kind="починка", verdict="отклонён", verdict_detail=why)
            continue

        applied = _run(["git", "apply"], cwd=work, stdin=diff)
        if applied.returncode != 0:
            output = f"git apply не принял патч: {applied.stderr.strip()[:1000]}"
            incident.add_attempt(kind="починка", verdict="не применился",
                                 verdict_detail=output[:500])
            continue

        green, output = run_suite(work)
        incident.attach(f"attempt-{attempt_no}.patch", diff)
        if green:
            return _ship(incident, work, branch, diff, patch.get("explanation", ""))

        incident.add_attempt(kind="починка", verdict="набор красный",
                             verdict_detail=output[-500:])
        _run(["git", "checkout", "--", "."], cwd=work)

    return _finish(incident, "не починилось",
                   f"попыток: {config.REPAIR_MAX_ATTEMPTS}, набор так и не стал зелёным")


def _ship(incident, work: pathlib.Path, branch: str, diff: str, explanation: str) -> dict:
    message = (
        f"repro: fix {incident.reason} reproduced by a failing test\n\n"
        f"{explanation.strip()}\n\n"
        f"Автопочинка, инцидент {incident.id}. Тест падал до правки и проходит после,\n"
        f"остальной набор зелёный. В main не сливалось — решает человек.\n"
    )
    _run(["git", "add", "-A"], cwd=work)
    committed = _run(["git", "commit", "-m", message], cwd=work)
    if committed.returncode != 0:
        return _finish(incident, "сорвалось", f"коммит не удался: {committed.stdout[-300:]}")

    pushed = _run(["git", "push", "-u", "origin", branch], cwd=work)
    if pushed.returncode != 0:
        return _finish(incident, "ветка только локальная",
                       f"пуш не удался: {pushed.stderr.strip()[:300]}")

    return _finish(incident, "готова ветка", f"{branch}: {explanation.strip()[:400]}",
                   branch=branch, diff=diff)


def _finish(incident, state: str, detail: str, **extra) -> dict:
    incident.data["repair_state"] = state
    incident.data["repair"] = {"state": state, "detail": detail[:1000], **{
        key: value for key, value in extra.items() if key != "diff"}}
    incident.save()
    return {"state": state, "detail": detail, **extra}
