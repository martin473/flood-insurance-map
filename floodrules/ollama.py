"""Ollama models through /api/chat: Ollama Cloud (https://ollama.com) or a local server.

Ollama Cloud is used when OLLAMA_API_KEY is set (sent as a Bearer token), unless
OLLAMA_HOST points somewhere else. Model names are the ones `floodrules models` lists.

Compared with the Claude path:
- No tool calling. The schema goes in `format` to constrain output to valid JSON, and is
  also written into the prompt. If an endpoint rejects `format`, the call is retried
  without it; the reply is still parsed and schema-validated, so nothing weaker gets through.
- Truncation. A local Ollama silently cuts prompts longer than num_ctx, and its default is
  small, so for local hosts num_ctx is sized to the document. For every host, a prompt token
  count far below the document's size is treated as truncation and raised, instead of
  scoring a model that never saw part of the guide.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

import requests

from .claude import ModelError, Usage

CLOUD_HOST = "https://ollama.com"
LOCAL_HOST = "http://localhost:11434"
MAX_CTX = 131_072
MAX_OUTPUT_TOKENS = int(os.environ.get("FLOODRULES_MAX_OUTPUT_TOKENS", "16384"))
MAX_CHARS_PER_TOKEN = 8.0   # above this many chars per counted token, part of the prompt wasn't read

Transport = Callable[[str, str, dict[str, Any] | None, dict[str, str]], tuple[int, dict[str, Any], dict[str, str]]]


class OllamaError(ModelError):
    pass


def _requests_transport(method: str, url: str, body: dict[str, Any] | None, headers: dict[str, str]) -> tuple[int, dict[str, Any], dict[str, str]]:
    try:
        r = requests.request(method, url, json=body, headers=headers, timeout=1800)
    except requests.ConnectionError as e:
        host = url.rsplit("/api", 1)[0]
        hint = "Check your network connection." if host == CLOUD_HOST else "Start it with `ollama serve`."
        raise OllamaError(f"Can't reach Ollama at {host}. {hint}") from e
    try:
        data = r.json()
    except ValueError:
        data = {"error": r.text[:500]}
    return r.status_code, data, dict(r.headers)


def context_size(prompt_chars: int) -> int:
    need = prompt_chars // 3 + MAX_OUTPUT_TOKENS + 1024
    size = 8192
    while size < need and size < MAX_CTX:
        size *= 2
    return min(size, MAX_CTX)


class Ollama:
    def __init__(self, model: str, host: str | None = None, api_key: str | None = None, transport: Transport | None = None,
                 num_ctx: int | None = None, max_retries: int = 4, sleep: Callable[[float], None] = time.sleep):
        self.model = model
        self.api_key = api_key if api_key is not None else os.environ.get("OLLAMA_API_KEY", "")
        host = host or os.environ.get("OLLAMA_HOST") or (CLOUD_HOST if self.api_key else LOCAL_HOST)
        self.host = (host if host.startswith("http") else "http://" + host).rstrip("/")
        self.transport = transport or _requests_transport
        self.num_ctx = num_ctx
        self.max_retries = max_retries
        self.sleep = sleep
        self.used_format: bool | None = None   # False if the endpoint rejected `format`
        self.used_think_off: bool | None = None   # False if the model doesn't accept `think`

    @property
    def is_cloud(self) -> bool:
        return self.host == CLOUD_HOST

    def _headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self.api_key:
            h["authorization"] = f"Bearer {self.api_key}"
        return h

    def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            status, data, headers = self.transport(method, f"{self.host}{path}", body, self._headers())
            if status == 200:
                return data
            err = str(data.get("error", data))
            if status == 401 or status == 403:
                raise OllamaError("Ollama rejected the API key. Check OLLAMA_API_KEY." if self.is_cloud
                                  else f"Ollama refused the request ({status}): {err}")
            if status == 404 or "not found" in err.lower():
                hint = "Run `floodrules models` to see the names your key can use." if self.is_cloud else f"Run `ollama pull {self.model}`."
                raise OllamaError(f"Model {self.model} isn't available. {hint}")
            if status in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = headers.get("retry-after", "")
                self.sleep(float(retry_after) if retry_after.replace(".", "", 1).isdigit() else min(2 ** attempt, 30))
                continue
            raise _StatusError(status, err)
        raise OllamaError("unreachable")

    def list_models(self) -> list[str]:
        data = self._send("GET", "/api/tags")
        return sorted(m.get("name") or m.get("model", "") for m in data.get("models", []))

    def json_call(self, *, system: str, document: str, instruction: str, schema: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
        user = (f"{document}\n\n{instruction}\n\nRespond with only a JSON object that matches this JSON Schema:\n"
                f"{json.dumps(schema, separators=(',', ':'))}")
        prompt_chars = len(system) + len(user)
        if prompt_chars // 3 + MAX_OUTPUT_TOKENS > MAX_CTX:
            raise OllamaError(f"Document is too long for a {MAX_CTX}-token context. Split it or use fewer pages.")
        options: dict[str, Any] = {"temperature": 0, "num_predict": MAX_OUTPUT_TOKENS}
        num_ctx = None
        if not self.is_cloud:
            num_ctx = self.num_ctx or context_size(prompt_chars)
            options["num_ctx"] = num_ctx
        # Reasoning models (e.g. Nemotron, gpt-oss, DeepSeek-R1) spend output tokens on hidden thinking,
        # which can exhaust the budget before the JSON is written. Extraction doesn't need it, so turn it off.
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False, "format": schema, "think": False, "options": options,
        }
        # If the endpoint or model rejects a field, drop it and retry. The reply is still parsed and
        # schema-validated afterwards, so a fallback can't let a malformed answer through.
        while True:
            try:
                data = self._send("POST", "/api/chat", body)
                break
            except _StatusError as e:
                msg = e.message.lower()
                if e.status == 400 and "think" in msg and "think" in body:
                    body.pop("think")
                elif e.status == 400 and "format" in msg and "format" in body:
                    body.pop("format")
                else:
                    raise OllamaError(f"Ollama error {e.status}: {e.message}") from None
        self.used_format = "format" in body
        self.used_think_off = "think" in body

        prompt_tokens = data.get("prompt_eval_count", 0) or 0
        if num_ctx is not None and prompt_tokens >= num_ctx - 16:
            raise OllamaError(f"Prompt was truncated to the {num_ctx}-token context window ({prompt_tokens} tokens read). "
                              "Pass a larger num_ctx or use a model with a longer context.")
        if prompt_tokens and prompt_chars / prompt_tokens > MAX_CHARS_PER_TOKEN:
            raise OllamaError(f"Only {prompt_tokens} prompt tokens were read for a {prompt_chars:,}-character prompt, so the "
                              "document was probably truncated. Use a model with a longer context window.")
        message = data.get("message") or {}
        if data.get("done_reason") == "length":
            thinking = len(message.get("thinking") or "")
            why = (f" The model spent about {thinking // 4:,} tokens thinking before answering; it may not support turning thinking off."
                   if thinking else " The output may be repeating itself; the end of it was: " + repr((message.get("content") or "")[-120:]))
            raise OllamaError(f"Output hit the {MAX_OUTPUT_TOKENS:,}-token limit before the JSON was finished.{why} "
                              "Raise FLOODRULES_MAX_OUTPUT_TOKENS or try another model.")

        usage = Usage(input_tokens=prompt_tokens, output_tokens=data.get("eval_count", 0) or 0,
                      latency_s=round((data.get("total_duration", 0) or 0) / 1e9, 3), calls=1)
        content = (message.get("content") or "").strip()
        if content.startswith("```"):   # without `format`, some models wrap JSON in a code fence
            content = content.strip("`").removeprefix("json").strip()
        try:
            return json.loads(content), usage
        except json.JSONDecodeError as e:
            raise OllamaError(f"Model returned invalid JSON: {content[:200]!r}") from e


class _StatusError(OllamaError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Ollama error {status}: {message}")
        self.status, self.message = status, message
