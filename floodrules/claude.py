"""Minimal Claude Messages API client: one tool call, usage and cost accounting.

Uses plain HTTP (requests) so the request shape is visible and easy to test.
The transport is injectable, so tests run without a network or API key.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# USD per million tokens, from platform.claude.com/docs/en/about-claude/pricing (October 2026).
# Haiku 5.5 rates apply to prompts up to 100K tokens; longer prompts cost more (see PRICING_LONG).
PRICING = {
    "claude-haiku-5-5":  {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.10},
    "claude-opus-5-5":   {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20},
}
PRICING_LONG = {"claude-haiku-5-5": {"input": 0.50, "output": 2.50, "cache_write": 0.625, "cache_read": 0.05}}
LONG_PROMPT_TOKENS = 100_000

Transport = Callable[[dict[str, Any], dict[str, str]], tuple[int, dict[str, Any], dict[str, str]]]


class ClaudeError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    latency_s: float = 0.0
    calls: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens

    def add(self, other: "Usage") -> "Usage":
        return Usage(*(getattr(self, f) + getattr(other, f) for f in
                       ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "latency_s", "calls")))

    def cost_usd(self, model: str) -> float | None:
        rates = PRICING.get(model)
        if rates is None:
            return None
        if self.calls == 1 and self.prompt_tokens > LONG_PROMPT_TOKENS and model in PRICING_LONG:
            rates = PRICING_LONG[model]
        return round((self.input_tokens * rates["input"] + self.output_tokens * rates["output"]
                      + self.cache_write_tokens * rates["cache_write"] + self.cache_read_tokens * rates["cache_read"]) / 1e6, 6)


@dataclass
class ToolCallResult:
    tool_input: dict[str, Any]
    usage: Usage
    model: str
    stop_reason: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def _requests_transport(body: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any], dict[str, str]]:
    r = requests.post(API_URL, json=body, headers=headers, timeout=180)
    try:
        data = r.json()
    except ValueError:
        data = {"error": {"message": r.text[:500]}}
    return r.status_code, data, dict(r.headers)


class Claude:
    def __init__(self, model: str = "claude-haiku-5-5", api_key: str | None = None,
                 transport: Transport | None = None, max_retries: int = 4, sleep: Callable[[float], None] = time.sleep):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.transport = transport or _requests_transport
        self.max_retries = max_retries
        self.sleep = sleep
        if transport is None and not self.api_key:
            raise ClaudeError("Set ANTHROPIC_API_KEY to call the Claude API.")

    def call_tool(self, *, system: str, document: str, instruction: str, tool_name: str,
                  tool_description: str, input_schema: dict[str, Any], max_tokens: int = 8000) -> ToolCallResult:
        """Ask Claude to answer by calling one strict tool.

        The system prompt and the document are marked for prompt caching, so repeat runs
        over the same document (eval reruns, prompt tweaks after the document) pay the
        cache-read rate for it. Newer Sonnet/Opus models reject a forced tool_choice, so
        this uses "auto" with a strict tool and an instruction to call it.
        """
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system}],
            "tools": [{"name": tool_name, "description": tool_description, "strict": True, "input_schema": input_schema}],
            "tool_choice": {"type": "auto"},
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": document, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": instruction},
                ],
            }],
        }
        headers = {"x-api-key": self.api_key, "anthropic-version": API_VERSION, "content-type": "application/json"}

        start = time.monotonic()
        data = self._post_with_retries(body, headers)
        latency = time.monotonic() - start

        u = data.get("usage", {})
        usage = Usage(input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                      cache_write_tokens=u.get("cache_creation_input_tokens", 0) or 0,
                      cache_read_tokens=u.get("cache_read_input_tokens", 0) or 0,
                      latency_s=round(latency, 3), calls=1)
        blocks = [b for b in data.get("content", []) if b.get("type") == "tool_use" and b.get("name") == tool_name]
        if not blocks:
            raise ClaudeError(f"Model answered without calling {tool_name} (stop_reason={data.get('stop_reason')}).")
        return ToolCallResult(tool_input=blocks[0]["input"], usage=usage, model=data.get("model", self.model),
                              stop_reason=data.get("stop_reason"), raw=data)

    def _post_with_retries(self, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            status, data, resp_headers = self.transport(body, headers)
            if status == 200:
                return data
            retryable = status in (408, 409, 429, 500, 502, 503, 504, 529)
            message = (data.get("error") or {}).get("message", str(data)[:300])
            if not retryable or attempt == self.max_retries:
                raise ClaudeError(f"Claude API error {status}: {message}")
            retry_after = resp_headers.get("retry-after")
            delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else min(2 ** attempt, 30)
            self.sleep(delay)
        raise ClaudeError("unreachable")
