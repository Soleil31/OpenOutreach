# tools/autoheal/gateway.py
"""Единственная дверь к модели.

Была вшита в хилер; с появлением разбора отказов её понадобилось звать из двух
мест, а два клиента к одному шлюзу — это два набора таймаутов, заголовков и
способов проглядеть ошибку.

Модель здесь не получает ни доступа к базе, ни учётных данных, ни сети
LinkedIn — только то, что ей кладут в подсказку.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from tools.autoheal import config


class GatewayUnavailable(RuntimeError):
    """Шлюз недоступен или ответил не по схеме — это не вина ответа модели."""


def ask(system_prompt: str, user_prompt: str, schema: dict,
        required: tuple[str, ...]) -> dict:
    """Строгий JSON по схеме. Любая неожиданность — GatewayUnavailable."""
    payload = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "json_response": True,
        "json_schema": schema,
        "model": config.CODEX_MODEL,
    }
    request = urllib.request.Request(
        config.CODEX_GATEWAY_URL.rstrip("/") + "/v1/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if config.CODEX_GATEWAY_TOKEN:
        request.add_header("Authorization", "Bearer " + config.CODEX_GATEWAY_TOKEN)

    try:
        with urllib.request.urlopen(request, timeout=config.CODEX_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise GatewayUnavailable(f"шлюз недоступен: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise GatewayUnavailable(f"шлюз ответил неожиданно: {exc}") from exc

    try:
        answer = json.loads(body.get("content", ""))
    except json.JSONDecodeError as exc:
        raise GatewayUnavailable(f"ответ не разобрался как JSON: {exc}") from exc

    for field in required:
        if field not in answer:
            raise GatewayUnavailable(f"в ответе нет поля {field}")
    return answer
