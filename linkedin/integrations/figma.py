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
