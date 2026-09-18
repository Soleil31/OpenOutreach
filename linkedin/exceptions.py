class AuthenticationError(Exception):
    """Custom exception for 401 Unauthorized errors."""
    pass


class TerminalStateError(Exception):
    """Profile is already done or dead — caller must skip it"""
    pass


class SkipProfile(Exception):
    """Profile must be skipped."""
    pass


class ProfileInaccessibleError(Exception):
    """Profile is private, deleted, or restricted (HTTP 403/404)."""
    pass


class ReachedConnectionLimit(Exception):
    """ Weekly connection limit reached. """
    pass


class ProfileViewLimitReached(Exception):
    """The account's profile-view budget is spent; try again after ``retry_after``.

    Deliberately NOT an IOError: tenacity retries IOError on Voyager calls, and
    this must never be retried — the whole point is to stop asking.
    """

    def __init__(self, retry_after: float, reason: str):
        super().__init__(f"{reason} — retry in {int(retry_after)}s")
        self.retry_after = retry_after
        self.reason = reason


class MessagingNetworkError(IOError):
    """The messaging page never loaded, and the API fallback could not deliver.

    A verdict on the network, not on the lead. A failed send used to move the
    Deal back to QUALIFIED "for re-connection" — and during the 2026-09-16
    proxy outage every send failed exactly this way, throwing 34 live
    conversations out of CONNECTED in one morning.
    """
    pass


class BrowserUnresponsiveError(IOError):
    """Python-side watchdog fired because Playwright did not return in time.

    Subclasses ``IOError`` so tenacity retries on ``get_profile`` / related
    Voyager calls pick it up automatically; handlers can still catch it
    distinctly to log 'browser watchdog fired' rather than a generic 5xx.
    """
    pass

