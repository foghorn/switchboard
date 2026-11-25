"""Streaming: SSE parsing/serialising and event-level translation.

Hub format is the Anthropic event stream: OpenAI chunks and Gemini chunks are
turned into Anthropic events, and Anthropic events are turned into OpenAI
chunks for OpenAI clients.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from .translate import an_stop_to_oa, new_chat_id, new_msg_id, oa_finish_to_an, safe_tool_id

AnEvent = tuple[str, dict]


# ---------------------------------------------------------------------------
# SSE wire format
# ---------------------------------------------------------------------------


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str | None, str]]:
    """Yield (event_name, data) for each SSE message."""
    event: str | None = None
    data: list[str] = []
    async for line in lines:
        line = line.rstrip("\r")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data:
        yield event, "\n".join(data)


def sse_anthropic(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def sse_openai(chunk: dict) -> bytes:
    return f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode()


SSE_OPENAI_DONE = b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Progress / error detection on provider-native items
# ---------------------------------------------------------------------------


def openai_chunk_is_progress(chunk: dict) -> bool:
    for c in chunk.get("choices") or []:
        d = c.get("delta") or {}
        if d.get("content") or d.get("reasoning_content") or d.get("reasoning") or d.get("tool_calls"):
            return True
        if c.get("finish_reason"):
            return True
    return False


def anthropic_event_is_progress(ev: AnEvent) -> bool:
    return ev[0] in ("content_block_start", "content_block_delta", "message_delta", "message_stop")


def gemini_chunk_is_progress(chunk: dict) -> bool:
    return bool(chunk.get("candidates"))


# ---------------------------------------------------------------------------
# OpenAI chunks -> Anthropic events
# ---------------------------------------------------------------------------


class OpenAIToAnthropic:
    def __init__(self, model: str):
        self.model = model
        self.started = False
        self.index = -1  # current Anthropic content block index
        self.open_kind: str | None = None  # "text" | "tool"
        self.open_tool: int | None = None  # OpenAI tool index of the open block
        self.finish: str | None = None
        self.saw_tool = False
        self.in_tokens = 0
        self.out_tokens = 0
        self.done = False

    def _start(self) -> list[AnEvent]:
        if self.started:
            return []
        self.started = True
        return [("message_start", {"type": "message_start", "message": {
            "id": new_msg_id(), "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})]

    def _close(self) -> list[AnEvent]:
        if self.open_kind is None:
            return []
        self.open_kind, self.open_tool = None, None
        return [("content_block_stop", {"type": "content_block_stop", "index": self.index})]

    def feed(self, chunk: dict) -> list[AnEvent]:
        out = self._start()
        if u := chunk.get("usage"):
            self.in_tokens = u.get("prompt_tokens", self.in_tokens) or self.in_tokens
            self.out_tokens = u.get("completion_tokens", self.out_tokens) or self.out_tokens
        for c in chunk.get("choices") or []:
            d = c.get("delta") or {}
            if text := d.get("content"):
                if self.open_kind != "text":
                    out += self._close()
                    self.index += 1
                    self.open_kind = "text"
                    out.append(("content_block_start", {"type": "content_block_start", "index": self.index,
                                                        "content_block": {"type": "text", "text": ""}}))
                out.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                    "delta": {"type": "text_delta", "text": text}}))
            for tc in d.get("tool_calls") or []:
                ti = tc.get("index", 0)
                fn = tc.get("function") or {}
                if self.open_kind != "tool" or self.open_tool != ti:
                    out += self._close()
                    self.index += 1
                    self.open_kind, self.open_tool = "tool", ti
                    self.saw_tool = True
                    out.append(("content_block_start", {"type": "content_block_start", "index": self.index,
                                                        "content_block": {"type": "tool_use", "id": safe_tool_id(tc.get("id")),
                                                                          "name": fn.get("name", ""), "input": {}}}))
                if args := fn.get("arguments"):
                    out.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                        "delta": {"type": "input_json_delta", "partial_json": args}}))
            if c.get("finish_reason"):
                self.finish = c["finish_reason"]
        return out

    def finish_events(self) -> list[AnEvent]:
        if self.done:
            return []
        self.done = True
        out = self._start() + self._close()
        stop = oa_finish_to_an(self.finish)
        if self.saw_tool:
            stop = "tool_use"
        out.append(("message_delta", {"type": "message_delta",
                                      "delta": {"stop_reason": stop, "stop_sequence": None},
                                      "usage": {"input_tokens": self.in_tokens, "output_tokens": self.out_tokens}}))
        out.append(("message_stop", {"type": "message_stop"}))
        return out


# ---------------------------------------------------------------------------
# Gemini chunks -> Anthropic events
# ---------------------------------------------------------------------------


class GeminiToAnthropic(OpenAIToAnthropic):
    """Reuses the block bookkeeping: Gemini text parts become text deltas and each
    functionCall part (always complete in one chunk) becomes a whole tool_use block."""

    def feed(self, chunk: dict) -> list[AnEvent]:  # type: ignore[override]
        out = self._start()
        if u := chunk.get("usageMetadata"):
            self.in_tokens = u.get("promptTokenCount", self.in_tokens)
            self.out_tokens = u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
        for cand in chunk.get("candidates") or []:
            for p in (cand.get("content") or {}).get("parts") or []:
                if p.get("thought"):
                    continue
                if p.get("text"):
                    if self.open_kind != "text":
                        out += self._close()
                        self.index += 1
                        self.open_kind = "text"
                        out.append(("content_block_start", {"type": "content_block_start", "index": self.index,
                                                            "content_block": {"type": "text", "text": ""}}))
                    out.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                        "delta": {"type": "text_delta", "text": p["text"]}}))
                elif "functionCall" in p:
                    fc = p["functionCall"]
                    out += self._close()
                    self.index += 1
                    self.saw_tool = True
                    out.append(("content_block_start", {"type": "content_block_start", "index": self.index,
                                                        "content_block": {"type": "tool_use", "id": safe_tool_id(fc.get("id")),
                                                                          "name": fc.get("name", ""), "input": {}}}))
                    out.append(("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                        "delta": {"type": "input_json_delta",
                                                                  "partial_json": json.dumps(fc.get("args") or {})}}))
                    out.append(("content_block_stop", {"type": "content_block_stop", "index": self.index}))
            if fr := cand.get("finishReason"):
                self.finish = {"MAX_TOKENS": "length", "SAFETY": "content_filter",
                               "RECITATION": "content_filter", "PROHIBITED_CONTENT": "content_filter"}.get(fr, "stop")
        return out


# ---------------------------------------------------------------------------
# Anthropic events -> OpenAI chunks
# ---------------------------------------------------------------------------


class AnthropicToOpenAI:
    def __init__(self, model: str, include_usage: bool):
        self.model = model
        self.include_usage = include_usage
        self.id = new_chat_id()
        self.created = int(time.time())
        self.tool_index: dict[int, int] = {}  # Anthropic block index -> OpenAI tool index
        self.in_tokens = 0
        self.out_tokens = 0
        self.stop: str | None = None
        self.done = False

    def _chunk(self, delta: dict, finish: str | None = None) -> dict:
        return {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def feed(self, ev: AnEvent) -> list[dict]:
        name, data = ev
        if name == "message_start":
            u = (data.get("message") or {}).get("usage") or {}
            self.in_tokens = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                              + u.get("cache_creation_input_tokens", 0))
            return [self._chunk({"role": "assistant", "content": ""})]
        if name == "content_block_start":
            cb = data.get("content_block") or {}
            if cb.get("type") == "tool_use":
                ti = len(self.tool_index)
                self.tool_index[data.get("index", 0)] = ti
                return [self._chunk({"tool_calls": [{"index": ti, "id": cb.get("id"), "type": "function",
                                                     "function": {"name": cb.get("name", ""), "arguments": ""}}]})]
            if cb.get("type") == "text" and cb.get("text"):
                return [self._chunk({"content": cb["text"]})]
            return []
        if name == "content_block_delta":
            d = data.get("delta") or {}
            t = d.get("type")
            if t == "text_delta":
                return [self._chunk({"content": d.get("text", "")})]
            if t == "input_json_delta" and data.get("index") in self.tool_index:
                return [self._chunk({"tool_calls": [{"index": self.tool_index[data["index"]],
                                                     "function": {"arguments": d.get("partial_json", "")}}]})]
            if t == "thinking_delta" and d.get("thinking"):
                return [self._chunk({"reasoning_content": d["thinking"]})]
            return []
        if name == "message_delta":
            self.stop = (data.get("delta") or {}).get("stop_reason") or self.stop
            u = data.get("usage") or {}
            self.out_tokens = u.get("output_tokens", self.out_tokens)
            if u.get("input_tokens"):
                self.in_tokens = u["input_tokens"]
            return []
        if name == "message_stop":
            return self.finish_chunks()
        return []

    def finish_chunks(self) -> list[dict]:
        if self.done:
            return []
        self.done = True
        out = [self._chunk({}, an_stop_to_oa(self.stop))]
        if self.include_usage:
            out.append({"id": self.id, "object": "chat.completion.chunk", "created": self.created,
                        "model": self.model, "choices": [],
                        "usage": {"prompt_tokens": self.in_tokens, "completion_tokens": self.out_tokens,
                                  "total_tokens": self.in_tokens + self.out_tokens}})
        return out


def error_payload(fmt: str, message: str, kind: str = "api_error") -> dict[str, Any]:
    if fmt == "anthropic":
        return {"type": "error", "error": {"type": kind, "message": message}}
    return {"error": {"message": message, "type": kind, "code": None}}
