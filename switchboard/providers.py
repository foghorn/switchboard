"""Upstream adapters: URLs, auth headers, streaming item parsing, error rules."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import Block
from .streams import (
    anthropic_event_is_progress,
    gemini_chunk_is_progress,
    openai_chunk_is_progress,
    parse_sse,
)

ANTHROPIC_VERSION = "2023-06-01"

_CONTEXT_RE = re.compile(
    r"context|too long|too many tokens|maximum.{0,40}tokens|exceed|n_ctx|token limit|prompt is too large",
    re.I,
)


class UpstreamError(Exception):
    """One block failed. failover=False means the request itself is bad and the
    next block would reject it too, so the client gets this error directly."""

    def __init__(self, message: str, status: int | None = None, failover: bool = True,
                 counts_against_block: bool = True):
        super().__init__(message)
        self.message = message
        self.status = status
        self.failover = failover
        self.counts_against_block = counts_against_block


def classify_http_error(block: Block, status: int, text: str) -> UpstreamError:
    snippet = _error_message(text)
    msg = f"HTTP {status}: {snippet}"
    if status in (400, 422):
        is_context = bool(_CONTEXT_RE.search(snippet))
        if block.failover_on_400 == "always" or (block.failover_on_400 == "context" and is_context):
            # A too-long prompt is the block's limit, not a broken block.
            return UpstreamError(msg, status, failover=True, counts_against_block=not is_context)
        return UpstreamError(msg, status, failover=False, counts_against_block=False)
    if status in (401, 403, 404, 408, 409, 413, 429) or status >= 500:
        return UpstreamError(msg, status, failover=True)
    return UpstreamError(msg, status, failover=False, counts_against_block=False)


def _error_message(text: str) -> str:
    try:
        data = json.loads(text)
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            return str(err.get("message") or err)[:500]
        return str(err)[:500]
    except (ValueError, AttributeError):
        return text.strip()[:500] or "(empty body)"


def _ensure_v(base: str, version: str) -> str:
    return base if base.endswith(f"/{version}") else f"{base}/{version}"


class Provider:
    fmt: str

    def url(self, block: Block, stream: bool) -> str:
        raise NotImplementedError

    def headers(self, block: Block, inbound: dict[str, str]) -> dict[str, str]:
        raise NotImplementedError

    def probe_url(self, block: Block) -> str:
        raise NotImplementedError

    async def items(self, resp: httpx.Response) -> AsyncIterator[Any]:
        raise NotImplementedError
        yield  # pragma: no cover

    def is_progress(self, item: Any) -> bool:
        raise NotImplementedError

    def item_error(self, item: Any) -> str | None:
        raise NotImplementedError

    def response_error(self, data: Any) -> str | None:
        """A 200 response that is actually a failure."""
        raise NotImplementedError


class OpenAIProvider(Provider):
    fmt = "openai"

    def url(self, block: Block, stream: bool) -> str:
        return f"{block.base_url}/chat/completions"

    def headers(self, block: Block, inbound: dict[str, str]) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if block.api_key:
            h["authorization"] = f"Bearer {block.api_key}"
        return h | block.headers

    def probe_url(self, block: Block) -> str:
        return f"{block.base_url}/models"

    async def items(self, resp: httpx.Response) -> AsyncIterator[Any]:
        async for _event, data in parse_sse(resp.aiter_lines()):
            if data.strip() == "[DONE]":
                return
            try:
                yield json.loads(data)
            except ValueError:
                continue

    def is_progress(self, item: Any) -> bool:
        return openai_chunk_is_progress(item)

    def item_error(self, item: Any) -> str | None:
        if isinstance(item, dict) and item.get("error"):
            return _error_message(json.dumps(item))
        return None

    def response_error(self, data: Any) -> str | None:
        if not isinstance(data, dict):
            return "response is not a JSON object"
        if data.get("error") and not data.get("choices"):
            return _error_message(json.dumps(data))
        if not data.get("choices"):
            return "response has no choices"
        return None


class AnthropicProvider(Provider):
    fmt = "anthropic"

    def url(self, block: Block, stream: bool) -> str:
        return f"{_ensure_v(block.base_url, 'v1')}/messages"

    def headers(self, block: Block, inbound: dict[str, str]) -> dict[str, str]:
        h = {
            "content-type": "application/json",
            "x-api-key": block.api_key,
            "anthropic-version": inbound.get("anthropic-version") or ANTHROPIC_VERSION,
        }
        if inbound.get("anthropic-beta"):
            h["anthropic-beta"] = inbound["anthropic-beta"]
        return h | block.headers

    def probe_url(self, block: Block) -> str:
        return f"{_ensure_v(block.base_url, 'v1')}/models"

    async def items(self, resp: httpx.Response) -> AsyncIterator[Any]:
        async for event, data in parse_sse(resp.aiter_lines()):
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            name = event or obj.get("type", "")
            if name == "ping":
                continue
            yield (name, obj)

    def is_progress(self, item: Any) -> bool:
        return anthropic_event_is_progress(item)

    def item_error(self, item: Any) -> str | None:
        name, data = item
        if name == "error":
            err = data.get("error") or {}
            return f"{err.get('type', 'error')}: {err.get('message', '')}"
        return None

    def response_error(self, data: Any) -> str | None:
        if not isinstance(data, dict) or data.get("type") != "message":
            return _error_message(json.dumps(data)) if isinstance(data, dict) else "bad response"
        return None


class GeminiProvider(Provider):
    fmt = "gemini"

    def url(self, block: Block, stream: bool) -> str:
        base = _ensure_v(block.base_url, "v1beta")
        if stream:
            return f"{base}/models/{block.model}:streamGenerateContent?alt=sse"
        return f"{base}/models/{block.model}:generateContent"

    def headers(self, block: Block, inbound: dict[str, str]) -> dict[str, str]:
        return {"content-type": "application/json", "x-goog-api-key": block.api_key} | block.headers

    def probe_url(self, block: Block) -> str:
        return f"{_ensure_v(block.base_url, 'v1beta')}/models/{block.model}"

    async def items(self, resp: httpx.Response) -> AsyncIterator[Any]:
        async for _event, data in parse_sse(resp.aiter_lines()):
            try:
                yield json.loads(data)
            except ValueError:
                continue

    def is_progress(self, item: Any) -> bool:
        return gemini_chunk_is_progress(item)

    def item_error(self, item: Any) -> str | None:
        if isinstance(item, dict) and item.get("error"):
            return _error_message(json.dumps(item))
        return None

    def response_error(self, data: Any) -> str | None:
        if not isinstance(data, dict):
            return "bad response"
        if data.get("error"):
            return _error_message(json.dumps(data))
        if not data.get("candidates"):
            reason = (data.get("promptFeedback") or {}).get("blockReason")
            return f"no candidates{f' (blocked: {reason})' if reason else ''}"
        return None


PROVIDERS: dict[str, Provider] = {
    "openai": OpenAIProvider(),
    "anthropic": AnthropicProvider(),
    "gemini": GeminiProvider(),
}


class Usage:
    """Token counts pulled from whatever the provider reports."""

    def __init__(self, fmt: str):
        self.fmt = fmt
        self.input = 0
        self.output = 0

    def observe_item(self, item: Any) -> None:
        if self.fmt == "openai":
            if u := item.get("usage"):
                self.input = u.get("prompt_tokens") or self.input
                self.output = u.get("completion_tokens") or self.output
        elif self.fmt == "anthropic":
            name, data = item
            if name == "message_start":
                u = (data.get("message") or {}).get("usage") or {}
                self.input = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                              + u.get("cache_creation_input_tokens", 0))
                self.output = u.get("output_tokens", 0)
            elif name == "message_delta":
                u = data.get("usage") or {}
                self.output = u.get("output_tokens", self.output)
        elif self.fmt == "gemini":
            if u := item.get("usageMetadata"):
                self.input = u.get("promptTokenCount", self.input)
                self.output = u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)

    def observe_response(self, data: dict) -> None:
        if self.fmt == "openai":
            u = data.get("usage") or {}
            self.input, self.output = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        elif self.fmt == "anthropic":
            u = data.get("usage") or {}
            self.input = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                          + u.get("cache_creation_input_tokens", 0))
            self.output = u.get("output_tokens", 0)
        elif self.fmt == "gemini":
            self.observe_item(data)
