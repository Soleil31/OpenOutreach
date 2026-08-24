# linkedin/actions/post.py
"""Playwright UI automation for publishing LinkedIn posts.

The bot's normal browser context is mobile (Android Chrome UA, 393×852,
touch=True). LinkedIn's mobile web (mweb) deliberately omits the
"create your own post" composer — only OPEN_SHARE_MODAL (reshare) is
available. To publish from a server with only ``li_at`` + ``JSESSIONID``
cookies, we spawn a SECOND browser context on the same Playwright
instance with a desktop UA + viewport, copy cookies across, and drive the
desktop composer UI. The mobile context (which runs connect/scrape
tasks) is left untouched.

Why this is safer than it sounds:
- Same Playwright process, same Chromium build, same IP (residential
  proxy) — the only fingerprint difference is UA/viewport.
- Used once per post (1-2 posts/day per Cexim guidance), not for every
  outreach action — well below LinkedIn's anomaly thresholds.
- Pattern documented in cookie-based LinkedIn automation guides as the
  canonical way to use ``li_at`` for content creation without OAuth.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
)

logger = logging.getLogger(__name__)


# Desktop fingerprint used for one-shot publishing contexts. Linux Chrome
# matches the proxy's residential geography (NL/UAE) without raising the
# usual "headless Chromium" flags.
_DESKTOP_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
_DESKTOP_VIEWPORT = {"width": 1366, "height": 900}


def _spawn_desktop_context(session) -> tuple[BrowserContext, Page]:
    """Open a fresh desktop browser context on the session's existing
    Playwright/Browser, carrying over the mobile session's cookies.

    Returns ``(context, page)``. Caller must call ``context.close()``
    when done. Does NOT touch ``session.context`` / ``session.page``.
    """
    from playwright_stealth import Stealth  # same lib login.py uses

    session.ensure_browser()
    if session.browser is None:
        raise RuntimeError("Session has no live browser — cannot spawn desktop context")

    cookies = session.context.cookies()

    # Match the locale/timezone of the mobile profile so things like
    # geo-suggestion + date formatting line up across the two contexts.
    profile = session.linkedin_profile
    locale = profile.browser_locale or "en-US"
    timezone_id = profile.browser_timezone or "Europe/Amsterdam"

    desktop_ctx = session.browser.new_context(
        user_agent=_DESKTOP_UA,
        viewport=_DESKTOP_VIEWPORT,
        locale=locale,
        timezone_id=timezone_id,
        is_mobile=False,
        has_touch=False,
        device_scale_factor=1,
    )
    desktop_ctx.add_cookies(cookies)
    try:
        Stealth().apply_stealth_sync(desktop_ctx)
    except Exception as exc:
        logger.warning("Stealth patch failed on desktop context (continuing): %s", exc)

    page = desktop_ctx.new_page()
    logger.info(
        "Spawned desktop publishing context (UA=Linux Chrome, %dx%d, %d cookies)",
        _DESKTOP_VIEWPORT["width"], _DESKTOP_VIEWPORT["height"], len(cookies),
    )
    return desktop_ctx, page

# LinkedIn renders the feed/composer in the account's UI language, so any
# selector keyed on visible text must cover EN + RU at minimum. We also keep
# language-agnostic structural selectors (class/aria-label/data-view-name) as
# the primary lookup — they survive translation.
# Правила поиска — в слое селекторов, единственном файле, куда пишет автопочинка.
from linkedin.browser.selectors import (
    GDPR_ACCEPT_SELECTORS as _GDPR_ACCEPT_SELECTORS,
    POST_EDITOR_SELECTORS as _POST_EDITOR_SELECTORS,
    POST_SUBMIT_SELECTORS as _SUBMIT_SELECTORS,
    START_POST_SELECTORS as _START_POST_SELECTORS,
)


def _dismiss_gdpr_if_present(page: Page) -> None:
    """Click any visible 'accept cookies' button. Safe no-op if no banner."""
    for sel in _GDPR_ACCEPT_SELECTORS:
        try:
            btn = page.wait_for_selector(sel, timeout=1_500, state="visible")
            if btn:
                btn.click()
                logger.info("GDPR cookie banner dismissed via %s", sel)
                time.sleep(1)
                return
        except PWTimeout:
            continue


def _click_robust(element) -> None:
    """Click past LinkedIn's #interop-outlet overlay via JS if it intercepts."""
    try:
        element.click(timeout=8_000)
    except PWTimeout:
        element.evaluate("el => el.click()")


def _open_composer(page: Page) -> None:
    """Open the LinkedIn post composer modal on a desktop page.

    Strategy: navigate to ``/feed/?shareActive=true`` (LinkedIn's own
    deep-link to the composer), dismiss any GDPR banner, and wait for
    the editor. Fall back to clicking the "Start a post" trigger if
    the deep-link doesn't auto-open the modal.
    """
    logger.info("Opening post composer via ?shareActive=true")
    page.goto(
        "https://www.linkedin.com/feed/?shareActive=true",
        wait_until="domcontentloaded",
        timeout=30_000,
    )
    time.sleep(3)

    _dismiss_gdpr_if_present(page)

    # Did the deep-link open the editor? If yes — no further click needed.
    for sel in _POST_EDITOR_SELECTORS:
        try:
            if page.wait_for_selector(sel, timeout=2_000, state="visible"):
                logger.info("Composer opened via deep-link")
                return
        except PWTimeout:
            continue

    logger.info("Deep-link didn't open composer, falling back to trigger click")
    start_btn = None
    for sel in _START_POST_SELECTORS:
        try:
            start_btn = page.wait_for_selector(sel, timeout=5_000, state="visible")
            if start_btn:
                break
        except PWTimeout:
            continue
    if not start_btn:
        raise RuntimeError("Could not open post composer (deep-link + trigger both failed)")
    _click_robust(start_btn)
    time.sleep(1.5)

    for sel in _POST_EDITOR_SELECTORS:
        try:
            if page.wait_for_selector(sel, timeout=4_000, state="visible"):
                logger.info("Composer opened after trigger click")
                return
        except PWTimeout:
            continue
    raise RuntimeError("Trigger clicked but post editor never appeared")


def _find_first(page: Page, selectors: list[str], timeout_ms: int):
    """Return the first visible element matching any selector, or None."""
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
            if el:
                return el
        except PWTimeout:
            continue
    return None


def publish_text_post(session, text: str) -> None:
    """Publish a text-only LinkedIn post.

    Spawns a temporary desktop browser context (mobile-context cookies
    are copied across), drives the desktop composer UI, and tears down
    the context. The mobile session is not touched.
    """
    desktop_ctx, page = _spawn_desktop_context(session)
    try:
        _open_composer(page)

        editor = _find_first(page, _POST_EDITOR_SELECTORS, timeout_ms=5_000)
        if not editor:
            raise RuntimeError("Post editor did not open")
        _click_robust(editor)
        time.sleep(0.4)
        editor.fill(text)
        time.sleep(1)

        submit_btn = _find_first(page, _SUBMIT_SELECTORS, timeout_ms=5_000)
        if not submit_btn:
            raise RuntimeError("Could not find Post submit button")
        _click_robust(submit_btn)
        logger.info("Post submit clicked, waiting for confirmation")
        time.sleep(4)

        # Modal should disappear once the share is accepted server-side.
        try:
            page.wait_for_selector(".share-creation-state", state="hidden", timeout=10_000)
        except PWTimeout:
            pass

        logger.info("Text post published successfully")
    finally:
        try:
            desktop_ctx.close()
        except Exception as exc:
            logger.warning("Failed to close desktop context: %s", exc)


# ── Image post ──────────────────────────────────────────────────────────


from linkedin.browser.selectors import (
    ADD_MEDIA_SELECTORS as _ADD_MEDIA_SELECTORS,
    DONE_AFTER_UPLOAD_SELECTORS as _DONE_AFTER_UPLOAD_SELECTORS,
)


def publish_image_post(session, text: str, image_path) -> None:
    """Publish a LinkedIn post with a single image attached.

    Spawns a temporary desktop browser context (mobile-context cookies
    are copied across), drives the desktop composer UI:
      1. open composer
      2. fill text
      3. click "Add media", upload the file via ``input[type=file]``
      4. confirm crop modal (Done/Next)
      5. submit Post
    Then tears down the desktop context. The mobile session is not
    touched. Crashes on unexpected errors per project convention.
    """
    image_path = str(Path(image_path))
    desktop_ctx, page = _spawn_desktop_context(session)
    try:
        _open_composer(page)

        editor = _find_first(page, _POST_EDITOR_SELECTORS, timeout_ms=5_000)
        if not editor:
            raise RuntimeError("Post editor did not open")
        _click_robust(editor)
        time.sleep(0.4)
        editor.fill(text)
        time.sleep(1)

        media_btn = _find_first(page, _ADD_MEDIA_SELECTORS, timeout_ms=5_000)
        if not media_btn:
            raise RuntimeError("Could not find 'Add media' button in composer")
        _click_robust(media_btn)
        time.sleep(1)

        # LinkedIn keeps the hidden <input type=file> in the DOM even when
        # the picker dialog hasn't rendered the OS file chooser.
        file_input = page.locator('input[type="file"]').first
        file_input.wait_for(state="attached", timeout=10_000)
        file_input.set_input_files(image_path)
        logger.info("Image uploaded: %s", image_path)

        # The crop/preview modal then appears. Hit Done/Next (give it
        # extra time — LinkedIn does server-side processing here).
        done_btn = _find_first(page, _DONE_AFTER_UPLOAD_SELECTORS, timeout_ms=15_000)
        if done_btn:
            _click_robust(done_btn)
            time.sleep(2)

        submit_btn = _find_first(page, _SUBMIT_SELECTORS, timeout_ms=10_000)
        if not submit_btn:
            raise RuntimeError("Could not find Post submit button after media upload")
        _click_robust(submit_btn)
        logger.info("Image post submit clicked, waiting for upload to finalize")
        time.sleep(5)

        try:
            page.wait_for_selector(".share-creation-state", state="hidden", timeout=15_000)
        except PWTimeout:
            pass

        logger.info("Image post published successfully")
    finally:
        try:
            desktop_ctx.close()
        except Exception as exc:
            logger.warning("Failed to close desktop context: %s", exc)
