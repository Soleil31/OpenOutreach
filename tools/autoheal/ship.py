# tools/autoheal/ship.py
"""Шиппер: фиксирует патч, выкатывает, наблюдает и откатывает.

Порядок обязателен: сначала репозиторий, потом сервер. Иначе на сервере
оказывается код, которого нет ни в одной ветке, и следующая пересборка образа
его молча теряет — ровно так и жили патчи до августа 2026.

Откат — не аварийная процедура, а штатный исход. Успехом считается только
появление настоящих действий в журнале; «ошибка больше не сыплется» успехом
не считается, потому что молчать умеет и мёртвый демон.
"""
from __future__ import annotations

import datetime
import hashlib
import pathlib
import subprocess

from tools.autoheal import config, incidents


class DeployRefused(RuntimeError):
    """Выкатка запрещена ограничителем — это не ошибка, а сработавшая защита."""


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=180, **kwargs)


def _ssh(server: str, command: str) -> subprocess.CompletedProcess:
    return _run(["ssh", "-o", "ConnectTimeout=25", server, command])


def check_allowed(server: str) -> None:
    """Проверяет ограничители раздела 14 спеки. Бросает DeployRefused."""
    now = datetime.datetime.now()
    if not config.ALLOW_NIGHT_DEPLOY:
        hour = now.hour
        night = (hour >= config.NIGHT_START_HOUR or hour < config.NIGHT_END_HOUR)
        if night:
            raise DeployRefused(
                f"ночная выкатка запрещена (сейчас {hour}:00, ночь "
                f"{config.NIGHT_START_HOUR}:00–{config.NIGHT_END_HOUR}:00)")

    done = incidents.deploys_today(server)
    if done >= config.MAX_DEPLOYS_PER_DAY:
        raise DeployRefused(
            f"суточный потолок выкаток исчерпан: {done} из {config.MAX_DEPLOYS_PER_DAY}")


def commit_to_repo(repo: pathlib.Path, incident, patch: dict) -> str:
    """Кладёт патч в main и возвращает хеш коммита."""
    target = repo / config.WRITABLE_PATHS[0]
    target.write_text(patch["selectors_py"], encoding="utf-8")

    chains = ", ".join(patch.get("changed_chains") or ["—"])
    message = (
        f"selectors: restore {chains} after a LinkedIn layout change\n"
        f"\n"
        f"{patch.get('reasoning', '').strip()}\n"
        f"\n"
        f"Автопочинка, инцидент {incident.id}. Причина: {incident.reason}.\n"
        f"Патч подтверждён верификатором на корпусе сохранённых страниц.\n"
    )
    _run(["git", "add", config.WRITABLE_PATHS[0]], cwd=repo)
    result = _run(["git", "commit", "-m", message], cwd=repo)
    if result.returncode != 0 and "nothing to commit" not in result.stdout:
        raise RuntimeError(f"коммит не удался: {result.stdout}{result.stderr}")

    push = _run(["git", "push", "origin", "main"], cwd=repo)
    if push.returncode != 0:
        raise RuntimeError(f"пуш не удался: {push.stderr}")

    return _run(["git", "rev-parse", "--short", "HEAD"], cwd=repo).stdout.strip()


def deploy(server: str, source: pathlib.Path, incident) -> dict:
    """Кладёт файл на сервер, сохранив предыдущую версию для отката."""
    remote = config.REMOTE_PATCH_PATH
    backup = f"{remote}.rollback-{incident.id}"

    _ssh(server, f"cp -a {remote} {backup} 2>/dev/null || true")

    copy = _run(["scp", "-q", str(source), f"{server}:{remote}"])
    if copy.returncode != 0:
        raise RuntimeError(f"не удалось скопировать файл: {copy.stderr}")

    local_sum = hashlib.md5(source.read_bytes()).hexdigest()
    remote_sum = _ssh(server, f"md5sum {remote}").stdout.split()[0:1]
    if not remote_sum or remote_sum[0] != local_sum:
        raise RuntimeError("файл на сервере не совпал с репозиторием — откатываю")

    restart = _ssh(server, f"docker restart {config.CONTAINER_NAME}")
    if restart.returncode != 0:
        raise RuntimeError(f"контейнер не перезапустился: {restart.stderr}")

    incident.set_state(incidents.DEPLOYED, f"{server}: {remote}")
    return {"server": server, "remote": remote, "backup": backup, "md5": local_sum}


def _successful_actions(server: str, since_iso: str) -> int:
    """Сколько настоящих действий появилось в журнале после выкатки."""
    script = (
        "docker exec openoutreach-admin python -c \""
        "import sqlite3;"
        "c=sqlite3.connect('file:/app/data/db.sqlite3?mode=ro',uri=True);"
        f"print(c.execute(\\\"select count(*) from linkedin_actionlog where created_at > '{since_iso}'\\\").fetchone()[0])"
        "\""
    )
    result = _ssh(server, script)
    try:
        return int(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def _error_signature_present(server: str, signature: str) -> bool:
    escaped = signature.replace("'", "")[:60]
    result = _ssh(
        server,
        f"docker logs --since {config.OBSERVE_SECONDS}s {config.CONTAINER_NAME} 2>&1 "
        f"| grep -c '{escaped}' || true")
    try:
        return int(result.stdout.strip().splitlines()[-1]) > 0
    except (ValueError, IndexError):
        return False


def observe(server: str, incident, since_iso: str, signature: str) -> tuple[bool, str]:
    """Итог наблюдения. Вызывать после паузы OBSERVE_SECONDS."""
    incident.set_state(incidents.OBSERVING, f"наблюдение {config.OBSERVE_SECONDS} с")

    if _error_signature_present(server, signature):
        return False, "исходная ошибка появилась снова"

    state = _ssh(server, "cat /var/openoutreach-tmp/oo-account-state.json 2>/dev/null || true")
    if '"parked"' in state.stdout:
        return False, "демон ушёл в парковку"

    actions = _successful_actions(server, since_iso)
    if actions < config.REQUIRED_SUCCESSFUL_ACTIONS:
        return False, (f"за время наблюдения появилось действий: {actions}, "
                       f"нужно {config.REQUIRED_SUCCESSFUL_ACTIONS}")

    return True, f"аккаунт выполнил действий: {actions}"


def rollback(server: str, deployment: dict, incident, why: str) -> None:
    remote, backup = deployment["remote"], deployment["backup"]
    _ssh(server, f"test -f {backup} && cp -a {backup} {remote}")
    _ssh(server, f"docker restart {config.CONTAINER_NAME}")
    incident.set_state(incidents.ROLLED_BACK, why)


def revert_repo(repo: pathlib.Path) -> None:
    """Откатывает последний коммит автопочинки, чтобы репозиторий и сервер сошлись."""
    _run(["git", "revert", "--no-edit", "HEAD"], cwd=repo)
    _run(["git", "push", "origin", "main"], cwd=repo)
