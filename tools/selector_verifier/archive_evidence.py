#!/usr/bin/env python3
"""Архиватор улик: сохраняет по одному образцу на каждую вёрстку LinkedIn.

Пакеты диагностики пишутся в /tmp контейнера, а /tmp чистится кроном — то есть
доказательства поломок испаряются. На 24.08.2026 в наличии было 43 пакета, все
за один день и одну вёрстку. Без корпуса верификатору нечего проверять, а
хилеру не на чем учиться.

Скрипт разбирает свежие пакеты, считает отпечаток вёрстки и складывает НОВЫЕ
вёрстки в постоянный корпус. Дубликаты той же вёрстки отбрасываются, поэтому
корпус растёт по одному образцу на редизайн, а не на каждое падение.

Образец приводится к самодостаточному виду: внешние стили вшиваются внутрь.
Без этого проверка врёт — см. README.

Запуск (обычно из крона раз в час):
    archive_evidence.py --source /var/openoutreach-tmp/openoutreach-diagnostics \\
                        --corpus /var/lib/openoutreach-corpus
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import urllib.request

SCRUB_PATTERNS = [
    # значения полей ввода — на всякий случай, LinkedIn их обычно и так не пишет
    (re.compile(r'(<input[^>]*\svalue=")([^"]*)(")', re.I), r'\1\3'),
    # любые токены в аттрибутах
    (re.compile(r'(csrf[-_]?token"?\s*[:=]\s*"?)([^"&,\s]{8,})', re.I), r'\1REMOVED'),
]

SENSITIVE = re.compile(r'li_at|JSESSIONID|bcookie|Set-Cookie', re.I)


def fingerprint(html: str) -> str:
    """Отпечаток вёрстки: структура формы, а не её случайные идентификаторы.

    React-id на этой странице меняются на каждом рендере, поэтому в отпечаток
    берётся только то, что переживает перерисовку: набор типов полей, имена
    аттрибутов и классы контейнеров.
    """
    inputs = sorted(re.findall(r'<input[^>]*type="([a-z]+)"', html, re.I))
    autos = sorted(set(re.findall(r'autocomplete="([^"]+)"', html, re.I)))
    attrs = sorted(set(re.findall(r'\s(componentkey|data-[a-z-]+)=', html, re.I)))
    buttons = len(re.findall(r'<button', html, re.I))
    basis = "|".join(inputs + autos + attrs) + f"|buttons={buttons}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def external_css_urls(html: str) -> list[str]:
    return re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', html, re.I)


# CDN LinkedIn отвечает 403 на Python-urllib — нужен обычный заголовок браузера.
CSS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"),
    "Accept": "text/css,*/*;q=0.1",
}


def _fetch_css(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers=CSS_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def make_self_contained(html: str, fetch=_fetch_css) -> tuple[str, int]:
    """Вшивает внешние стили в страницу и убирает остальные внешние ссылки."""
    inlined = 0
    for url in external_css_urls(html):
        if not url.startswith("http"):
            continue
        try:
            css = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! не удалось забрать {url}: {exc}", file=sys.stderr)
            continue
        html = html.replace(
            next(link for link in re.findall(r'<link[^>]*rel="stylesheet"[^>]*>', html, re.I)
                 if url in link),
            f"<style>{css}</style>", 1)
        inlined += 1
    html = re.sub(r'<link[^>]*rel="stylesheet"[^>]*>', "", html, flags=re.I)
    html = re.sub(r'<script[^>]*src="[^"]*"[^>]*>\s*</script>', "", html, flags=re.I)
    return html, inlined


def scrub(html: str) -> str:
    for pattern, repl in SCRUB_PATTERNS:
        html = pattern.sub(repl, html)
    return html


def load_manifest(corpus: pathlib.Path) -> dict:
    path = corpus / "manifest.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"samples": {}}


def save_manifest(corpus: pathlib.Path, manifest: dict) -> None:
    (corpus / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="каталог пакетов диагностики")
    parser.add_argument("--corpus", required=True, help="постоянный корпус")
    parser.add_argument("--dry-run", action="store_true", help="только показать, что было бы добавлено")
    args = parser.parse_args()

    source = pathlib.Path(args.source)
    corpus = pathlib.Path(args.corpus)
    corpus.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(corpus)
    known = set(manifest["samples"])
    added = skipped = 0

    for page in sorted(source.rglob("page.html")):
        raw = page.read_text(encoding="utf-8", errors="replace")
        mark = fingerprint(raw)
        if mark in known:
            skipped += 1
            continue

        print(f"новая вёрстка {mark} ← {page.parent.name}")
        if args.dry_run:
            known.add(mark)
            added += 1
            continue

        html, inlined = make_self_contained(raw)
        if inlined == 0 and external_css_urls(raw):
            print("  ! стили не забрались — образец не самодостаточен, пропускаю", file=sys.stderr)
            continue
        html = scrub(html)
        if SENSITIVE.search(html):
            print("  ! в образце остались чувствительные данные, пропускаю", file=sys.stderr)
            continue

        name = f"{page.parent.name[:19]}_{mark}.html"
        (corpus / name).write_text(html, encoding="utf-8")
        manifest["samples"][mark] = {
            "file": name,
            "source": page.parent.name,
            "inlined_stylesheets": inlined,
            "bytes": len(html),
        }
        known.add(mark)
        added += 1
        print(f"  → {name} ({len(html)} байт, стилей вшито: {inlined})")

    if not args.dry_run:
        save_manifest(corpus, manifest)

    print(f"\nдобавлено вёрсток: {added}, пропущено как известные: {skipped}, "
          f"всего в корпусе: {len(manifest['samples'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
