# tools/autoheal/verify.py
"""Запуск верификатора на кандидатном патче.

Кандидат подставляется вместо слоя селекторов в одноразовом контейнере, где
и гоняется проверка по корпусу. Рабочая копия при этом не трогается: патч,
который не прошёл, не должен оставлять следов.

Верификатор обязан уметь сказать «нет». Перед тем как доверять его зелёному,
убедись, что на текущем сломанном состоянии он даёт красный — раздел 12.3 спеки.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

from tools.autoheal import config

IMAGE = "europe-west3-docker.pkg.dev/gen-lang-client-0289784019/openoutreach/openoutreach:cexim-figma"


def run_verifier(repo: pathlib.Path, candidate: pathlib.Path | None = None,
                 corpus: str | None = None, timeout: int = 400) -> tuple[bool, str]:
    """Возвращает (зелёный, человекочитаемая причина)."""
    corpus_dir = corpus or config.CORPUS_DIR
    if not pathlib.Path(corpus_dir).exists():
        return False, f"корпус {corpus_dir} не найден — проверять не на чем"

    with tempfile.TemporaryDirectory() as tmp:
        selectors = pathlib.Path(tmp) / "selectors.py"
        if candidate is not None:
            shutil.copy2(candidate, selectors)
        else:
            shutil.copy2(repo / config.WRITABLE_PATHS[0], selectors)

        verifier = repo / "tools/selector_verifier/verify_locators.py"
        args = [
            "docker", "run", "--rm",
            "-v", f"{corpus_dir}:/corpus:ro",
            "-v", f"{verifier}:/tmp/v.py:ro",
            # слой поверхностей нужен верификатору внутри контейнера
            "-v", f"{repo / 'tools'}:/app/tools:ro",
            "-v", f"{selectors}:/app/linkedin/browser/selectors.py:ro",
            "-v", f"{repo / 'linkedin/browser/nav.py'}:/app/linkedin/browser/nav.py:ro",
            "-v", f"{repo / 'linkedin/browser/login.py'}:/app/linkedin/browser/login.py:ro",
            "-v", f"{repo / 'linkedin/account_state.py'}:/app/linkedin/account_state.py:ro",
            "--entrypoint", "python", IMAGE, "/tmp/v.py", "/corpus",
        ]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"верификатор не уложился в {timeout} с"

    output = (result.stdout or "") + (result.stderr or "")
    tail = "\n".join(line for line in output.strip().splitlines() if line.strip())[-1500:]
    return result.returncode == 0, tail


def self_check(repo: pathlib.Path) -> tuple[bool, str]:
    """Проверка пригодности самого верификатора: на сломанном коде — красный."""
    broken = pathlib.Path(tempfile.mkstemp(suffix=".py")[1])
    broken.write_text(
        "EMAIL_LOCATORS = [lambda p: p.locator('input#nonexistent-xyz')]\n"
        "PASSWORD_LOCATORS = [lambda p: p.locator('input#nonexistent-xyz')]\n"
        "SUBMIT_LOCATORS = [lambda p: p.locator('button#nonexistent-xyz')]\n"
        "COMPLY_LOCATORS = []\n"
        "COMPLY_PROBE_TIMEOUT_MS = 5000\n"
        "CHALLENGE_URL_MARKERS = ()\n"
        "CAPTCHA_MARKERS = ()\n"
        "BLOCKED_MARKERS = ()\n"
        "CREDENTIAL_MARKERS = ()\n"
        "NAMED_CHAINS = {}\n",
        encoding="utf-8")
    green, detail = run_verifier(repo, broken)
    broken.unlink(missing_ok=True)
    if green:
        return False, ("верификатор выдал зелёный на заведомо сломанных селекторах — "
                       "доверять ему нельзя")
    return True, "верификатор корректно отклоняет сломанные селекторы"
