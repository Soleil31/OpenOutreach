"""Потолок на дампы диагностики.

10.09.2026 шторм падений оставил 15 881 папку за одно рабочее окно, 4 сентября
— 97 559. Именно этот объём и заставил завести хостовый крон уборки, который
потом стирал улики каждые шесть часов, раньше чем их кто-то открывал.
"""
import pytest

from linkedin import diagnostics


@pytest.fixture(autouse=True)
def _clean_quota():
    diagnostics._recent_dumps.clear()
    yield
    diagnostics._recent_dumps.clear()


def test_first_failures_are_always_captured():
    """Первые падения — самые ценные, их терять нельзя."""
    assert all(diagnostics._quota_allows() for _ in range(diagnostics.MAX_DUMPS_PER_HOUR))


def test_a_storm_stops_at_the_quota():
    for _ in range(diagnostics.MAX_DUMPS_PER_HOUR):
        diagnostics._quota_allows()

    # Ещё пятнадцать тысяч попыток не должны оставить ни одной папки.
    assert not any(diagnostics._quota_allows() for _ in range(1000))
    assert len(diagnostics._recent_dumps) == diagnostics.MAX_DUMPS_PER_HOUR


def test_the_quota_refills_as_the_hour_rolls(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: clock["now"])

    for _ in range(diagnostics.MAX_DUMPS_PER_HOUR):
        diagnostics._quota_allows()
    assert diagnostics._quota_allows() is False

    clock["now"] += 3601  # час прошёл
    assert diagnostics._quota_allows() is True


def test_capture_is_skipped_without_touching_disk(monkeypatch, tmp_path):
    """При исчерпанном потолке capture_failure не должен ничего создавать."""
    monkeypatch.setattr(diagnostics, "DIAGNOSTICS_DIR", tmp_path)
    for _ in range(diagnostics.MAX_DUMPS_PER_HOUR):
        diagnostics._quota_allows()

    diagnostics.capture_failure(session=None, error=RuntimeError("Target crashed"))

    assert list(tmp_path.iterdir()) == []
