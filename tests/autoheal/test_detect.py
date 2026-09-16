"""Что автопочинка заводит в журнал, а что оставляет мониторингу.

С 31.08 по 16.09.2026 она завела 697 инцидентов: все с причиной `unknown`, все
«требует человека», ни одной попытки патча. Из них 367 — один и тот же след
сломанного сторожа браузера. Причин было две: состояние «требует человека»
считалось закрытым, поэтому дедуп не срабатывал и каждый прогон крона заводил
инцидент заново; и на смерть браузера, за которой и так следит мониторинг,
заводился инцидент.
"""
import datetime
import json
import pathlib

import pytest

from tools.autoheal import config, detect, incidents


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INCIDENTS_DIR", str(tmp_path / "incidents"))
    monkeypatch.setattr(config, "DIAGNOSTICS_DIR", str(tmp_path / "dumps"))
    monkeypatch.setattr(config, "ACCOUNT_STATE_FILE", str(tmp_path / "account.json"))
    return tmp_path


def _evidence(root: pathlib.Path, error: str, name: str = "2026-09-16_050000_Error"):
    package = root / "dumps" / name
    package.mkdir(parents=True)
    (package / "page.html").write_text("<html><body>лента</body></html>", encoding="utf-8")
    (package / "error.txt").write_text(error, encoding="utf-8")
    return package


def _incidents(root: pathlib.Path):
    return sorted((root / "incidents").glob("*/incident.json"))


class TestOneFailureOneIncident:
    def test_repeats_land_in_the_same_incident(self, journal):
        _evidence(journal, "ValueError: незнакомая поломка")

        first = detect.detect("NL")
        assert first is not None and first.data["repeats"] == 0

        for expected in (1, 2, 3):
            again = detect.detect("NL")
            assert again.id == first.id
            assert again.data["repeats"] == expected

        assert len(_incidents(journal)) == 1

    def test_history_stops_growing_once_a_human_is_called(self, journal):
        _evidence(journal, "ValueError: незнакомая поломка")

        for _ in range(4):
            incident = detect.detect("NL")

        assert incident.state == incidents.NEEDS_HUMAN
        # «обнаружен» и «требует человека» — и больше ничего.
        assert len(incident.data["history"]) == 2


class TestWhoOwnsWhat:
    @pytest.mark.parametrize("error", [
        "playwright._impl._errors.Error: Browser.new_context: Target crashed",
        "Page.evaluate: Connection closed while reading from the driver",
        "RuntimeError: can't start new thread",
        "AttributeError: 'NoneType' object has no attribute 'wait_for_load_state'",
    ])
    def test_browser_deaths_are_left_to_monitoring(self, journal, error):
        _evidence(journal, error)

        assert detect.detect("NL") is None
        assert _incidents(journal) == []

    def test_a_layout_break_is_still_ours(self, journal):
        _evidence(journal, "RuntimeError: No locator matched on https://www.linkedin.com/feed/")

        incident = detect.detect("NL")

        assert incident is not None
        assert incident.reason == "locator_break"
        assert incident.state == incidents.DETECTED

    def test_a_healthy_account_closes_nothing(self, journal):
        _evidence(journal, "RuntimeError: No locator matched on https://www.linkedin.com/feed/")
        (journal / "account.json").write_text(
            json.dumps({"status": "ok", "account": "acc"}), encoding="utf-8")

        assert detect.detect("NL") is None


class TestJournalPruning:
    def _write(self, journal, incident_id, age_days):
        created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=age_days)
        path = journal / "incidents" / incident_id
        path.mkdir(parents=True)
        (path / "incident.json").write_text(json.dumps({
            "id": incident_id, "server": "NL", "state": incidents.NEEDS_HUMAN,
            "created_at": created.isoformat(),
        }), encoding="utf-8")
        (path / "page.html").write_text("<html></html>", encoding="utf-8")

    def test_old_ones_go_and_the_newest_stay(self, journal):
        for day, incident_id in enumerate(["20260301-000001-aaa", "20260302-000001-bbb", "20260303-000001-ccc"]):
            self._write(journal, incident_id, age_days=90 - day)
        self._write(journal, "20260916-000001-yyy", age_days=0)
        self._write(journal, "20260915-000001-zzz", age_days=1)

        assert incidents.prune(keep=2, keep_days=14) == 3

        left = [path.parent.name for path in _incidents(journal)]
        assert left == ["20260915-000001-zzz", "20260916-000001-yyy"]

    def test_young_incidents_survive_even_beyond_the_limit(self, journal):
        self._write(journal, "20260916-000001-aaa", age_days=0)
        self._write(journal, "20260916-000002-bbb", age_days=1)

        assert incidents.prune(keep=1, keep_days=14) == 0
        assert len(_incidents(journal)) == 2


def test_evidence_scan_survives_a_dump_vanishing_mid_walk(journal, monkeypatch):
    """Крон уборки сносит дампы во время обхода — модуль падал на этом 6 раз."""
    _evidence(journal, "ValueError: неважно")

    def explode(self, pattern):
        raise FileNotFoundError(pattern)

    monkeypatch.setattr(pathlib.Path, "rglob", explode)

    assert detect.latest_evidence() is None
