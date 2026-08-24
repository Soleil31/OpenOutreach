# linkedin/browser/selectors.py
"""Слой селекторов — все правила поиска элементов на страницах LinkedIn.

ЕДИНСТВЕННЫЙ файл, в который модулю автопочинки разрешено вносить изменения.
Патч, затрагивающий что-либо ещё, отклоняется без разбора содержания.

Требования к содержимому:

* только правила поиска и текстовые маркеры страниц;
* никакой бизнес-логики, обращений к базе, отправки сообщений и учётных данных;
* правила упорядочены от точных к широким; порядок менять можно, но широкие
  правила обязаны оставаться последними, иначе точные перестанут работать.

Правила — функции от страницы, а не строки: часть из них пользуется
семантическим поиском Playwright по роли и подписи, который строкой CSS не
выражается.

Маркеры страниц проверяются по ВИДИМОМУ тексту и заголовку вкладки, не по
разметке: поиск по HTML ловится на содержимое <style> и <script>.
"""

EMAIL_LOCATORS = [
    # Language-independent CSS-селекторы (любой UI)
    lambda p: p.locator('input#username'),
    lambda p: p.locator('input[autocomplete="username"]'),
    lambda p: p.locator('input[autocomplete="email"]'),
    lambda p: p.locator('input[type="email"]'),
    lambda p: p.locator('input[name="session_key"]'),
    lambda p: p.locator('input[autocomplete="webauthn"]'),
    lambda p: p.locator('input[inputmode="email"]'),
    # Английский text — fallback
    lambda p: p.get_by_role("textbox", name="Email or phone"),
    lambda p: p.get_by_label("Email or phone"),
    # Самый широкий: первый visible text-input
    lambda p: p.locator('form input[type="text"]:visible').first,
    lambda p: p.locator('form input:visible').first,
]

PASSWORD_LOCATORS = [
    lambda p: p.locator('input[type="password"]'),
    lambda p: p.locator('input[autocomplete="current-password"]'),
    lambda p: p.get_by_role("textbox", name="Password"),
    lambda p: p.get_by_label("Password"),
    lambda p: p.locator('input[name="session_password"]'),
    lambda p: p.locator('input#password'),
]

SUBMIT_LOCATORS = [
    # Точный селектор из реального HTML LinkedIn login (любой язык UI)
    lambda p: p.locator('button[data-id="organic-login-submit-button"]'),
    lambda p: p.locator('button[id*="organic-login-submit"]'),
    lambda p: p.locator('button[id*="login-submit"]'),
    # type=submit (если есть)
    lambda p: p.locator('form button[type="submit"]'),
    lambda p: p.locator('button[type="submit"]'),
    # Английский text
    lambda p: p.locator("form").get_by_role("button", name="Sign in", exact=True),
    lambda p: p.get_by_role("button", name="Sign in", exact=True),
    # Прочие fallback
    lambda p: p.locator('button[aria-label*="Sign in" i]'),
    lambda p: p.locator('button.from__button--floating'),
    lambda p: p.locator('button.btn__primary--large'),
    lambda p: p.locator('form button.artdeco-button--primary'),
    # Самые широкие — любая видимая кнопка в форме
    lambda p: p.locator('form.login__form button:visible').last,
    lambda p: p.locator('form button:visible').last,
    lambda p: p.locator('button:visible').last,
]

COMPLY_LOCATORS = [
    lambda p: p.locator('button#content__button--primary--muted'),
    lambda p: p.get_by_role("button", name="Agree to comply", exact=True),
    lambda p: p.locator('button.content__button--primary'),
]

COMPLY_PROBE_TIMEOUT_MS = 5000

CHALLENGE_URL_MARKERS = ("/checkpoint/challenge", "/checkpoint/lg/", "/uas/consumer-email-challenge")
CAPTCHA_MARKERS = (
    "captcha", "recaptcha", "arkoselabs",
    # LinkedIn называет эту страницу по-разному в заголовке и в тексте
    "security check", "quick security check", "security verification",
    "let's do a quick security check", "проверка безопасности",
)
BLOCKED_MARKERS = ("http 999", "access denied", "unusual activity", "temporarily restricted")
CREDENTIAL_MARKERS = (
    "wrong email or password", "couldn't find a linkedin account",
    "неверный пароль", "please enter a valid email",
)

# Имена наборов — по ним верификатор отчитывается, а патч обосновывает правку.
NAMED_CHAINS = {
    "email": EMAIL_LOCATORS,
    "password": PASSWORD_LOCATORS,
    "submit": SUBMIT_LOCATORS,
    "comply": COMPLY_LOCATORS,
}
