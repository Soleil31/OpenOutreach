# linkedin/browser/login.py
import asyncio
import logging
import os
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from termcolor import colored

from linkedin.account_state import LoginBlocked
from linkedin.browser.nav import goto_page, human_type, resolve_locator
from linkedin.conf import (
    BROWSER_DEFAULT_TIMEOUT_MS,
    BROWSER_LOGIN_TIMEOUT_MS,
    BROWSER_SLOW_MO,
)
# Правила поиска элементов вынесены в отдельный слой: это единственный файл,
# куда модулю автопочинки разрешено писать. Здесь они только используются.
from linkedin.browser.selectors import (
    BLOCKED_MARKERS,
    CAPTCHA_MARKERS,
    CHALLENGE_URL_MARKERS,
    COMPLY_LOCATORS,
    COMPLY_PROBE_TIMEOUT_MS,
    CREDENTIAL_MARKERS,
    EMAIL_LOCATORS,
    PASSWORD_LOCATORS,
    SUBMIT_LOCATORS,
)

logger = logging.getLogger(__name__)

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"

PROFILE_BROWSER_FIELDS = [
    "cookie_data",
    "browser_user_agent",
    "browser_locale",
    "browser_timezone",
    "browser_is_mobile",
    "browser_has_touch",
    "browser_viewport_width",
    "browser_viewport_height",
]


def dismiss_comply_gate(page, timeout_ms: int = COMPLY_PROBE_TIMEOUT_MS) -> bool:
    """Click LinkedIn's 'Agree to comply' interstitial if present. Return True if clicked."""
    for factory in COMPLY_LOCATORS:
        locator = factory(page).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            continue
        logger.info(colored("Dismissing 'Agree to comply' interstitial", "yellow"))
        locator.click()
        return True
    return False


def classify_login_failure(page, exc: Exception | None = None) -> tuple[str, str]:
    """Назвать причину, по которой не удалось войти. Возвращает ``(reason, detail)``.

    Демон по причине решает, есть ли смысл в следующей попытке: сломанный
    локатор чинится деплоем, а челлендж — только человеком. Всё делается
    best-effort: если страницу не прочитать, отдаём ``unknown``, но сам
    классификатор не должен бросать исключение.
    """
    url = ""
    body = ""
    try:
        url = (page.url or "").lower()
    except Exception:
        pass
    try:
        # Именно видимый текст, а не разметка. Поиск слов вроде "security check"
        # по всему HTML ловится на содержимое <style> и <script>: страница со
        # вшитыми стилями LinkedIn определялась как капча, хотя это была обычная
        # форма входа.
        body = (page.inner_text("body") or "").lower()[:200_000]
    except Exception:
        try:
            body = (page.content() or "").lower()[:200_000]
        except Exception:
            pass

    # Заголовок вкладки читаем отдельно: страница проверки безопасности может
    # почти не иметь видимого текста, но в <title> называет себя честно.
    title = ""
    try:
        title = (page.title() or "").lower()
    except Exception:
        pass

    if any(marker in url for marker in CHALLENGE_URL_MARKERS):
        return "checkpoint_2fa", f"LinkedIn увёл на проверку: {url}"
    if any(marker in title or marker in body for marker in CAPTCHA_MARKERS):
        return "captcha", f"страница проверки безопасности: {title.strip() or url}"
    if any(marker in body for marker in CREDENTIAL_MARKERS):
        return "bad_credentials", "LinkedIn отклонил пару логин/пароль"
    if any(marker in body for marker in BLOCKED_MARKERS):
        return "proxy_blocked", f"LinkedIn закрыл доступ с этого IP: {url}"
    if "/login" in url and "No locator matched" in str(exc or ""):
        return "locator_break", (
            "форма входа отрисована, но поля не находятся — LinkedIn сменил вёрстку. "
            f"{exc}"
        )
    if str(exc or ""):
        return "unknown", str(exc)[:500]
    return "unknown", f"вход не завершился, текущий URL: {url}"


def playwright_login(session: "AccountSession"):
    """Войти или бросить ``LoginBlocked`` с названной причиной отказа."""
    try:
        return _playwright_login_inner(session)
    except LoginBlocked:
        raise
    except Exception as exc:
        reason, detail = classify_login_failure(session.page, exc)
        url = ""
        try:
            url = session.page.url
        except Exception:
            pass
        logger.warning("Login blocked for %s — %s: %s", session, reason, detail)
        raise LoginBlocked(reason, detail, url) from exc


def _playwright_login_inner(session: "AccountSession"):
    page = session.page
    lp = session.linkedin_profile
    logger.info(colored("Fresh login sequence starting", "cyan") + f" for {session}")

    goto_page(
        session,
        action=lambda: page.goto(LINKEDIN_LOGIN_URL),
        expected_url_pattern="/login",
        error_message="Failed to load login page",
    )

    email_locator = _maybe_resolve_locator(page, EMAIL_LOCATORS, timeout_per_ms=3000)
    password_locator = _maybe_resolve_locator(page, PASSWORD_LOCATORS, timeout_per_ms=3000)

    if email_locator is not None:
        human_type(email_locator, lp.linkedin_username)
        session.wait()
        # The password field found before typing is kept. Re-resolving here
        # unconditionally is what used to fail: typing into the email field
        # re-renders LinkedIn's SDUI form, and the second lookup could land on
        # the stale copy left behind. Only look again if the handle really died.
        if not _still_usable(password_locator):
            password_locator = None

    if password_locator is None:
        password_locator = resolve_locator(page, PASSWORD_LOCATORS)

    human_type(password_locator, lp.linkedin_password)
    session.wait()

    submit = resolve_locator(page, SUBMIT_LOCATORS)
    submit.click()
    dismiss_comply_gate(page)
    goto_page(
        session,
        action=lambda: None,
        expected_url_pattern="/feed",
        timeout=BROWSER_LOGIN_TIMEOUT_MS,
        error_message="Login failed – no redirect to feed",
    )


def _maybe_resolve_locator(page, candidates, timeout_per_ms: int = 3000):
    try:
        return resolve_locator(page, candidates, timeout_per_ms=timeout_per_ms)
    except RuntimeError:
        return None


def _still_usable(locator, timeout_ms: int = 1000) -> bool:
    """Is this locator still attached to a visible element after a re-render?"""
    if locator is None:
        return False
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _detach_running_loop():
    """Снять пометку «в этом потоке крутится цикл asyncio».

    linkedin/llm.py на импорте зовёт nest_asyncio.apply(), а pydantic-ai
    гоняет запросы к модели через run_sync. После такого вызова в потоке
    остаётся помеченный активным цикл, и синхронный Playwright наотрез
    отказывается стартовать: "Sync API inside the asyncio loop".

    Метку снимаем и НЕ возвращаем: если её вернуть, Playwright падает уже
    на первом действии страницы (Page.goto). Запросы к модели от этого не
    страдают — nest_asyncio ставит метку заново на время каждого вызова.
    """
    if asyncio._get_running_loop() is not None:
        logger.debug("Detaching stale asyncio loop before launching Playwright")
        asyncio._set_running_loop(None)


def launch_browser(storage_state=None, linkedin_profile=None):
    logger.debug("Launching Playwright")
    _detach_running_loop()
    playwright = sync_playwright().start()
    launch_options = {
        "headless": False,
        "slow_mo": BROWSER_SLOW_MO,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            # The VPS has no real GPU, so Chromium falls back to SwiftShader
            # (software GPU emulation via ANGLE/Vulkan). SwiftShader crashes
            # the renderer process ("Target crashed") on heavy desktop
            # LinkedIn pages — confirmed in the renderer crash dump. Force a
            # pure-CPU render path and disable the SwiftShader fallback so
            # the DOM still renders for UI automation without the GPU layer.
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-gpu-compositing",
        ],
    }
    proxy_options = _proxy_options()
    if proxy_options:
        launch_options["proxy"] = proxy_options

    # Всё, что после start(), обязано быть под try: цикл событий Playwright
    # живёт в ЭТОМ потоке, и если уйти отсюда с исключением не позвав stop(),
    # он останется висеть. Тогда каждый следующий sync_playwright().start()
    # падает с "Sync API inside the asyncio loop" до конца жизни процесса —
    # то есть одна разовая ошибка (упавший Chromium, недоступный прокси)
    # навсегда выключает аккаунт. Именно так умирали Дубай и Казахстан:
    # десятки тысяч одинаковых ошибок после одного сбоя.
    browser = context = None
    try:
        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(
            **_context_options(storage_state=storage_state, linkedin_profile=linkedin_profile),
        )
        context.set_default_timeout(BROWSER_DEFAULT_TIMEOUT_MS)
        context.set_default_navigation_timeout(BROWSER_DEFAULT_TIMEOUT_MS)
        Stealth().apply_stealth_sync(context)
        page = context.new_page()
    except BaseException:
        # Разбираем в обратном порядке и глушим ошибки уборки: если Chromium
        # уже мёртв, close() тоже бросит, а нам важно добраться до stop().
        for closer in (
            getattr(context, "close", None),
            getattr(browser, "close", None),
            playwright.stop,
        ):
            if closer is None:
                continue
            try:
                closer()
            except Exception:
                logger.debug("cleanup after failed launch raised", exc_info=True)
        raise

    return page, context, browser, playwright


def _context_options(storage_state=None, linkedin_profile=None) -> dict:
    options = {}
    if storage_state:
        options["storage_state"] = storage_state

    user_agent = _profile_or_env(linkedin_profile, "browser_user_agent", "LINKEDIN_USER_AGENT")
    locale = _profile_or_env(linkedin_profile, "browser_locale", "LINKEDIN_LOCALE")
    timezone_id = _profile_or_env(linkedin_profile, "browser_timezone", "LINKEDIN_TIMEZONE")
    viewport_width = _profile_or_env_int(
        linkedin_profile,
        "browser_viewport_width",
        "LINKEDIN_VIEWPORT_WIDTH",
        default=1365,
    )
    viewport_height = _profile_or_env_int(
        linkedin_profile,
        "browser_viewport_height",
        "LINKEDIN_VIEWPORT_HEIGHT",
        default=768,
    )
    is_mobile = _profile_or_env_bool(linkedin_profile, "browser_is_mobile", "LINKEDIN_IS_MOBILE")
    has_touch = _profile_or_env_bool(linkedin_profile, "browser_has_touch", "LINKEDIN_HAS_TOUCH")

    if user_agent:
        options["user_agent"] = user_agent
    if locale:
        options["locale"] = locale
    if timezone_id:
        options["timezone_id"] = timezone_id
    if viewport_width and viewport_height:
        options["viewport"] = {"width": viewport_width, "height": viewport_height}
    if is_mobile:
        options["is_mobile"] = True
    if has_touch:
        options["has_touch"] = True

    return options


def _profile_or_env(linkedin_profile, profile_field: str, env_name: str) -> str:
    value = getattr(linkedin_profile, profile_field, "") if linkedin_profile else ""
    return str(value or os.getenv(env_name, "")).strip()


def _profile_or_env_int(linkedin_profile, profile_field: str, env_name: str, default: int) -> int:
    value = getattr(linkedin_profile, profile_field, None) if linkedin_profile else None
    if value in (None, ""):
        value = os.getenv(env_name)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _profile_or_env_bool(linkedin_profile, profile_field: str, env_name: str) -> bool:
    value = getattr(linkedin_profile, profile_field, None) if linkedin_profile else None
    if value is None:
        value = os.getenv(env_name)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _proxy_options() -> dict | None:
    proxy_server = os.getenv("PROXY_SERVER", "").strip()
    if not proxy_server:
        return None

    parsed = urlparse(proxy_server)
    if parsed.scheme and parsed.hostname:
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
        options = {"server": server}
        if parsed.username:
            options["username"] = parsed.username
        if parsed.password:
            options["password"] = parsed.password
        return options

    return {"server": proxy_server}


def _save_cookies(session):
    """Persist Playwright storage state (cookies) to the DB."""
    state = session.context.storage_state()
    session.linkedin_profile.cookie_data = state
    session.linkedin_profile.save(update_fields=["cookie_data"])


def start_browser_session(session: "AccountSession", force_login: bool = False):
    """Открыть браузер для аккаунта.

    ``cookie_data`` теперь только *перезаписывается успешным логином* и никогда
    не стирается заранее. Стирание до попытки (как было раньше) означало, что
    неудачный вход оставлял аккаунт вообще без сессии — и сессия, заведённая
    руками, не переживала ни одного неудачного цикла.
    """
    logger.debug("Configuring browser for %s", session)

    session.linkedin_profile.refresh_from_db(fields=PROFILE_BROWSER_FIELDS)
    cookie_data = session.linkedin_profile.cookie_data

    storage_state = None if force_login else (cookie_data if cookie_data else None)
    if storage_state:
        logger.info("Loading saved session for %s", session)
    elif force_login and cookie_data:
        logger.info("Forced re-login for %s — saved session kept until the new one works", session)

    session.page, session.context, session.browser, session.playwright = launch_browser(
        storage_state=storage_state,
        linkedin_profile=session.linkedin_profile,
    )

    if not storage_state:
        playwright_login(session)
        _save_cookies(session)
        logger.info(colored("Login successful – session saved", "green", attrs=["bold"]))
    else:
        try:
            session.page.goto(LINKEDIN_FEED_URL)
            dismiss_comply_gate(session.page)
            goto_page(
                session,
                action=lambda: None,
                expected_url_pattern="/feed",
                timeout=BROWSER_DEFAULT_TIMEOUT_MS,
                error_message="Saved session invalid",
            )
        except RuntimeError as exc:
            logger.warning("Saved LinkedIn session invalid for %s: %s", session, exc)
            # Перезапускаем браузер на чистом контексте вместо стирания
            # сохранённого состояния: если логин ниже упадёт, старые куки
            # останутся в базе и заведённая руками сессия не будет уничтожена
            # неудачной повторной попыткой.
            session.close()
            session.page, session.context, session.browser, session.playwright = launch_browser(
                storage_state=None,
                linkedin_profile=session.linkedin_profile,
            )
            playwright_login(session)
            _save_cookies(session)
            logger.info(colored("Login successful – session saved", "green", attrs=["bold"]))

    session.page.wait_for_load_state("load")
    logger.info(colored("Browser ready", "green", attrs=["bold"]))


if __name__ == "__main__":
    from linkedin.browser.registry import cli_parser, cli_session

    parser = cli_parser("Start a LinkedIn browser session")
    args = parser.parse_args()
    session = cli_session(args)
    session.ensure_browser()

    start_browser_session(session=session)
    logger.info("Logged in! Close browser manually.")
    session.page.pause()
