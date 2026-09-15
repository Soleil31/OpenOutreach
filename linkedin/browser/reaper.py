# linkedin/browser/reaper.py
"""Kill a wedged browser from a thread that does not own it.

Sync Playwright objects belong to the thread that created them. The daemon's
watchdogs run on ``threading.Timer`` threads, and every ``close()`` they made
failed with ``greenlet.error: Cannot switch to a different thread`` — logged at
DEBUG and swallowed, while the log line above it said "closing browser". The
blocked call in the main thread kept waiting. On 2026-09-14 and 2026-09-15 the
NL daemon sat like that for 18.5 h and 13 h, with three Chromium instances
still running inside the container.

What does work from any thread is the operating system. SIGKILL the driver
process and the blocked call fails in its own thread within a second
("Connection closed while reading from the driver"). After that only
``playwright.stop()`` is safe: ``BrowserContext.close()`` waits for a "closed"
event that a dead driver never sends, and hangs just the same.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess

logger = logging.getLogger(__name__)


def _transport(playwright):
    try:
        return playwright._impl_obj._connection._transport
    except AttributeError:
        return None


def driver_gone(playwright) -> bool:
    """True once the connection to the Playwright driver has died."""
    future = getattr(_transport(playwright), "on_error_future", None)
    return future is not None and future.done()


def _children_by_parent() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    if os.path.isdir("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as fh:
                    stat = fh.read().decode(errors="replace")
            except OSError:
                continue
            # "pid (comm) state ppid …" — comm may itself contain ") ".
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            children.setdefault(ppid, []).append(int(entry))
        return children

    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid="],
        capture_output=True, text=True, timeout=10, check=False,
    ).stdout
    for line in listing.splitlines():
        pid, ppid = (int(field) for field in line.split())
        children.setdefault(ppid, []).append(pid)
    return children


def process_tree(root: int) -> list[int]:
    """*root* followed by all of its descendants."""
    children = _children_by_parent()
    tree, stack = [], [root]
    while stack:
        pid = stack.pop()
        tree.append(pid)
        stack.extend(children.get(pid, []))
    return tree


def kill_browser(playwright) -> int:
    """SIGKILL the Playwright driver and every Chromium process under it.

    Never calls into Playwright, so it is safe from any thread. Returns how
    many processes were signalled. The tree is collected before the first
    kill: once the driver dies, its children are re-parented to init and can
    no longer be told apart from anything else in the container.
    """
    proc = getattr(_transport(playwright), "_proc", None)
    if proc is None or proc.returncode is not None:
        # Nothing launched, or already reaped — the pid may belong to someone else now.
        return 0

    try:
        targets = process_tree(proc.pid)
    except Exception:
        logger.debug("Could not list the browser process tree", exc_info=True)
        targets = [proc.pid]

    killed = 0
    for pid in targets:
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            pass
    return killed
