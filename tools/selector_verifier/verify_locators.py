#!/usr/bin/env python3
"""Офлайн-проверка локаторов входа на сохранённых страницах.

Открывает page.html из пакета диагностики через file:// и проверяет, находятся
ли поля формы входа. Сети нет: страница локальная, внешние запросы блокируются.
Это тот самый контур, который делает автопочинку безопасной, — патч проверяется
на реальной вёрстке, не трогая LinkedIn.

Запуск:  python verify_locators.py <каталог-корпуса>
Код 0 — все обязательные элементы найдены на всех образцах.
"""
import sys
import pathlib

from playwright.sync_api import sync_playwright

sys.path.insert(0, "/app")

from linkedin.browser.login import (  # noqa: E402
    EMAIL_LOCATORS,
    PASSWORD_LOCATORS,
    SUBMIT_LOCATORS,
    classify_login_failure,
)
from linkedin.browser.nav import resolve_locator  # noqa: E402

REQUIRED = [
    ("email", EMAIL_LOCATORS),
    ("password", PASSWORD_LOCATORS),
    ("submit", SUBMIT_LOCATORS),
]

# Причины, при которых страница вообще не является формой входа. Патч селекторов
# такой случай не лечит — его место в маршруте «зовём человека».
NON_LOGIN_REASONS = {"checkpoint_2fa", "captcha", "proxy_blocked", "bad_credentials"}


def check_page(page, path: pathlib.Path):
    """Проверяет образец сообразно тому, что это за страница.

    Не всякий сохранённый снимок — форма входа. Среди улик попадаются страницы
    проверки безопасности: полей там нет и никаким патчем они не появятся.
    Требовать от такого образца поле пароля бессмысленно — вместо этого
    проверяем, что классификатор правильно назвал причину и не отправил случай
    на автопочинку.
    """
    page.goto(path.as_uri(), wait_until="domcontentloaded")

    reason, _detail = classify_login_failure(page)
    if reason in NON_LOGIN_REASONS:
        return {"классификация": ("OK — " + reason, "не форма входа, автопочинка неприменима")}

    results = {}
    for name, chain in REQUIRED:
        try:
            loc = resolve_locator(page, chain, timeout_per_ms=2000)
            visible = loc.is_visible()
            enabled = True
            try:
                enabled = loc.is_enabled()
            except Exception:
                pass
            results[name] = ("OK" if (visible and enabled) else "НЕ ГОТОВ", "")
        except RuntimeError as exc:
            results[name] = ("НЕ НАЙДЕН", str(exc)[:70])
    return results


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/corpus")
    # корпус хранит образцы под говорящими именами, сырые пакеты диагностики —
    # под page.html; принимаем и то и другое
    pages = sorted(p for p in root.rglob("*.html"))
    if not pages:
        print(f"В {root} нет ни одного .html — проверять нечего")
        return 2

    print(f"Образцов в корпусе: {len(pages)}\n")
    failed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
        context = browser.new_context()
        # никаких внешних запросов: страница проверяется как есть
        context.route("**", lambda route: route.abort()
                      if not route.request.url.startswith("file:") else route.continue_())
        page = context.new_page()
        for path in pages:
            res = check_page(page, path)
            bad = [k for k, (v, _) in res.items() if not v.startswith("OK")]
            mark = "✓" if not bad else "✗"
            if bad:
                failed += 1
            print(f"{mark} {path.name}")
            for name, (verdict, detail) in res.items():
                print(f"     {name:<10} {verdict}{('  — ' + detail) if detail else ''}")
        context.close()
        browser.close()

    print(f"\nИтог: {len(pages) - failed} из {len(pages)} образцов прошли проверку")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
