# linkedin/management/commands/save_session.py
"""Restore the LinkedIn session by hand, in the form the daemon reads.

The daemon keeps its session as a Playwright ``storage_state`` in
``LinkedInProfile.cookie_data``, not as a Chrome profile. So the human has to
log in through the SAME ``launch_browser()`` — proxy, stealth, user agent,
locale, timezone, viewport. A session made in any other browser is one the
daemon cannot pick up.

Opens a window on the container's X display (watch it through noVNC), waits
for a human to get past whatever LinkedIn asks, and saves the state the moment
the browser reaches the feed. It never types into fields and never clicks: the
human does. ``--autofill`` fills the login form exactly once, never again.

    docker exec -u ubuntu -e DISPLAY=:99 openoutreach python manage.py save_session

Stop the proxy watchdog first: it restarts the container when the proxy
flickers, and a restart kills the window in the middle of the login.
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

from django.core.management.base import BaseCommand
from django.utils import timezone

POLL_SECONDS = 5
FEED_URL = "https://www.linkedin.com/feed/"
LOGIN_URL = "https://www.linkedin.com/login"


def _goto_with_retries(page, url: str, attempts: int = 8, pause: int = 10) -> bool:
    """Open a page, riding out blips of the residential pool.

    Retrying a PAGE LOAD is safe — it is not a login attempt. Retrying a form
    SUBMISSION is never safe.
    """
    for attempt in range(1, attempts + 1):
        try:
            # domcontentloaded: the feed keeps pulling resources for a long
            # time, and all we need is to know the page is there.
            page.goto(url, timeout=90_000, wait_until="domcontentloaded")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"  attempt {attempt}/{attempts} failed: {str(exc).splitlines()[0]}", flush=True)
            if attempt < attempts:
                time.sleep(pause)
    return False


def _logged_in(urls: list[str]) -> bool:
    """True once any tab is on the feed or a profile.

    Judged by the path, not the whole URL: the login page itself is
    ``/login?session_redirect=/feed/``, and a substring check would take it
    for the feed.
    """
    return any(urlparse(url).path.startswith(("/feed", "/in/")) for url in urls)


class Command(BaseCommand):
    help = "Log in to LinkedIn by hand through noVNC and save the session for the daemon."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fresh", action="store_true",
            help="Start from a clean browser and the login form instead of the saved cookies.",
        )
        parser.add_argument(
            "--autofill", action="store_true",
            help="With --fresh: fill the login form once. Never retried.",
        )
        parser.add_argument("--wait-minutes", type=int, default=30)

    def handle(self, *args, fresh=False, autofill=False, wait_minutes=30, **options):
        from linkedin.browser.login import launch_browser
        from linkedin.models import LinkedInProfile

        profile = LinkedInProfile.objects.first()
        if profile is None:
            self.stderr.write("No LinkedInProfile — onboarding has not run yet.")
            return

        # Start from the saved cookies by default. They carry bcookie, the
        # browser id LinkedIn uses to recognise a known device; a clean browser
        # looks like a new device, which is one more reason to challenge. On
        # 2026-09-18 the saved state got straight past a checkpoint that the
        # daemon itself had been stopped by.
        saved = None if fresh else (profile.cookie_data or None)
        print(f"account: {profile.linkedin_username}", flush=True)
        print("start: saved cookies (known device)" if saved
              else "start: clean browser, login form", flush=True)

        page, context, browser, playwright = launch_browser(
            storage_state=saved, linkedin_profile=profile,
        )
        try:
            if _goto_with_retries(page, "https://api.ipify.org", attempts=4, pause=5):
                print(f"browser exit IP: {page.inner_text('body').strip()}", flush=True)

            if not _goto_with_retries(page, FEED_URL if saved else LOGIN_URL):
                print("LinkedIn never loaded — the proxy pool, not the account", flush=True)
                return

            if not saved and autofill:
                self._autofill_once(page, profile)

            print(f"window is open — waiting up to {wait_minutes} min for the feed", flush=True)
            if not self._wait_for_feed(page, context, wait_minutes * 60):
                print("never reached the feed — session NOT saved", flush=True)
                return

            # Let the page finish its requests so the cookies are all set.
            page.wait_for_timeout(5_000)
            state = context.storage_state()
            names = {cookie.get("name") for cookie in state.get("cookies", [])}
            if "li_at" not in names:
                print(f"no li_at in the state ({sorted(names)[:10]}) — not saving", flush=True)
                return

            profile.cookie_data = state
            fields = ["cookie_data"]
            if hasattr(profile, "cookie_imported_at"):
                profile.cookie_imported_at = timezone.now()
                fields.append("cookie_imported_at")
            profile.save(update_fields=fields)
            print(f"session saved: {len(state['cookies'])} cookies, li_at present. "
                  f"Restart the daemon to pick it up.", flush=True)
        finally:
            for closer in (context.close, browser.close, playwright.stop):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _wait_for_feed(page, context, seconds: int) -> bool:
        """Poll every open tab until one of them is the feed.

        page.wait_for_timeout, NOT time.sleep. In the sync API navigation
        events are only processed inside Playwright calls, and ``page.url`` is
        a cached field. With time.sleep the loop saw the URL from the moment it
        started forever: on 2026-09-18 a human sat in the feed while the
        script kept "waiting for the login" and would have closed the window
        with the only live session in it.
        """
        deadline = time.monotonic() + seconds
        last = ""
        while time.monotonic() < deadline:
            try:
                page.wait_for_timeout(POLL_SECONDS * 1000)
                urls = [tab.url for tab in context.pages]
            except Exception:  # noqa: BLE001
                print("window closed", flush=True)
                return False
            joined = " | ".join(url[:100] for url in urls)
            if joined != last:
                print(f"  [{time.strftime('%H:%M:%S')}] {joined}", flush=True)
                last = joined
            if _logged_in(urls):
                return True
        return False

    @staticmethod
    def _autofill_once(page, profile) -> None:
        from linkedin.browser.login import EMAIL_LOCATORS, PASSWORD_LOCATORS, SUBMIT_LOCATORS
        from linkedin.browser.nav import human_type, resolve_locator

        try:
            human_type(resolve_locator(page, EMAIL_LOCATORS, timeout_per_ms=4000),
                       profile.linkedin_username)
            page.wait_for_timeout(1500)
            human_type(resolve_locator(page, PASSWORD_LOCATORS, timeout_per_ms=4000),
                       profile.linkedin_password)
            page.wait_for_timeout(1500)
            resolve_locator(page, SUBMIT_LOCATORS, timeout_per_ms=4000).click()
            print("form submitted once — no retries", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"autofill did not go through ({exc}) — continue by hand", flush=True)
