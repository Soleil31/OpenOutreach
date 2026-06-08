# linkedin/actions/post.py
"""Playwright automation for publishing a LinkedIn text post."""
from __future__ import annotations

import logging
import time

from playwright.sync_api import Page, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

_START_POST_SELECTORS = [
    "button:has-text('Start a post')",
    "button:has-text('Create a post')",
    "[data-view-name='share-creation-state']",
    ".share-box-feed-entry__trigger",
]

_POST_EDITOR_SELECTORS = [
    ".ql-editor",
    "[data-placeholder='What do you want to talk about?']",
    "[contenteditable='true']",
]

_SUBMIT_SELECTORS = [
    "button.share-actions__primary-action",
    "button:has-text('Post')",
]


def publish_text_post(page: Page, text: str) -> None:
    """Publish a plain-text post on the LinkedIn feed.

    Navigates to the feed, opens the post composer, types the text,
    and submits. Raises RuntimeError on any step failure.
    """
    logger.info("Navigating to LinkedIn feed")
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30_000)
    time.sleep(2)

    # Open post composer
    start_btn = None
    for sel in _START_POST_SELECTORS:
        try:
            start_btn = page.wait_for_selector(sel, timeout=5_000, state="visible")
            if start_btn:
                break
        except PWTimeout:
            continue

    if not start_btn:
        raise RuntimeError("Could not find 'Start a post' button on feed page")

    start_btn.click()
    time.sleep(1.5)

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
    "button:has-text('Add media')",
    "button[aria-label*='media' i]",
    "button[aria-label*='photo' i]",
    "button[data-test-icon='image-medium']",
    ".share-promoted-detour-button button",
]

_DONE_AFTER_UPLOAD_SELECTORS = [
    "button:has-text('Done')",
    "button:has-text('Next')",
    "button.share-box-footer__primary-btn",
    ".image-detour-actions button.share-box-footer__primary-btn",
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
    logger.info("Navigating to LinkedIn feed (image post)")
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30_000)
    time.sleep(2)

    start_btn = None
    for sel in _START_POST_SELECTORS:
        try:
            start_btn = page.wait_for_selector(sel, timeout=5_000, state="visible")
            if start_btn:
                break
        except PWTimeout:
            continue
    if not start_btn:
        raise RuntimeError("Could not find 'Start a post' button on feed page")
    start_btn.click()
    time.sleep(1.5)

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
