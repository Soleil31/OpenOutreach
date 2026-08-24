#!/usr/bin/env python3
"""Офлайн-проверка селекторов на сохранённых страницах — по всем поверхностям.

Открывает образец через file:// и проверяет, находятся ли элементы, нужные
именно этой странице: на форме входа — поля и кнопка, на карточке профиля —
верхняя карточка, в переписке — поле ввода и отправка, в поиске — ссылки на
профили. Сети нет.

Это контур, который делает автопочинку безопасной: патч проверяется на реальной
вёрстке, не трогая LinkedIn.

ВАЖНО: сырой page.html не годится — стили LinkedIn лежат по внешней ссылке, без
них скрытые копии элементов читаются как видимые, и сломанный код проходит
проверку. Прогоняй образцы через inline_css.py. Подробности в README.

Запуск:  python verify_locators.py <каталог-корпуса>
Код 0 — все образцы прошли.
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, "/app")

from linkedin.browser.login import classify_login_failure  # noqa: E402
from linkedin.browser.nav import resolve_locator  # noqa: E402
from linkedin.browser import selectors as sel  # noqa: E402

# tools/ монтируется в контейнер рядом с приложением, поэтому /app в sys.path
# уже достаточно. Путь от __file__ не годится: скрипт подключается как /tmp/v.py.
from tools.autoheal import surfaces  # noqa: E402


def _resolve_chain(page, name, timeout_ms=2000):
    """Ищет по набору правил. Возвращает (найдено, пояснение)."""
    chain = sel.NAMED_CHAINS.get(name)
    if not chain:
        return False, f"набора «{name}» нет в слое селекторов"

    # наборы бывают двух видов: функции от страницы и строки CSS/XPath
    factories = []
    for rule in chain:
        if callable(rule):
            factories.append(rule)
        else:
            factories.append(lambda p, r=rule: p.locator(r))
    try:
        locator = resolve_locator(page, factories, timeout_per_ms=timeout_ms)
    except RuntimeError as exc:
        return False, str(exc)[:80]

    try:
        if not locator.is_visible():
            return False, "элемент найден, но не виден"
    except Exception:
        pass
    return True, ""


def check_page(page, path: pathlib.Path):
    page.goto(path.as_uri(), wait_until="domcontentloaded")

    reason, _detail = classify_login_failure(page)
    if reason in surfaces.BLOCKING_REASONS:
        return None, {"классификация": (f"OK — {reason}",
                                        "не рабочая страница, автопочинка неприменима")}

    html = ""
    try:
        html = page.content()
    except Exception:
        pass
    key = surfaces.classify_sample(page.url, html, page=page)
    if key is None:
        return None, {"поверхность": ("НЕ ОПОЗНАНА", "образец не отнесён ни к одной странице")}

    surface = surfaces.BY_KEY[key]
    results = {}
    for name in surface.required_chains:
        found, why = _resolve_chain(page, name)
        results[name] = ("OK" if found else "НЕ НАЙДЕН", why)
    for name in surface.optional_chains:
        found, why = _resolve_chain(page, name, timeout_ms=1000)
        results[name + " (необяз.)"] = ("OK" if found else "нет", why)
    return surface, results


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/corpus")
    pages = sorted(p for p in root.rglob("*.html"))
    if not pages:
        print(f"В {root} нет ни одного .html — проверять нечего")
        return 2

    print(f"Образцов в корпусе: {len(pages)}\n")
    failed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
        context = browser.new_context()
        context.route("**", lambda route: route.abort()
                      if not route.request.url.startswith("file:") else route.continue_())
        page = context.new_page()
        for path in pages:
            surface, res = check_page(page, path)
            # необязательные наборы на вердикт не влияют
            bad = [k for k, (v, _) in res.items()
                   if not v.startswith("OK") and "(необяз.)" not in k]
            if bad:
                failed += 1
            mark = "✓" if not bad else "✗"
            title = f" [{surface.title}]" if surface else ""
            print(f"{mark} {path.name}{title}")
            for name, (verdict, detail) in res.items():
                print(f"     {name:<24} {verdict}{('  — ' + detail) if detail else ''}")
        context.close()
        browser.close()

    print(f"\nИтог: {len(pages) - failed} из {len(pages)} образцов прошли проверку")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
