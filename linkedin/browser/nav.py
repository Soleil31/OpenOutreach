# linkedin/browser/nav.py
import logging
import random
from urllib.parse import unquote, urlparse, urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.conf import BROWSER_NAV_TIMEOUT_MS, DUMP_PAGES, FIXTURE_PAGES_DIR, HUMAN_TYPE_MIN_DELAY_MS, HUMAN_TYPE_MAX_DELAY_MS
from linkedin.exceptions import SkipProfile

logger = logging.getLogger(__name__)


def goto_page(session,
              action,
              expected_url_pattern: str,
              timeout: int = BROWSER_NAV_TIMEOUT_MS,
              error_message: str = "",
              ):
    page = session.page
    action()
    if not page:
        return

    try:
        page.wait_for_url(lambda url: expected_url_pattern in unquote(url), timeout=timeout)
    except PlaywrightTimeoutError:
        pass  # we still continue and check URL below

    session.wait()

    current = unquote(page.url)
    if expected_url_pattern not in current:
        if "/404" in current:
            raise SkipProfile(f"Profile returned 404 → {current}")
        raise RuntimeError(f"{error_message} → expected '{expected_url_pattern}' | got '{current}'")

    logger.debug("Navigated to %s", page.url)


def extract_in_urls(page):
    """Extract all /in/ profile URLs from the current page."""
    from linkedin.url_utils import url_to_public_id

    seen = set()
    urls = []
    for link in page.locator('a[href*="/in/"]').all():
        href = link.get_attribute("href")
        if href and "/in/" in href:
            full_url = urljoin(page.url, href.strip())
            clean = urlparse(full_url)._replace(query="", fragment="").geturl()
            if not url_to_public_id(clean):
                continue
            if clean not in seen:
                seen.add(clean)
                urls.append(clean)
    logger.debug(f"Extracted {len(urls)} unique /in/ profiles")
    return urls


def find_first_visible(page, selectors: list[str]):
    """Try selectors in order, return first locator that matches."""
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() > 0:
            return locator.first
    return None


def resolve_locator(page, candidates, timeout_per_ms: int = 5000):
    """Try locator factories in order, return the first match that is visible.

    Every match is tried, not just ``.first``. LinkedIn's SDUI login page keeps
    more than one copy of the form in the DOM — a hidden one and the live one,
    distinguishable only by React ids that change on every render. Taking
    ``.first`` landed on the hidden copy, so the wait timed out on a field that
    was plainly on screen, and every fallback in the chain repeated the same
    mistake. That is what broke the login on 21.08.2026.

    The first probe of each match is short: with several copies in the DOM,
    spending the full timeout on each hidden one would take minutes. Only when
    no match anywhere is visible do we go back and wait properly on the first
    one, so a form that is merely slow to render is still caught.
    """
    quick_ms = min(1000, timeout_per_ms)
    for factory in candidates:
        locator = factory(page)
        try:
            count = locator.count()
        except Exception:  # a factory may resolve to nothing at all
            continue
        if count == 0:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                candidate.wait_for(state="visible", timeout=quick_ms)
                return candidate
            except PlaywrightTimeoutError:
                continue
        # Nothing visible yet — the page may still be rendering.
        try:
            first = locator.first
            first.wait_for(state="visible", timeout=timeout_per_ms)
            return first
        except PlaywrightTimeoutError:
            continue
    raise RuntimeError(f"No locator matched on {page.url}")


from linkedin.browser.selectors import TOP_CARD_SELECTORS

def find_top_card(session):
    top_card = find_first_visible(session.page, TOP_CARD_SELECTORS)
    if top_card is None:
        logger.warning("Top card not found on %s", session.page.url)
        raise SkipProfile("Top Card section not found")
    return top_card


def human_type(locator, text: str, min_delay: int = HUMAN_TYPE_MIN_DELAY_MS, max_delay: int = HUMAN_TYPE_MAX_DELAY_MS):
    """Type text with randomized per-keystroke delay to mimic human input."""
    locator.type(text, delay=random.randint(min_delay, max_delay))


def dump_page_html(session: "AccountSession", profile: dict, category: str = "connect"):
    if not DUMP_PAGES:
        return
    dest = FIXTURE_PAGES_DIR / category
    dest.mkdir(parents=True, exist_ok=True)
    filepath = dest / f"{profile.get('public_identifier')}.html"
    html_content = session.page.content()
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info("Saved page snapshot → %s", filepath)
