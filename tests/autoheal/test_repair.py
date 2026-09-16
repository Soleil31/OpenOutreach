"""Починка по красному тесту: что петля делает и чего она делать не смеет.

Смысл петли в одном: патч принимается, только если поломка сперва доказана
падающим тестом. Поэтому самый важный тест здесь — не про успешную починку, а
про остановку, когда предложенный тест зелёный сразу.
"""
from types import SimpleNamespace

import pytest

from tools.autoheal import config, incidents, repair


GOOD_DIFF = (
    "--- a/linkedin/daemon.py\n"
    "+++ b/linkedin/daemon.py\n"
    "@@ -1,3 +1,3 @@\n"
    "-    session.close()\n"
    "+    reaper.kill_browser(session.playwright)\n"
)


@pytest.fixture
def incident(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INCIDENTS_DIR", str(tmp_path / "incidents"))
    monkeypatch.setattr(config, "REPAIR_ENABLED", True)
    monkeypatch.setattr(config, "REPAIR_MAX_ATTEMPTS", 2)
    opened = incidents.open_incident("NL", "acc", "wedge", "сторож счёл задачу зависшей")
    opened.attach("error.txt", "BrowserUnresponsiveError: watchdog fired on connect\n")
    return opened


class _Gateway:
    """Подставной шлюз: отдаёт заготовленные ответы по очереди."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, system_prompt, user_prompt, schema, required):
        self.prompts.append(user_prompt)
        return self.answers.pop(0)


def _wire(monkeypatch, tmp_path, answers, suite):
    gateway = _Gateway(*answers)
    monkeypatch.setattr(repair.gateway, "ask", gateway)

    results = list(suite)
    monkeypatch.setattr(repair, "run_suite", lambda work, extra="": results.pop(0))

    work = tmp_path / "work"
    (work / "tests").mkdir(parents=True)
    monkeypatch.setattr(repair, "_worktree", lambda repo, branch: work)

    calls = []

    def fake_run(args, cwd=None, timeout=900, stdin=None):
        calls.append(" ".join(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(repair, "_run", fake_run)
    monkeypatch.setattr(repair, "_context", lambda incident, repo: "улики")
    return gateway, calls, work


class TestProof:
    def test_a_test_that_passes_right_away_stops_everything(self, incident, monkeypatch, tmp_path):
        """Зелёный на сломанном коде тест ничего не доказывает — дальше нельзя."""
        gateway, calls, _ = _wire(
            monkeypatch, tmp_path,
            answers=[{"test_source": "def test_x(): assert True", "explanation": "ок"}],
            suite=[(True, "1 passed")],
        )

        report = repair.attempt(incident, tmp_path)

        assert report["state"] == "не воспроизводится"
        assert len(gateway.prompts) == 1, "патч запрашивать было нельзя"
        assert not [c for c in calls if "commit" in c or "push" in c]

    def test_an_honest_refusal_is_accepted(self, incident, monkeypatch, tmp_path):
        _wire(monkeypatch, tmp_path,
              answers=[{"test_source": "  ", "explanation": "нужен живой браузер"}],
              suite=[])

        report = repair.attempt(incident, tmp_path)

        assert report["state"] == "не воспроизводится"
        assert "браузер" in report["detail"]

    def test_red_turning_green_produces_a_branch(self, incident, monkeypatch, tmp_path):
        gateway, calls, work = _wire(
            monkeypatch, tmp_path,
            answers=[
                {"test_source": "def test_x(): assert False", "explanation": "падает"},
                {"diff": GOOD_DIFF, "explanation": "убиваем драйвер вместо close()"},
            ],
            suite=[(False, "1 failed"), (True, "379 passed")],
        )

        report = repair.attempt(incident, tmp_path)

        assert report["state"] == "готова ветка"
        assert report["branch"] == f"autoheal/{incident.id}"
        assert any("git commit" in c for c in calls)
        assert any("git push -u origin autoheal/" in c for c in calls)
        # тест доехал до рабочей копии и остался в инциденте
        assert (work / repair.REPRO_DIR / f"test_{incident.id.replace('-', '_')}.py").exists()
        assert (incident.path / "repro_test.py").exists()

    def test_it_gives_up_instead_of_guessing_forever(self, incident, monkeypatch, tmp_path):
        forbidden = GOOD_DIFF.replace("linkedin/daemon.py", "linkedin/templates/prompts/follow_up_agent.j2")
        gateway, calls, _ = _wire(
            monkeypatch, tmp_path,
            answers=[
                {"test_source": "def test_x(): assert False", "explanation": "падает"},
                {"diff": forbidden, "explanation": "правим промпт"},
                {"diff": forbidden, "explanation": "всё равно правим промпт"},
            ],
            suite=[(False, "1 failed")],
        )

        report = repair.attempt(incident, tmp_path)

        assert report["state"] == "не починилось"
        assert not [c for c in calls if "push" in c]

    def test_one_attempt_per_incident(self, incident, monkeypatch, tmp_path):
        _wire(monkeypatch, tmp_path,
              answers=[{"test_source": "", "explanation": "не воспроизвести"}],
              suite=[])

        repair.attempt(incident, tmp_path)
        second = repair.attempt(incident, tmp_path)

        assert second["state"] == "уже пробовали"

    def test_the_switch_is_honoured(self, incident, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "REPAIR_ENABLED", False)

        assert repair.attempt(incident, tmp_path)["state"] == "выключено"


class TestDiffGuards:
    @pytest.mark.parametrize("path, why", [
        ("linkedin/templates/prompts/follow_up_agent.j2", "переписка с живыми людьми"),
        ("linkedin/agents/follow_up_defaults.py", "тексты сообщений"),
        ("crm/migrations/0011_something.py", "миграции"),
        ("tools/autoheal/repair.py", "сам себя"),
        ("deploy/whatever.sh", "вне зоны"),
    ])
    def test_forbidden_paths(self, path, why):
        ok, detail = repair.check_diff(GOOD_DIFF.replace("linkedin/daemon.py", path))
        assert ok is False, why
        assert path in detail or "вне разрешённой зоны" in detail

    def test_a_patch_may_not_touch_the_proof(self):
        diff = ("--- a/tests/test_watchdog.py\n"
                "+++ b/linkedin/daemon.py\n"
                "@@ -1 +1 @@\n-a\n+b\n")
        ok, detail = repair.check_diff(diff)
        assert ok is False and "тесты" in detail

    def test_oversized_patches_are_refused(self):
        body = "".join(f"+строка {i}\n" for i in range(repair.MAX_DIFF_LINES + 5))
        ok, detail = repair.check_diff(
            "--- a/linkedin/daemon.py\n+++ b/linkedin/daemon.py\n@@ -1 +1 @@\n" + body)
        assert ok is False and "слишком большая" in detail

    def test_a_sane_patch_passes(self):
        ok, detail = repair.check_diff(GOOD_DIFF)
        assert ok is True and "затронуто файлов: 1" in detail
