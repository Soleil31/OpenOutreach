#!/usr/bin/env python3
"""Автопочинка: обнаружить → починить → проверить → выкатить → пронаблюдать.

Один проход. Запускается из крона; если чинить нечего, молча выходит.

    python -m tools.autoheal.run --repo /path/to/OpenOutreach
    python -m tools.autoheal.run --repo ... --dry-run    # без выкатки
    python -m tools.autoheal.run --status                # что происходит

Порядок шагов не переставлять. Выкатка разрешена ТОЛЬКО после зелёного
вердикта верификатора: генерация патчей без проверки — это автоматизированный
способ выкатывать непроверенное в рабочий контур.
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import subprocess
import sys
import time

from tools.autoheal import config, detect, heal, incidents, notify


def _verify(repo: pathlib.Path, candidate: pathlib.Path) -> tuple[bool, str]:
    """Гоняет верификатор на корпусе с подставленным кандидатом."""
    from tools.autoheal.verify import run_verifier
    return run_verifier(repo, candidate)


def _log(message: str) -> None:
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def handle(server: str, repo: pathlib.Path, dry_run: bool) -> int:
    incident = detect.detect(server)
    if incident is None:
        _log("поломок не обнаружено")
        return 0

    _log(f"инцидент {incident.id}: {incident.reason} — {incident.state}")

    if incident.state == incidents.NEEDS_HUMAN:
        notify.incident_needs_human(incident)
        _log("класс поломки не чинится кодом — позван человек")
        return 0

    if len(incident.attempts) >= config.MAX_HEAL_ATTEMPTS:
        incident.set_state(incidents.NEEDS_HUMAN,
                           f"исчерпан лимит попыток ({config.MAX_HEAL_ATTEMPTS})")
        notify.incident_needs_human(incident)
        return 1

    notify.incident_opened(incident)
    incident.set_state(incidents.HEALING, "запрошен патч")

    current = heal.load_current_selectors(repo)
    try:
        patch = heal.request_patch(incident, current)
    except heal.HealerUnavailable as exc:
        incident.add_attempt(verdict="шлюз недоступен", verdict_detail=str(exc))
        incident.set_state(incidents.DETECTED, f"шлюз недоступен: {exc}")
        _log(f"шлюз недоступен: {exc}")
        return 1

    ok, why = heal.sanity_check(patch)
    attempt = incident.add_attempt(
        changed_chains=patch.get("changed_chains", []),
        reasoning=patch.get("reasoning", "")[:2000],
        verdict="принят предварительно" if ok else "отклонён",
        verdict_detail=why,
    )
    incident.attach(f"candidate-{attempt['n']}.py", patch["selectors_py"])
    if not ok:
        _log(f"патч отклонён на предварительной проверке: {why}")
        return 1

    incident.set_state(incidents.VERIFYING, "проверка на корпусе")
    candidate = incident.path / f"candidate-{attempt['n']}.py"
    green, detail = _verify(repo, candidate)
    attempt["verdict"] = "подтверждён" if green else "отклонён"
    attempt["verdict_detail"] = detail
    incident.save()

    notify.verdict(incident, green, detail)
    if not green:
        _log(f"верификатор отклонил патч: {detail}")
        incident.set_state(incidents.DETECTED, "патч не прошёл проверку")
        return 1

    _log(f"верификатор подтвердил патч: {detail}")

    if dry_run:
        _log("--dry-run: выкатка пропущена")
        return 0

    from tools.autoheal import ship
    try:
        ship.check_allowed(server)
    except ship.DeployRefused as exc:
        incident.set_state(incidents.NEEDS_HUMAN, f"выкатка запрещена: {exc}")
        notify.deploy_refused(incident, str(exc))
        _log(f"выкатка запрещена: {exc}")
        return 0

    commit = ship.commit_to_repo(repo, incident, patch)
    incident.data["commit"] = commit
    incident.save()
    _log(f"зафиксировано в main: {commit}")

    since = datetime.datetime.utcnow().replace(microsecond=0).isoformat(sep=" ")
    deployment = ship.deploy(server, repo / config.WRITABLE_PATHS[0], incident)
    notify.deployed(incident, commit, server)
    _log(f"выкачено на {server}, наблюдаю {config.OBSERVE_SECONDS} с")

    time.sleep(config.OBSERVE_SECONDS)

    good, why = ship.observe(server, incident, since, incident.data.get("detail", ""))
    if good:
        incident.set_state(incidents.RESOLVED, why)
        notify.resolved(incident, why)
        _log(f"инцидент закрыт: {why}")
        return 0

    ship.rollback(server, deployment, incident, why)
    ship.revert_repo(repo)
    notify.rolled_back(incident, why)
    _log(f"откат: {why}")
    return 1


def show_status() -> int:
    print("Параметры:\n" + config.summary())
    print("\nПоследние инциденты:")
    found = incidents.recent(limit=10)
    if not found:
        print("  инцидентов не было")
    for incident in found:
        print(f"  {incident.id}  {incident.data['server']:<10} "
              f"{incident.reason:<16} {incident.state:<18} "
              f"попыток: {len(incident.attempts)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="путь к рабочей копии OpenOutreach")
    parser.add_argument("--server", default=None, help="сервер (по умолчанию первый из конфига)")
    parser.add_argument("--dry-run", action="store_true", help="дойти до вердикта, не выкатывать")
    parser.add_argument("--status", action="store_true", help="показать состояние и выйти")
    args = parser.parse_args()

    if args.status:
        return show_status()
    if not args.repo:
        parser.error("нужен --repo")

    server = args.server or config.TARGET_SERVERS[0]
    return handle(server, pathlib.Path(args.repo), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
