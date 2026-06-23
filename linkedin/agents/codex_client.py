"""HTTP client for the owlgram codex-gateway.

Replaces the previous local ``codex exec`` subprocess wrapper. Instead of
running the Codex CLI on each server (which needs a per-host ``auth.json``
that expires and conflicts when shared), we POST to a central
codex-gateway over an SSH tunnel. The gateway holds the ChatGPT auth in
one place and refreshes it itself — so the bots never see a 401.

Wire protocol (owlgram/pkg/codexgateway/client.go):
    POST {CODEX_GATEWAY_URL}/v1/chat
    body  {model, system_prompt, user_prompt, json_response, json_schema, ...}
    resp  {content, provider, model, usage}

Reachability: the gateway listens on 127.0.0.1:18789 on OwlwebMain; an
autossh systemd unit on each LinkedIn server forwards it to the host's
0.0.0.0:18789, and the openoutreach container reaches it via
``host.docker.internal`` (extra_hosts: host-gateway).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class CodexClient:
    DEFAULT_MODEL = "gpt-5.5"

    def __init__(
        self,
        gateway_url: str,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
        token: str = "",
    ) -> None:
        self.gateway_url = (gateway_url or "").rstrip("/")
        self.model = self._normalize_model(model) or self.DEFAULT_MODEL
        self.timeout = timeout if timeout > 0 else 120.0
        self.token = (token or "").strip()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        json_response: bool = False,
        max_tokens: int = 0,
        stop: list[str] | None = None,
        assistant_prefill: str = "",
        json_schema: Any = None,
    ) -> str:
        if not self.gateway_url:
            raise RuntimeError("CODEX_GATEWAY_URL is not set")

        payload: dict[str, Any] = {
            "model": self.model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "json_response": json_response,
        }
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens
        if stop:
            payload["stop"] = stop
        if assistant_prefill:
            payload["assistant_prefill"] = assistant_prefill
        if json_schema is not None:
            # OpenAI structured-output requires additionalProperties:false on
            # every object node; Pydantic doesn't emit it, so we inject it
            # before sending. Harmless if the gateway also strictifies.
            payload["json_schema"] = _strictify_schema(json_schema)

        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(
            f"{self.gateway_url}/v1/chat", data=data, headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(
                f"codex gateway HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"codex gateway unreachable: {exc.reason}") from exc

        parsed = json.loads(body)
        content = (parsed.get("content") or "").strip()
        if not content:
            raise RuntimeError("empty response from codex gateway")
        return content

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: Any = None,
    ) -> Any:
        content = self.chat(
            system_prompt, user_prompt, json_response=True, json_schema=json_schema,
        )
        return json.loads(content)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_model(model: str) -> str:
        model = (model or "").strip()
        model = model.removeprefix("openai-codex/")
        model = model.removeprefix("codex/")
        return model


def get_codex_client() -> CodexClient:
    """Construct a CodexClient pointed at the codex-gateway.

    ``CODEX_GATEWAY_URL`` defaults to the autossh-forwarded gateway on the
    docker host. ``CODEX_GATEWAY_TOKEN`` is optional (the gateway runs with
    authorization disabled in our setup).
    """
    return CodexClient(
        gateway_url=os.getenv("CODEX_GATEWAY_URL", "http://host.docker.internal:18789"),
        model=os.getenv("CODEX_MODEL", CodexClient.DEFAULT_MODEL),
        timeout=float(os.getenv("CODEX_TIMEOUT_SECONDS", "120")),
        token=os.getenv("CODEX_GATEWAY_TOKEN", ""),
    )


def _strictify_schema(schema):
    """Return a deep copy of ``schema`` with ``additionalProperties: false``
    and a complete ``required`` list forced on every object-type subschema.

    OpenAI's structured-output JSON-schema validator rejects schemas that
    don't explicitly disallow extra properties AND that don't mark every
    property as required. Pydantic's ``model_json_schema()`` emits neither,
    so we inject both ourselves — otherwise the gateway rejects the schema
    (``codex exec`` exits 1 → HTTP 502) and every schema-typed call (e.g.
    fact extraction in follow-ups) fails. Walks ``properties``, ``items``,
    ``anyOf``/``oneOf``/``allOf``, and ``$defs`` so nested models work too.
    """
    import copy

    def _walk(node):
        if isinstance(node, dict):
            t = node.get("type")
            if t == "object" and "additionalProperties" not in node:
                node["additionalProperties"] = False
            # OpenAI strict mode requires `required` to list every property.
            if t == "object" and isinstance(node.get("properties"), dict):
                node["required"] = list(node["properties"].keys())
            for key in ("properties", "patternProperties", "$defs", "definitions"):
                inner = node.get(key)
                if isinstance(inner, dict):
                    for v in inner.values():
                        _walk(v)
            for key in ("items", "contains", "additionalItems"):
                inner = node.get(key)
                if isinstance(inner, (dict, list)):
                    _walk(inner)
            for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
                inner = node.get(key)
                if isinstance(inner, list):
                    for v in inner:
                        _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    out = copy.deepcopy(schema)
    _walk(out)
    return out
