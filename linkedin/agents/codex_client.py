"""Python port of owlgram/pkg/codexgateway/exec.go — direct Codex CLI subprocess wrapper."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any


class CodexClient:
    DEFAULT_MODEL = "gpt-5.5"

    def __init__(
        self,
        binary: str = "codex",
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
        work_dir: str | None = None,
        ssl_cert: str | None = None,
        proxy: str | None = None,
    ) -> None:
        self.binary = binary or "codex"
        self.model = self._normalize_model(model) or self.DEFAULT_MODEL
        self.timeout = timeout if timeout > 0 else 120.0
        self.work_dir = work_dir or tempfile.gettempdir()
        self.ssl_cert = (ssl_cert or "").strip()
        self.proxy = (proxy or "").strip()

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
        os.makedirs(self.work_dir, exist_ok=True)

        out_fd, out_path = tempfile.mkstemp(prefix="openoutreach-codex-output-", suffix=".txt")
        os.close(out_fd)

        schema_path: str | None = None
        try:
            args = [
                self.binary, "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox", "read-only",
                "-C", self.work_dir,
                "-m", self.model,
                "-o", out_path,
            ]

            if json_schema is not None:
                s_fd, schema_path = tempfile.mkstemp(
                    prefix="openoutreach-codex-schema-", suffix=".json"
                )
                with os.fdopen(s_fd, "w") as f:
                    json.dump(json_schema, f, indent=2)
                args.extend(["--output-schema", schema_path])

            args.append("-")

            prompt = self._build_prompt(
                system_prompt, user_prompt, json_response, max_tokens, assistant_prefill
            )

            result = subprocess.run(
                args,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.work_dir,
                env=self._build_env(),
            )

            if result.returncode != 0:
                output = (result.stderr + result.stdout).strip()[:2000]
                raise RuntimeError(f"codex exec failed: {output}")

            with open(out_path) as f:
                content = f.read().strip()

            if stop:
                content = self._apply_stop_sequences(content, stop)
            if json_response:
                content = self._strip_json_fence(content)
            if assistant_prefill:
                content = self._strip_generic_fence(content)

            content = content.strip()
            if not content:
                raise RuntimeError("empty response from codex")

            return content

        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
            if schema_path:
                try:
                    os.unlink(schema_path)
                except OSError:
                    pass

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: Any = None,
    ) -> Any:
        content = self.chat(
            system_prompt, user_prompt, json_response=True, json_schema=json_schema
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

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        if self.ssl_cert:
            env["SSL_CERT_FILE"] = self.ssl_cert
        if self.proxy:
            for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                env[key] = self.proxy
        return env

    def _build_prompt(
        self,
        system_prompt: str,
        user_prompt: str,
        json_response: bool,
        max_tokens: int,
        assistant_prefill: str,
    ) -> str:
        lines: list[str] = [
            "You are a stateless text-generation backend for the OpenOutreach service.",
            "Do not inspect files. Do not run shell commands. Do not mention Codex, tools, files, or this wrapper.",
            "Return only the final answer requested by the prompts.",
        ]
        if json_response:
            lines.append(
                "Return exactly one valid JSON object. "
                "No markdown, no code fences, no commentary before or after JSON."
            )
        if max_tokens > 0:
            lines.append(
                f"Keep the response compact; requested max output is about {max_tokens} tokens."
            )
        if assistant_prefill:
            if "html" in assistant_prefill.lower():
                lines.append(
                    "Return a raw HTML fragment only. Do not wrap it in markdown or code fences."
                )
            else:
                lines.append(
                    f"Use this assistant prefill only as a formatting hint: {assistant_prefill}"
                )

        if system_prompt:
            lines += ["", "<system>", system_prompt, "</system>"]
        if user_prompt:
            lines += ["", "<user>", user_prompt, "</user>"]
        else:
            lines += ["", "<user>", "Выполни задачу из системной инструкции.", "</user>"]

        return "\n".join(lines) + "\n"

    @staticmethod
    def _apply_stop_sequences(content: str, stops: list[str]) -> str:
        cut_at = -1
        for stop in stops:
            if not stop:
                continue
            idx = content.find(stop)
            if idx >= 0 and (cut_at == -1 or idx < cut_at):
                cut_at = idx
        return content if cut_at == -1 else content[:cut_at]

    @staticmethod
    def _strip_json_fence(content: str) -> str:
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:].strip().rstrip("`").strip()
        elif content.startswith("```"):
            nl = content.find("\n")
            if nl >= 0:
                content = content[nl + 1:].rstrip("`").strip()
        return content.strip()

    @staticmethod
    def _strip_generic_fence(content: str) -> str:
        content = content.strip()
        if not content.startswith("```"):
            return content
        nl = content.find("\n")
        if nl >= 0:
            content = content[nl + 1:]
        return content.rstrip("`").strip()


def get_codex_client() -> CodexClient:
    """Construct CodexClient from env vars — mirrors owlgram's NewExecClient."""
    return CodexClient(
        binary=os.getenv("CODEX_BIN", "codex"),
        model=os.getenv("CODEX_MODEL", CodexClient.DEFAULT_MODEL),
        timeout=float(os.getenv("CODEX_TIMEOUT_SECONDS", "120")),
        work_dir=os.getenv("CODEX_WORKDIR") or tempfile.gettempdir(),
        ssl_cert=os.getenv("CODEX_SSL_CERT_FILE", ""),
        proxy=os.getenv("CODEX_PROXY") or os.getenv("CODEX_HTTPS_PROXY", ""),
    )
