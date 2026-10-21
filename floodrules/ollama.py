"""Local models through Ollama's /api/chat with a JSON-schema `format` (structured outputs).

Two things differ from the Claude path:
- No tool calling: the schema goes in `format`, which constrains decoding to valid JSON.
- Ollama silently truncates prompts longer than the context window (num_ctx), and its
  default window is small. This client sizes num_ctx to the document and raises if the
  prompt still came back truncated, instead of scoring a model that never saw page 3.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

import requests

from .claude import ModelError, Usage

DEFAULT_HOST = "http://localhost:11434"
MAX_CTX = 131_072
MAX_OUTPUT_TOKENS = 8192

Transport = Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]]


class OllamaError(ModelError):
    pass


def _requests_transport(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        r = requests.post(url, json=body, timeout=1800)
    except requests.ConnectionError as e:
        raise OllamaError(f"Can't reach Ollama at {url.rsplit('/api', 1)[0]}. Start it with `ollama serve`.") from e
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": r.text[:500]}


def estimate_tokens(text: str) -> int:
    return len(text) // 3 + 1   # conservative for English prose; real tokenizers average ~4 chars


def context_size(prompt_chars: int) -> int:
    need = prompt_chars // 3 + MAX_OUTPUT_TOKENS + 1024
    size = 8192
    while size < need and size < MAX_CTX:
        size *= 2
    return min(size, MAX_CTX)


class Ollama:
    def __init__(self, model: str, host: str | None = None, transport: Transport | None = None, num_ctx: int | None = None):
        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.transport = transport or _requests_transport
        self.num_ctx = num_ctx

    def json_call(self, *, system: str, document: str, instruction: str, schema: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
        user = (f"{document}\n\n{instruction}\n\nRespond with only a JSON object that matches this JSON Schema:\n"
                f"{json.dumps(schema, separators=(',', ':'))}")
        prompt_chars = len(system) + len(user)
        num_ctx = self.num_ctx or context_size(prompt_chars)
        if prompt_chars // 3 + MAX_OUTPUT_TOKENS > MAX_CTX:
            raise OllamaError(f"Document is too long for a {MAX_CTX}-token context. Split it or use fewer pages.")
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": MAX_OUTPUT_TOKENS},
        }
        status, data = self.transport(f"{self.host}/api/chat", body)
        if status == 404 or "not found" in str(data.get("error", "")).lower():
            raise OllamaError(f"Model {self.model} isn't installed. Run `ollama pull {self.model}`.")
        if status != 200:
            raise OllamaError(f"Ollama error {status}: {data.get('error', data)}")

        prompt_tokens = data.get("prompt_eval_count", 0) or 0
        if prompt_tokens >= num_ctx - 16:
            raise OllamaError(f"Prompt was truncated to the {num_ctx}-token context window ({prompt_tokens} tokens read). "
                              "Pass a larger num_ctx or a model with a longer context.")
        if data.get("done_reason") == "length":
            raise OllamaError(f"Output hit the {MAX_OUTPUT_TOKENS}-token limit before the JSON was finished.")

        usage = Usage(input_tokens=prompt_tokens, output_tokens=data.get("eval_count", 0) or 0,
                      latency_s=round((data.get("total_duration", 0) or 0) / 1e9, 3), calls=1)
        content = (data.get("message") or {}).get("content", "")
        try:
            return json.loads(content), usage
        except json.JSONDecodeError as e:
            raise OllamaError(f"Model returned invalid JSON: {content[:200]!r}") from e
