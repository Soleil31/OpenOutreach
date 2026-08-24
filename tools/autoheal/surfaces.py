# tools/autoheal/surfaces.py
"""Поверхности конвейера — что именно может сломаться и как это проверить.

Вход в аккаунт — лишь одна из точек, где LinkedIn может сменить вёрстку. Ломается
всё: карточка профиля, кнопка «Connect», окно переписки, выдача поиска, редактор
публикации. Поверхность описывает страницу конвейера: какие наборы правил на ней
обязаны сработать, как узнать её среди сохранённых улик и что означает поломка.

Добавление новой поверхности — одна запись здесь. Ни детектор, ни верификатор,
ни хилер при этом не меняются.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Surface:
    key: str
    title: str
    # наборы правил из linkedin/browser/selectors.py, обязательные на этой странице
    required_chains: tuple[str, ...]
    # Признаки, по которым образец относят к этой поверхности.
    # dom_markers — НАСТОЯЩИЕ селекторы, проверяемые через DOM, а не подстроки:
    # поиск подстрокой по HTML ловится на содержимое <style>, из-за чего форма
    # входа со вшитым CSS определялась как страница установления контакта.
    url_markers: tuple[str, ...] = ()
    dom_markers: tuple[str, ...] = ()
    # что встанет, если поверхность сломается
    breaks: str = ""
    # наборы, которые желательны, но их отсутствие не делает вердикт красным
    optional_chains: tuple[str, ...] = field(default_factory=tuple)


SURFACES: tuple[Surface, ...] = (
    Surface(
        key="login",
        title="Форма входа",
        required_chains=("email", "password", "submit"),
        url_markers=("/login", "/uas/login", "/checkpoint/lg"),
        dom_markers=('input[type="password"]',),
        breaks="аккаунт не входит — встаёт весь конвейер",
    ),
    Surface(
        key="profile",
        title="Карточка профиля",
        required_chains=("top_card",),
        url_markers=("/in/",),
        dom_markers=('section[data-member-id]', 'div[class*="pv-top-card"]',
                     'div.top-card-background-hero-image'),
        breaks="профили не разбираются — нет квалификации и нет лидов",
    ),
    Surface(
        key="connect",
        title="Установление контакта",
        required_chains=("invite_to_connect",),
        optional_chains=("more_button", "connect_option", "send_now"),
        url_markers=("/in/",),
        dom_markers=('[aria-label*="Invite"][aria-label*="to connect"]',
                     'button[aria-label*="More actions"]'),
        breaks="заявки в контакты не уходят — воронка не пополняется",
    ),
    Surface(
        key="messaging",
        title="Переписка",
        required_chains=("compose_input", "compose_send"),
        optional_chains=("connections_input", "search_result_row"),
        url_markers=("/messaging",),
        dom_markers=('div[class*="msg-form"]',
                     'div[role="textbox"][aria-label*="message" i]'),
        breaks="сообщения не отправляются — диалоги обрываются на полуслове",
    ),
    Surface(
        key="search",
        title="Поиск людей",
        required_chains=("profile_links",),
        optional_chains=("search_bar",),
        url_markers=("/search/results", "/search/people"),
        dom_markers=('div[class*="search-results"]', 'ul[class*="reusable-search"]'),
        breaks="новые лиды не находятся — конвейер пустеет",
    ),
    Surface(
        key="post",
        title="Публикация",
        required_chains=("start_post", "post_editor", "post_submit"),
        optional_chains=("add_media", "done_after_upload", "gdpr_accept"),
        url_markers=("/feed",),
        dom_markers=('.share-box-feed-entry__trigger', '.ql-editor'),
        breaks="публикации не выходят — падает прогрев аккаунта",
    ),
)

BY_KEY = {s.key: s for s in SURFACES}

# Страницы, которые вообще не являются рабочей поверхностью: чинить в них нечего.
BLOCKING_REASONS = ("checkpoint_2fa", "captcha", "bad_credentials", "proxy_blocked")


ORIGIN_META = "oo-origin-url"


def origin_url(html: str) -> str:
    """Исходный адрес страницы, вписанный архиватором при сохранении образца."""
    import re
    match = re.search(
        rf'<meta[^>]*name="{ORIGIN_META}"[^>]*content="([^"]*)"', html or "", re.I)
    return match.group(1) if match else ""


def classify_by_url(url: str) -> str | None:
    url = (url or "").lower()
    if not url:
        return None
    for surface in SURFACES:
        if any(marker in url for marker in surface.url_markers):
            return surface.key
    return None


def classify_by_dom(page) -> str | None:
    """По присутствию настоящих элементов, а не по тексту разметки."""
    best, best_hits = None, 0
    for surface in SURFACES:
        hits = 0
        for marker in surface.dom_markers:
            try:
                if page.locator(marker).count() > 0:
                    hits += 1
            except Exception:
                continue
        if hits > best_hits:
            best, best_hits = surface.key, hits
    return best


def classify_sample(url: str, html: str, page=None) -> str | None:
    """К какой поверхности относится образец. None — не определено.

    Порядок: сначала адрес (вписанный при сохранении), затем настоящий DOM.
    Файл на диске своего адреса не помнит, поэтому архиватор вписывает его
    в <head> при сохранении образца.
    """
    key = classify_by_url(url) or classify_by_url(origin_url(html))
    if key:
        return key
    if page is not None:
        return classify_by_dom(page)
    return None


def describe() -> str:
    lines = []
    for surface in SURFACES:
        chains = ", ".join(surface.required_chains)
        lines.append(f"{surface.key:<10} {surface.title:<24} обязательны: {chains}")
        lines.append(f"{'':<10} если сломается: {surface.breaks}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
