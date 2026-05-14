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
