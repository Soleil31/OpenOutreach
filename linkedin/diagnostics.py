# linkedin/diagnostics.py
"""Capture page state on automation failures for post-mortem debugging."""
from __future__ import annotations

import faulthandler
import logging
import pathlib
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime

from linkedin.conf import DIAGNOSTICS_DIR

logger = logging.getLogger(__name__)

# A crash storm writes one dump per failed task — an HTML page and a screenshot
# each. On 2026-09-10 that was 15 881 folders in a single working window, and
# 97 559 on 2026-09-04. The tenth copy of the same traceback teaches nobody
# anything, and that volume is exactly why a host cleanup cron had to exist —
# which then deleted the evidence every six hours before anyone read it.
MAX_DUMPS_PER_HOUR = 12

_recent_dumps: list[float] = []


def _quota_allows() -> bool:
    """True while this hour still has room for another dump."""
    now = time.monotonic()
    cutoff = now - 3600
    _recent_dumps[:] = [stamp for stamp in _recent_dumps if stamp > cutoff]
    if len(_recent_dumps) >= MAX_DUMPS_PER_HOUR:
        return False
    _recent_dumps.append(now)
    return True


def capture_failure(session, error: BaseException) -> None:
    """Save page HTML, screenshot, and error details into a per-failure folder."""
    if not _quota_allows():
        logger.debug(
            "Diagnostics quota reached (%d/hour) — not dumping this failure",
            MAX_DUMPS_PER_HOUR,
        )
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    error_name = type(error).__name__
    folder = DIAGNOSTICS_DIR / f"{timestamp}_{error_name}"
    folder.mkdir(parents=True, exist_ok=True)

    # Error traceback
    tb = traceback.format_exception(type(error), error, error.__traceback__)
    (folder / "error.txt").write_text("".join(tb))
    try:
        (folder / "resources.txt").write_text(resource_snapshot())
    except OSError as exc:
        logger.debug("Failed to capture the resource snapshot: %s", exc)

    page = getattr(session, "page", None)
    if page is None or page.is_closed():
        logger.debug("No live page — skipping HTML/screenshot capture")
        (folder / "page.html").write_text("<!-- page was None or closed -->")
        return

    try:
        (folder / "page.html").write_text(page.content())
    except Exception as exc:
        logger.debug("Failed to capture HTML: %s", exc)

    try:
        page.screenshot(path=str(folder / "screenshot.png"))
    except Exception as exc:
        logger.debug("Failed to capture screenshot: %s", exc)

    logger.info("Failure diagnostics saved → %s", folder)


_CGROUP = pathlib.Path("/sys/fs/cgroup")


def _cgroup_value(name: str) -> str:
    try:
        return (_CGROUP / name).read_text().strip()
    except OSError:
        return "?"


def _browser_processes() -> str:
    """Живых процессов браузера — по ним утечка видна сразу."""
    try:
        return str(sum(
            1 for entry in pathlib.Path("/proc").iterdir()
            if entry.name.isdigit() and _comm_is_browser(entry)
        ))
    except OSError:
        return "?"


def _comm_is_browser(entry: pathlib.Path) -> bool:
    try:
        return "chrome" in (entry / "comm").read_text()
    except OSError:  # процесс успел завершиться
        return False


def resource_snapshot() -> str:
    """Счётчики контейнера на момент падения.

    Стоит копейки, а без неё «почему упало» превращается в раскопки на хосте.
    У ``can't start new thread`` и у убитого рендерера причина одна — утёкшие
    браузеры, и видна она только здесь: один браузер это ~64 задачи при лимите
    в 400, так что трёх-шести хватало, чтобы упереться.
    """
    return (
        f"pids: {_cgroup_value('pids.current')} / {_cgroup_value('pids.max')}\n"
        f"memory: {_cgroup_value('memory.current')} / {_cgroup_value('memory.max')}"
        f" (peak {_cgroup_value('memory.peak')})\n"
        f"browser processes: {_browser_processes()}\n"
        f"python threads: {threading.active_count()}\n"
    )


def capture_wedge(label: str) -> pathlib.Path | None:
    """Стеки всех потоков в момент, когда сторож счёл демона зависшим.

    Именно этих улик не хватало 14 и 15.09.2026: контейнер жив, страница цела,
    аккаунт здоров, а висит Python — и найти, где именно, удалось только
    py-spy с хоста. Снимок делается ДО убийства браузера: после него
    зависший поток развернётся, и стек будет уже не тот.
    """
    if not _quota_allows():
        return None

    folder = DIAGNOSTICS_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_wedged"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        # error.txt — то, что читает классификатор автопочинки.
        (folder / "error.txt").write_text(
            f"BrowserUnresponsiveError: watchdog fired on {label}\n")
        (folder / "resources.txt").write_text(resource_snapshot())
        with (folder / "threads.txt").open("w") as handle:
            faulthandler.dump_traceback(file=handle, all_threads=True)
    except Exception:
        logger.debug("Failed to capture the wedge dump", exc_info=True)
        return None

    logger.error("Wedge diagnostics saved → %s", folder)
    return folder


@contextmanager
def failure_diagnostics(session):
    """Context manager that captures diagnostics on unhandled exceptions."""
    try:
        yield
    except Exception as exc:
        try:
            capture_failure(session, exc)
        except Exception as cap_exc:
            logger.debug("Diagnostic capture itself failed: %s", cap_exc)
        raise
