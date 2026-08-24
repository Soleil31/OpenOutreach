#!/usr/bin/env python3
"""Делает сохранённую страницу самодостаточной: вшивает внешний CSS внутрь.

Без этого офлайн-проверка врёт. Внешняя таблица стилей — это ровно то, что
прячет одну из двух копий формы входа; без неё обе копии видимы, .first
находит поле, и верификатор выдаёт зелёный на странице, которая в живом
браузере ломала вход.

Запуск: inline_css.py <исходная page.html> <файл css> <куда положить>
"""
import re
import sys
import pathlib


def main():
    src, css_path, dst = (pathlib.Path(p) for p in sys.argv[1:4])
    html = src.read_text(encoding="utf-8", errors="replace")
    css = css_path.read_text(encoding="utf-8", errors="replace")

    links = re.findall(r'<link[^>]*rel="stylesheet"[^>]*>', html)
    if not links:
        print("внешних стилей нет — страница уже самодостаточна")
    html = re.sub(r'<link[^>]*rel="stylesheet"[^>]*>',
                  f"<style>{css}</style>", html, count=1)
    # остальные внешние ссылки убираем, чтобы браузер не ходил в сеть
    html = re.sub(r'<link[^>]*rel="stylesheet"[^>]*>', "", html)
    html = re.sub(r'<script[^>]*src="[^"]*"[^>]*>\s*</script>', "", html)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(html, encoding="utf-8")
    print(f"вшито стилей: {len(links)}; результат: {dst} ({dst.stat().st_size} байт)")


if __name__ == "__main__":
    main()
