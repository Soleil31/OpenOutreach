"""Черновой разбор: что кладётся в инцидент и чего он не делает.

Появился 16.09.2026. До него на всё, кроме смены вёрстки, модуль отвечал
«нужен человек» и замолкал — а человек шёл читать стеки руками.
"""
import json
import pathlib

import pytest

from tools.autoheal import config, diagnose, gateway, incidents


ANSWER = {
    "hypothesis": "Сторож закрывает Playwright из чужого потока, вызов не возвращается",
    "evidence": ["threads.txt: MainThread в greenlet_main", "resources.txt: browser processes: 3"],
    "suggested_diff": "--- a/linkedin/daemon.py\n+++ b/linkedin/daemon.py\n@@\n-close\n+kill\n",
    "how_to_verify": "тест с зависшим page.evaluate под сторожем",
    "confidence": "высокая",
}


@pytest.fixture
def incident(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INCIDENTS_DIR", str(tmp_path / "incidents"))
    opened = incidents.open_incident("NL", "acc", "wedge", "сторож счёл задачу зависшей")
    opened.attach("error.txt", "BrowserUnresponsiveError: watchdog fired on connect\n")
    opened.attach("threads.txt", "MainThread\n  greenlet_main\n")
    opened.attach("resources.txt", "pids: 300 / 400\nbrowser processes: 3\n")
    return opened


def _answer_with(monkeypatch, answer=None, boom=None):
    def fake_ask(system_prompt, user_prompt, schema, required):
        if boom is not None:
            raise boom
        fake_ask.prompt = user_prompt
        return answer
    monkeypatch.setattr(gateway, "ask", fake_ask)
    monkeypatch.setattr(diagnose.gateway, "ask", fake_ask)
    return fake_ask


class TestDraft:
    def test_the_draft_lands_next_to_the_evidence(self, incident, monkeypatch, tmp_path):
        asked = _answer_with(monkeypatch, ANSWER)

        assert diagnose.write_draft(incident, tmp_path) is True

        draft = (incident.path / "diagnosis.md").read_text()
        assert "Гипотеза" in draft and ANSWER["hypothesis"] in draft
        assert "черновик" in draft.lower()
        assert (incident.path / "suggested.patch").read_text().startswith("--- a/linkedin/daemon.py")
        # улики действительно доехали до модели
        assert "greenlet_main" in asked.prompt and "pids: 300 / 400" in asked.prompt

    def test_the_patch_is_never_applied(self, incident, monkeypatch, tmp_path):
        _answer_with(monkeypatch, ANSWER)

        diagnose.write_draft(incident, tmp_path)

        # единственное, что появилось — файлы внутри инцидента
        assert sorted(p.name for p in incident.path.iterdir()) == [
            "diagnosis.md", "error.txt", "incident.json", "resources.txt",
            "suggested.patch", "threads.txt",
        ]

    def test_one_draft_per_incident(self, incident, monkeypatch, tmp_path):
        _answer_with(monkeypatch, ANSWER)

        assert diagnose.write_draft(incident, tmp_path) is True
        assert diagnose.write_draft(incident, tmp_path) is False

    def test_a_dead_gateway_leaves_the_incident_open(self, incident, monkeypatch, tmp_path):
        _answer_with(monkeypatch, boom=gateway.GatewayUnavailable("шлюз недоступен"))

        assert diagnose.write_draft(incident, tmp_path) is False
        assert incident.data.get("diagnosed") is not True
        assert incident.attempts[-1]["verdict"] == "шлюз недоступен"
        assert not (incident.path / "diagnosis.md").exists()

    def test_missing_evidence_is_not_hidden(self, incident, monkeypatch, tmp_path):
        thin = dict(ANSWER, suggested_diff="", evidence=[], confidence="низкая")
        _answer_with(monkeypatch, thin)

        diagnose.write_draft(incident, tmp_path)

        draft = (incident.path / "diagnosis.md").read_text()
        assert "улик не хватило" in draft
        assert not (incident.path / "suggested.patch").exists()
        assert json.loads((incident.path / "incident.json").read_text())["diagnosis"]["has_patch"] is False


def test_the_log_tail_is_optional(monkeypatch):
    """Нет docker — нет хвоста журнала, но разбор всё равно должен состояться."""
    def explode(*args, **kwargs):
        raise OSError("docker: not found")

    monkeypatch.setattr(diagnose.subprocess, "run", explode)

    assert diagnose._daemon_log() == ""
    assert diagnose._git(pathlib.Path("/nonexistent")) == ""
