# linkedin/browser/session.py
from __future__ import annotations

import logging
import random
import time
from functools import cached_property

from linkedin.browser.reaper import driver_gone
from linkedin.conf import MIN_DELAY, MAX_DELAY

logger = logging.getLogger(__name__)

# The main LinkedIn auth cookie
_AUTH_COOKIE_NAME = "li_at"


def random_sleep(min_val, max_val):
    delay = random.uniform(min_val, max_val)
    logger.debug(f"Pause: {delay:.2f}s")
    time.sleep(delay)


class AccountSession:
    def __init__(self, linkedin_profile):
        self.linkedin_profile = linkedin_profile
        self.django_user = linkedin_profile.user

        # Active campaign — set by the daemon before each lane execution
        self.campaign = None

        # Playwright objects – created on first access or after crash
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    @cached_property
    def campaigns(self):
        """All active campaigns this user belongs to (cached)."""
        from linkedin.models import Campaign
        return list(Campaign.objects.filter(users=self.django_user, active=True))

    def ensure_browser(self):
        """Launch or recover browser + login if needed. Call before using .page"""
        from linkedin.browser.login import start_browser_session

        if not self.page or self.page.is_closed() or driver_gone(self.playwright):
            logger.debug("Launching/recovering browser for %s", self)
            # Tear the old one down first: overwriting it left its Chromium running.
            if self.playwright is not None:
                self.close()
            start_browser_session(session=self)
        else:
            self._maybe_refresh_cookies()

    @cached_property
    def self_profile(self) -> dict:
        """Authenticated user's profile dict, fetched once per session.

        The dict isn't persisted to DB (we dropped ``Lead.profile_data``),
        so the first access per session triggers a Voyager call; the
        ``cached_property`` keeps it warm for the rest of the session.
        """
        from linkedin.setup.self_profile import discover_self_profile

        self.ensure_browser()
        return discover_self_profile(self)

    def wait(self, min_delay=MIN_DELAY, max_delay=MAX_DELAY):
        random_sleep(min_delay, max_delay)
        self.page.wait_for_load_state("domcontentloaded")

    def reauthenticate(self):
        """Force a fresh login: close the browser and log in on a clean context.

        The saved cookies are deliberately NOT cleared here. They are replaced
        only once a login actually succeeds — otherwise every failed attempt
        destroyed the stored session, and a session imported by hand could not
        survive a single failing cycle.
        """
        from linkedin.browser.login import start_browser_session

        logger.warning("Re-authenticating %s — saved session kept until the new one works", self)
        self.close()
        start_browser_session(session=self, force_login=True)

    def _maybe_refresh_cookies(self):
        """Re-login if the li_at auth cookie in the saved DB state is expired."""
        from linkedin.browser.login import start_browser_session

        self.linkedin_profile.refresh_from_db(fields=["cookie_data"])
        cookie_data = self.linkedin_profile.cookie_data
        if not cookie_data:
            return
        for cookie in cookie_data.get("cookies", []):
            if cookie.get("name") == _AUTH_COOKIE_NAME:
                expires = cookie.get("expires", -1)
                if expires > 0 and expires < time.time():
                    logger.warning("Auth cookie expired for %s — re-authenticating", self)
                    self.close()
                    start_browser_session(session=self)
                return

    def close(self):
        if self.context or self.playwright:
            if driver_gone(self.playwright):
                # context.close() would wait forever for the dead driver's
                # "closed" event. stop() still has to run: it is what shuts the
                # event loop down for the next sync_playwright().start().
                closers = (self.playwright.stop,)
            else:
                closers = (
                    getattr(self.context, "close", None),
                    getattr(self.browser, "close", None),
                    getattr(self.playwright, "stop", None),
                )
            try:
                # One failing step must not skip the rest — a skipped stop()
                # is the "Sync API inside the asyncio loop" trap.
                for closer in closers:
                    if closer is None:
                        continue
                    try:
                        closer()
                    except Exception as e:
                        logger.debug("Error closing browser: %s", e)
                logger.info("Browser closed (%s)", self)
            finally:
                self.page = self.context = self.browser = self.playwright = None

        logger.info("Account session closed → %s", self)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return self.linkedin_profile.linkedin_username
