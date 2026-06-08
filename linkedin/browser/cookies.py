# linkedin/browser/cookies.py
"""Helpers for the LinkedInProfile cookie-import admin field.

Two functions are exposed because ``linkedin/admin.py`` imports them:
  * ``normalise_cookie_export(raw)`` — accept either:
      - a Playwright ``storage_state`` dict (``{cookies: [...], origins: [...]}``)
      - or a flat cookie list (browser-extension exports), and return a
        well-formed Playwright ``storage_state`` dict. Raises ``ValueError``
        on parse / shape failures so the admin form can surface the message.
  * ``storage_state_summary(state)`` — short human-readable status used in
    the admin list/detail view (``"13 cookies, li_at expires 2027-06-04Z"``
    or ``"empty"`` / ``"no session"``).
"""
from __future__ import annotations

import datetime
import json
from typing import Any


def normalise_cookie_export(raw: Any) -> dict:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            raise ValueError("Cookie export is empty")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Cookie export is not valid JSON: {exc}") from exc
    else:
        data = raw

    if isinstance(data, dict) and "cookies" in data:
        cookies = data.get("cookies") or []
        origins = data.get("origins") or []
    elif isinstance(data, list):
        cookies = data
        origins = []
    else:
        raise ValueError(
            "Cookie export must be a JSON object with a 'cookies' key or a list of cookies"
        )

    cleaned: list[dict] = []
    for raw_cookie in cookies:
        if not isinstance(raw_cookie, dict):
            raise ValueError(f"Cookie entry must be an object, got {type(raw_cookie).__name__}")
        if "name" not in raw_cookie or "value" not in raw_cookie:
            raise ValueError("Each cookie must have 'name' and 'value'")
        # Some browser-export tools use different keys; normalize them.
        domain = raw_cookie.get("domain") or raw_cookie.get("host_key") or ""
        path = raw_cookie.get("path") or "/"
        expires = raw_cookie.get("expires", raw_cookie.get("expiry", -1))
        try:
            expires = float(expires)
        except (TypeError, ValueError):
            expires = -1.0
        cleaned.append({
            "name": raw_cookie["name"],
            "value": raw_cookie["value"],
            "domain": domain,
            "path": path,
            "expires": expires,
            "httpOnly": bool(raw_cookie.get("httpOnly", raw_cookie.get("is_httponly", False))),
            "secure": bool(raw_cookie.get("secure", raw_cookie.get("is_secure", False))),
            "sameSite": raw_cookie.get("sameSite", "Lax"),
        })

    if not cleaned:
        raise ValueError("Cookie export has no usable cookies")

    return {"cookies": cleaned, "origins": origins if isinstance(origins, list) else []}


def storage_state_summary(state: Any) -> str:
    if state is None:
        return "no session"
    if isinstance(state, str):
        try:
            state = json.loads(state)
        except json.JSONDecodeError:
            return "invalid JSON"
    if not isinstance(state, dict):
        return "invalid"
    cookies = state.get("cookies") or []
    if not cookies:
        return "empty"
    li_at = next((c for c in cookies if isinstance(c, dict) and c.get("name") == "li_at"), None)
    parts = [f"{len(cookies)} cookies"]
    if li_at is not None:
        exp = li_at.get("expires")
        try:
            exp = float(exp)
        except (TypeError, ValueError):
            exp = -1
        if exp and exp > 0:
            try:
                ts = datetime.datetime.utcfromtimestamp(exp).strftime("%Y-%m-%dZ")
                parts.append(f"li_at expires {ts}")
            except (OverflowError, OSError, ValueError):
                parts.append("li_at present")
        else:
            parts.append("li_at session-only")
    return ", ".join(parts)
