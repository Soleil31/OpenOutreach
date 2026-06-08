import hashlib
import os
import time
from pathlib import Path

import httpx

FIGMA_API_BASE = "https://api.figma.com/v1"
TEMPLATE_PAGE_NAME = "Шаблон"
CACHE_TTL_SECONDS = 2 * 60 * 60
_CACHE_DIR = Path(os.getenv("FIGMA_CACHE_DIR", "/app/data/figma_cache"))


class FigmaError(Exception):
    pass


def get_template_png(file_key: str, token: str, force: bool = False) -> Path:
    """Return path to cached PNG of the template frame, fetching if stale."""
    path = _cache_path(file_key)
    if not force and _is_fresh(path):
        return path
    return _download_and_cache(file_key, token, path)


def refresh_template(file_key: str, token: str) -> Path:
    """Force-refresh the cached template and return its path."""
    return get_template_png(file_key, token, force=True)


def _download_and_cache(file_key: str, token: str, path: Path) -> Path:
    node_id = _fetch_template_frame_node_id(file_key, token)
    png_bytes = _export_frame_png(file_key, node_id, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes)
    return path


def _fetch_template_frame_node_id(file_key: str, token: str) -> str:
    url = f"{FIGMA_API_BASE}/files/{file_key}?depth=2"
    data = _figma_get(url, token)
    pages = data.get("document", {}).get("children", [])
    for page in pages:
        if page.get("name") == TEMPLATE_PAGE_NAME:
            for child in page.get("children", []):
                if child.get("type") == "FRAME":
                    return child["id"]
            raise FigmaError(
                f"No FRAME found on page '{TEMPLATE_PAGE_NAME}' in file {file_key}"
            )
    page_names = [p.get("name") for p in pages]
    raise FigmaError(
        f"Page '{TEMPLATE_PAGE_NAME}' not found in Figma file {file_key}. "
        f"Available pages: {page_names}"
    )


def _export_frame_png(file_key: str, node_id: str, token: str) -> bytes:
    url = (
        f"{FIGMA_API_BASE}/images/{file_key}"
        f"?ids={node_id}&format=png&scale=2"
    )
    data = _figma_get(url, token)
    images = data.get("images", {})
    png_url = images.get(node_id)
    if not png_url:
        raise FigmaError(f"Figma did not return an image URL for node {node_id}")
    response = httpx.get(png_url, timeout=60, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _figma_get(url: str, token: str) -> dict:
    response = httpx.get(
        url,
        headers={"X-Figma-Token": token},
        timeout=30,
    )
    if response.status_code == 403:
        raise FigmaError("Figma API returned 403 — check your token")
    if response.status_code == 404:
        raise FigmaError(f"Figma resource not found: {url}")
    response.raise_for_status()
    return response.json()


def _cache_path(file_key: str) -> Path:
    name = hashlib.sha1(file_key.encode()).hexdigest()[:16]
    return _CACHE_DIR / f"{name}.png"


def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS


# ── Cover image composition ────────────────────────────────────────────


_COVER_OUT_DIR = Path(os.getenv("FIGMA_COVER_DIR", "/app/data/figma_covers"))
_FONT_CANDIDATES = (
    # Common fonts inside the Playwright/python image (DejaVu installed by libfontconfig1).
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)


def compose_cover(template_path: Path, cover_text: str, post_id: int) -> Path:
    """Render ``cover_text`` onto the Figma template PNG and return the output path.

    Output is a PNG written to ``$FIGMA_COVER_DIR/<post_id>.png`` (default
    ``/app/data/figma_covers``). The text is centered inside the lower 30% of
    the image, wrapped to fit the canvas width, and rendered in white with a
    soft drop-shadow for legibility on photographic backgrounds.

    Crashes on unexpected errors per project convention (no silent fallback).
    """
    from PIL import Image, ImageDraw, ImageFont  # pillow

    if not template_path.exists():
        raise FigmaError(f"Template PNG missing: {template_path}")

    _COVER_OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _COVER_OUT_DIR / f"{post_id}.png"

    img = Image.open(template_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    text = (cover_text or "").strip()
    if text:
        font = _load_font(int(img.width * 0.06))  # ~6% of width
        wrapped = _wrap_to_width(text, font, max_width=int(img.width * 0.85), draw=draw)

        line_height = font.size + 8
        block_h = line_height * len(wrapped)
        # bottom 30% — vertical anchor centered in that band
        y0 = int(img.height * 0.65) + (int(img.height * 0.30) - block_h) // 2

        for i, line in enumerate(wrapped):
            bbox = draw.textbbox((0, 0), line, font=font)
            line_w = bbox[2] - bbox[0]
            x = (img.width - line_w) // 2
            y = y0 + i * line_height
            # drop shadow for legibility
            draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 180))
            draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))

    composed = Image.alpha_composite(img, overlay).convert("RGB")
    composed.save(out_path, format="PNG", optimize=True)
    return out_path


def _load_font(size: int):
    from PIL import ImageFont  # pillow

    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    # Last-resort default (no truetype) — will still render, just less pretty.
    return ImageFont.load_default()


def _wrap_to_width(text: str, font, max_width: int, draw) -> list[str]:
    """Greedy word-wrap that keeps each line under ``max_width`` pixels."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
