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

# =========================================================================
# ПОВЕРХНОСТИ КОНВЕЙЕРА, КРОМЕ ВХОДА
#
# Ниже — правила для остальных страниц, на которых работает аутрич. Держим их
# здесь по той же причине: это единственный файл, куда автопочинке разрешено
# писать, и значит любая смена вёрстки LinkedIn чинится в одном месте.
#
# Здесь правила заданы строками CSS: так исторически устроены модули действий,
# и переписывать их на функции незачем — строку модель правит так же надёжно.
# =========================================================================

# --- карточка профиля ----------------------------------------------------

TOP_CARD_SELECTORS = [
    'section:has(div.top-card-background-hero-image)',
    'section[data-member-id]',
    'section.artdeco-card:has(> div.pv-top-card)',
    'section:has(> div[class*="pv-top-card"])',
    'section[componentkey*="com.linkedin.sdui.profile.card"]',
]

# --- установление контакта ------------------------------------------------

CONNECT_SELECTORS = {
    "weekly_limit": 'div[class*="ip-fuse-limit-alert__warning"]',
    "invite_to_connect": (
        '[aria-label*="Invite"][aria-label*="to connect"]:visible, '
        'a:has(span:text-is("Connect")):visible, '
        'button:has(span:text-is("Connect")):visible'
    ),
    "error_toast": 'div[data-test-artdeco-toast-item-type="error"]',
    "more_button": (
        'button[aria-label="More"]:visible, '
        'button[id*="overflow"]:visible, '
        'button[aria-label*="More actions"]:visible, '
        'button:has(span:text-is("More")):visible'
    ),
    "connect_option": (
        'div[role="button"][aria-label^="Invite"][aria-label*=" to connect"], '
        'div[role="button"]:text-is("Connect"), '
        '[role="menuitem"][aria-label*="Connect"], '
        '[role="menuitem"]:has-text("Connect"), '
        'li:text-is("Connect"), '
        'span[role="button"]:text-is("Connect")'
    ),
    "send_now": (
        'button:has-text("Send now"), '
        'button[aria-label*="Send without"], '
        'button[aria-label*="Send invitation"]'
    ),
}

STATUS_SELECTORS = {
    "pending_button": '[aria-label*="Pending"]',
    "invite_to_connect": CONNECT_SELECTORS["invite_to_connect"],
    "more_button": CONNECT_SELECTORS["more_button"],
    "connect_option": CONNECT_SELECTORS["connect_option"],
}

# --- переписка ------------------------------------------------------------
#
# LinkedIn прогоняет A/B-варианты интерфейса по аккаунтам и часто переименовывает
# классы, поэтому сначала идут семантические правила (роль, ARIA), потом классовые.

MESSAGE_SELECTOR_CHAINS = {
    "connections_input": [
        'input[role="combobox"][placeholder*="name"]',
        'input[class*="msg-connections"]',
        'input[placeholder*="Type a name"]',
        'input[type="text"][aria-owns]',
    ],
    "search_result_row": [
        'ul[role="listbox"] li[role="option"]',
        'div[class*="msg-connections-typeahead__search-result-row"]',
        'li[class*="search-result"]',
    ],
    "compose_input": [
        'div[role="textbox"][aria-label*="Write a message"]',
        'div[role="textbox"][aria-label*="message"i]',
        'div[class*="msg-form__contenteditable"]',
        'div[contenteditable="true"]',
    ],
    "compose_send": [
        'button[type="submit"][class*="msg-form"]',
        'button[class*="send-btn"]',
        'button[class*="send-button"]',
        'form button[type="submit"]',
        'button[type="submit"]',
    ],
}

# --- поиск людей ----------------------------------------------------------

SEARCH_SELECTORS = {
    "search_bar": "//input[contains(@placeholder, 'Search')]",
    "profile_links": 'a[href*="/in/"]',
}

# --- публикация -----------------------------------------------------------

START_POST_SELECTORS = [
    ".share-box-feed-entry__trigger",
    "[data-view-name='share-creation-state']",
    "button[aria-label*='post' i]",
    "button[aria-label*='публикац' i]",
    "button[aria-label*='bericht' i]",
    "button:has-text('Start a post')",
    "button:has-text('Create a post')",
    "button:has-text('Начать публикацию')",
    "button:has-text('Создать публикацию')",
    "button:has-text('Begin een bericht')",
    "button:has-text('Bericht maken')",
]

POST_EDITOR_SELECTORS = [
    ".ql-editor",
    "[contenteditable='true']",
    "[data-placeholder='What do you want to talk about?']",
    "[data-placeholder*='talk about' i]",
    "[data-placeholder*='хотите рассказать' i]",
    "[data-placeholder*='хотите поделиться' i]",
    "[data-placeholder*='waar wil je' i]",
    "[data-placeholder*='waar je het over' i]",
]

POST_SUBMIT_SELECTORS = [
    "button.share-actions__primary-action",
    "button:has-text('Post')",
    "button:has-text('Опубликовать')",
    "button:has-text('Plaatsen')",
    "button:has-text('Publiceren')",
    "button[aria-label='Post']",
    "button[aria-label='Опубликовать']",
    "button[aria-label='Plaatsen']",
]

GDPR_ACCEPT_SELECTORS = [
    "button[action-type='ACCEPT']",
    "button[data-tracking-control-name*='cookie.consent.accept' i]",
    "button:has-text('Accept')",
    "button:has-text('Accepteren')",
    "button:has-text('Принять')",
    "button:has-text('Akkoord')",
]

ADD_MEDIA_SELECTORS = [
    "button[aria-label*='media' i]",
    "button[aria-label*='photo' i]",
    "button[aria-label*='медиа' i]",
    "button[aria-label*='фото' i]",
    "button[aria-label*='foto' i]",
    "button[data-test-icon='image-medium']",
    ".share-promoted-detour-button button",
    "button:has-text('Add media')",
    "button:has-text('Добавить медиа')",
    "button:has-text('Media toevoegen')",
    "button:has-text('Foto toevoegen')",
]

DONE_AFTER_UPLOAD_SELECTORS = [
    "button.share-box-footer__primary-btn",
    ".image-detour-actions button.share-box-footer__primary-btn",
    "button:has-text('Done')",
    "button:has-text('Next')",
    "button:has-text('Готово')",
    "button:has-text('Далее')",
    "button:has-text('Gereed')",
    "button:has-text('Klaar')",
    "button:has-text('Volgende')",
]


# =========================================================================
# Единый указатель наборов. По этим именам отчитывается верификатор, на них
# ссылаются поверхности в tools/autoheal/surfaces.py и обоснования патчей.
# =========================================================================

NAMED_CHAINS = {
    # вход
    "email": EMAIL_LOCATORS,
    "password": PASSWORD_LOCATORS,
    "submit": SUBMIT_LOCATORS,
    "comply": COMPLY_LOCATORS,
    # профиль
    "top_card": TOP_CARD_SELECTORS,
    # контакт
    "invite_to_connect": [CONNECT_SELECTORS["invite_to_connect"]],
    "more_button": [CONNECT_SELECTORS["more_button"]],
    "connect_option": [CONNECT_SELECTORS["connect_option"]],
    "send_now": [CONNECT_SELECTORS["send_now"]],
    # переписка
    "connections_input": MESSAGE_SELECTOR_CHAINS["connections_input"],
    "search_result_row": MESSAGE_SELECTOR_CHAINS["search_result_row"],
    "compose_input": MESSAGE_SELECTOR_CHAINS["compose_input"],
    "compose_send": MESSAGE_SELECTOR_CHAINS["compose_send"],
    # поиск
    "search_bar": [SEARCH_SELECTORS["search_bar"]],
    "profile_links": [SEARCH_SELECTORS["profile_links"]],
    # публикация
    "start_post": START_POST_SELECTORS,
    "post_editor": POST_EDITOR_SELECTORS,
    "post_submit": POST_SUBMIT_SELECTORS,
    "add_media": ADD_MEDIA_SELECTORS,
    "done_after_upload": DONE_AFTER_UPLOAD_SELECTORS,
    "gdpr_accept": GDPR_ACCEPT_SELECTORS,
}
