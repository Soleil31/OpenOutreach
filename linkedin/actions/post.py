# linkedin/actions/post.py
"""Playwright automation for publishing a LinkedIn text post."""
from __future__ import annotations

import logging
import time

from playwright.sync_api import Page, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

# LinkedIn renders the feed/composer in the account's UI language, so any
# selector keyed on visible text must cover EN + RU at minimum. We also keep
# language-agnostic structural selectors (class/aria-label/data-view-name) as
# the primary lookup — they survive translation.
_START_POST_SELECTORS = [
    ".share-box-feed-entry__trigger",
    "[data-view-name='share-creation-state']",
    "button[aria-label*='post' i]",
    "button[aria-label*='публикац' i]",
    "button:has-text('Start a post')",
    "button:has-text('Create a post')",
    "button:has-text('Начать публикацию')",
    "button:has-text('Создать публикацию')",
]

_POST_EDITOR_SELECTORS = [
    ".ql-editor",
    "[contenteditable='true']",
    "[data-placeholder='What do you want to talk about?']",
    "[data-placeholder*='talk about' i]",
    "[data-placeholder*='хотите рассказать' i]",
    "[data-placeholder*='хотите поделиться' i]",
]

_SUBMIT_SELECTORS = [
    "button.share-actions__primary-action",
    "button:has-text('Post')",
    "button:has-text('Опубликовать')",
    "button[aria-label='Post']",
    "button[aria-label='Опубликовать']",
]


def _open_composer(page: Page) -> None:
    """Open the LinkedIn post composer modal.

    Strategy: navigate to ``/feed/?shareActive=true``, which LinkedIn itself
    treats as a deep-link to the composer — the modal opens on page load
    without needing to find/click a "Start a post" button (whose label
    depends on UI language). If that fails, fall back to clicking the
    trigger button by structural+text selectors.
    """
    logger.info("Opening post composer via ?shareActive=true")
    page.goto(
        "https://www.linkedin.com/feed/?shareActive=true",
        wait_until="domcontentloaded",
        timeout=30_000,
    )
    time.sleep(3)

    # Did the deep-link open the editor? If yes — no further click needed.
    for sel in _POST_EDITOR_SELECTORS:
        try:
            if page.wait_for_selector(sel, timeout=2_000, state="visible"):
                logger.info("Composer opened via deep-link")
                return
        except PWTimeout:
            continue

    # Fallback: click the trigger button.
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
    start_btn.click()
    time.sleep(1.5)


def publish_text_post(page: Page, text: str) -> None:
    """Publish a plain-text post on the LinkedIn feed.

    Navigates to the feed, opens the post composer, types the text,
    and submits. Raises RuntimeError on any step failure.
    """
    _open_composer(page)

    # Find editor
    editor = None
    for sel in _POST_EDITOR_SELECTORS:
        try:
            editor = page.wait_for_selector(sel, timeout=5_000, state="visible")
            if editor:
                break
        except PWTimeout:
            continue

    if not editor:
        raise RuntimeError("Post editor did not open")

    editor.click()
    time.sleep(0.5)
    editor.fill(text)
    time.sleep(1)

    # Submit
    submit_btn = None
    for sel in _SUBMIT_SELECTORS:
        try:
            submit_btn = page.wait_for_selector(sel, timeout=5_000, state="visible")
            if submit_btn:
                break
        except PWTimeout:
            continue

    if not submit_btn:
        raise RuntimeError("Could not find Post submit button")

    submit_btn.click()
    logger.info("Post submit clicked, waiting for confirmation")
    time.sleep(3)

    # Light confirmation: feed should reload or modal closes
    try:
        page.wait_for_selector(".share-creation-state", state="hidden", timeout=10_000)
    except PWTimeout:
        # Modal might not exist — that's fine
        pass

    logger.info("Post published successfully")


# ── Image post ──────────────────────────────────────────────────────────


_ADD_MEDIA_SELECTORS = [
    "button[aria-label*='media' i]",
    "button[aria-label*='photo' i]",
    "button[aria-label*='медиа' i]",
    "button[aria-label*='фото' i]",
    "button[data-test-icon='image-medium']",
    ".share-promoted-detour-button button",
    "button:has-text('Add media')",
    "button:has-text('Добавить медиа')",
]

_DONE_AFTER_UPLOAD_SELECTORS = [
    "button.share-box-footer__primary-btn",
    ".image-detour-actions button.share-box-footer__primary-btn",
    "button:has-text('Done')",
    "button:has-text('Next')",
    "button:has-text('Готово')",
    "button:has-text('Далее')",
]


def publish_image_post(page: "Page", text: str, image_path) -> None:
    """Publish a LinkedIn post with a single image attached.

    Same flow as ``publish_text_post`` but inserts a media upload between
    typing the text and pressing Post:
      1. open composer
      2. type text
      3. click "Add media", upload the file via ``input[type=file]``
      4. confirm media (Done/Next)
      5. submit
    Crashes on unexpected errors per project convention.
    """
    image_path = str(image_path)
    _open_composer(page)

    editor = None
    for sel in _POST_EDITOR_SELECTORS:
        try:
            editor = page.wait_for_selector(sel, timeout=5_000, state="visible")
            if editor:
                break
        except PWTimeout:
            continue
    if not editor:
        raise RuntimeError("Post editor did not open")
    editor.click()
    time.sleep(0.5)
    editor.fill(text)
    time.sleep(1)

    # Open media picker
    media_btn = None
    for sel in _ADD_MEDIA_SELECTORS:
        try:
            media_btn = page.wait_for_selector(sel, timeout=5_000, state="visible")
            if media_btn:
                break
        except PWTimeout:
            continue
    if not media_btn:
        raise RuntimeError("Could not find 'Add media' button in composer")
    media_btn.click()
    time.sleep(1)

    # Upload via hidden <input type=file>. LinkedIn keeps it in the DOM even
    # when the picker dialog hasn't rendered the file chooser button.
    file_input = page.locator('input[type="file"]').first
    file_input.wait_for(state="attached", timeout=10_000)
    file_input.set_input_files(image_path)
    logger.info("Image uploaded: %s", image_path)

    # Confirm the media (Done/Next) — LinkedIn shows a small modal with crop tools
    done_btn = None
    for sel in _DONE_AFTER_UPLOAD_SELECTORS:
        try:
            done_btn = page.wait_for_selector(sel, timeout=15_000, state="visible")
            if done_btn:
                break
        except PWTimeout:
            continue
    if done_btn:
        done_btn.click()
        time.sleep(2)

    # Submit
    submit_btn = None
    for sel in _SUBMIT_SELECTORS:
        try:
            submit_btn = page.wait_for_selector(sel, timeout=10_000, state="visible")
            if submit_btn:
                break
        except PWTimeout:
            continue
    if not submit_btn:
        raise RuntimeError("Could not find Post submit button after media upload")
    submit_btn.click()
    logger.info("Image post submit clicked")
    time.sleep(3)

    try:
        page.wait_for_selector(".share-creation-state", state="hidden", timeout=10_000)
    except PWTimeout:
        pass
    logger.info("Image post published successfully")
