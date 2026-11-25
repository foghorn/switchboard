"""Request and (non-streaming) response translation.

The hub format is the Anthropic Messages shape: OpenAI and Gemini bodies are
converted to and from it. When the client and the block speak the same format
nothing goes through here; the body is passed through with only the model
name swapped.

Things that cannot cross providers are dropped on purpose:
- reasoning/thinking content in history (Anthropic thinking blocks are signed
  and bound to the model that wrote them; other providers' reasoning has no
  Anthropic equivalent);
- parameters the target has no counterpart for.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

_ID_BAD = re.compile(r"[^A-Za-z0-9_-]")


def safe_tool_id(raw: str | None) -> str:
    """Tool-call ids every provider accepts. Deterministic, so a tool_use id and
    the matching tool_result id stay equal after translation."""
    if not raw:
        return "toolu_" + uuid.uuid4().hex[:24]
    return _ID_BAD.sub("_", raw)[:64]


def new_msg_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def new_chat_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex[:24]


def _json_loads_obj(s: Any) -> dict:
    if isinstance(s, dict):
        return s
    if not s:
        return {}
    try:
        v = json.loads(s)
    except (TypeError, ValueError):
        return {"_raw": s}
    return v if isinstance(v, dict) else {"_value": v}


def _text_of(content: Any) -> str:
    """Flatten OpenAI or Anthropic content (string or parts) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for part in content:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
            out.append(part.get("text", ""))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# OpenAI chat request  ->  Anthropic messages request
# ---------------------------------------------------------------------------

_DATA_URL = re.compile(r"^data:([^;,]+);base64,(.*)$", re.S)


def _oa_part_to_an(part: dict) -> dict | None:
    t = part.get("type")
    if t in ("text", "input_text"):
        return {"type": "text", "text": part.get("text", "")}
    if t == "image_url":
        url = (part.get("image_url") or {}).get("url", "")
        m = _DATA_URL.match(url)
        if m:
            return {"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _push(messages: list[dict], role: str, blocks: list[dict]) -> None:
    """Append, merging consecutive same-role turns (Anthropic wants alternation)."""
    if not blocks:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": list(blocks)})


def openai_to_anthropic_request(body: dict) -> dict:
    system_parts: list[str] = []
    messages: list[dict] = []
    for m in body.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if role in ("system", "developer"):
            system_parts.append(_text_of(content))
        elif role == "user":
            if isinstance(content, str):
                blocks = [{"type": "text", "text": content}] if content else []
            else:
                blocks = [b for b in (_oa_part_to_an(p) for p in content or []) if b]
            _push(messages, "user", blocks)
        elif role == "assistant":
            blocks = []
            text = _text_of(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                blocks.append({
                    "type": "tool_use",
                    "id": safe_tool_id(tc.get("id")),
                    "name": fn.get("name", ""),
                    "input": _json_loads_obj(fn.get("arguments")),
                })
            _push(messages, "assistant", blocks)
        elif role in ("tool", "function"):
            _push(messages, "user", [{
                "type": "tool_result",
                "tool_use_id": safe_tool_id(m.get("tool_call_id")),
                "content": _text_of(content),
            }])

    out: dict[str, Any] = {"messages": messages}
    if system_parts:
        out["system"] = "\n\n".join(p for p in system_parts if p)
    mt = body.get("max_completion_tokens") or body.get("max_tokens")
    if mt:
        out["max_tokens"] = mt
    for k in ("temperature", "top_p"):
        if body.get(k) is not None:
            out[k] = body[k]
    stop = body.get("stop")
    if stop:
        out["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    if body.get("stream"):
        out["stream"] = True

    tools = []
    for t in body.get("tools") or []:
        if t.get("type", "function") != "function":
            continue
        fn = t.get("function") or {}
        tools.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice")
        choice: dict | None = None
        if tc == "auto":
            choice = {"type": "auto"}
        elif tc == "required":
            choice = {"type": "any"}
        elif tc == "none":
            choice = {"type": "none"}
        elif isinstance(tc, dict) and tc.get("type") == "function":
            choice = {"type": "tool", "name": (tc.get("function") or {}).get("name", "")}
        if body.get("parallel_tool_calls") is False:
            choice = choice or {"type": "auto"}
            if choice["type"] != "none":
                choice["disable_parallel_tool_use"] = True
        if choice:
            out["tool_choice"] = choice

    output_config: dict[str, Any] = {}
    rf = body.get("response_format") or {}
    if rf.get("type") == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema")
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
    effort = body.get("reasoning_effort")
    if effort:
        output_config["effort"] = {"minimal": "low", "none": "low"}.get(effort, effort)
    if output_config:
        out["output_config"] = output_config
    return out


# ---------------------------------------------------------------------------
# Anthropic messages request  ->  OpenAI chat request
# ---------------------------------------------------------------------------


def _an_image_to_oa(block: dict) -> dict | None:
    src = block.get("source") or {}
    if src.get("type") == "base64":
        return {"type": "image_url", "image_url": {"url": f"data:{src.get('media_type')};base64,{src.get('data')}"}}
    if src.get("type") == "url":
        return {"type": "image_url", "image_url": {"url": src.get("url")}}
    return None


def _an_tool_result_text(block: dict) -> str:
    c = block.get("content")
    text = _text_of(c) if not isinstance(c, str) else c
    if block.get("is_error"):
        text = f"[tool error] {text}"
    return text


def anthropic_to_openai_request(body: dict) -> dict:
    messages: list[dict] = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": _text_of(system)})

    for m in body.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        content = content or []
        if role == "system":  # mid-conversation system message
            messages.append({"role": "system", "content": _text_of(content)})
        elif role == "user":
            parts: list[dict] = []
            for b in content:
                t = b.get("type")
                if t == "tool_result":
                    # tool messages must directly follow the assistant tool_calls
                    messages.append({
                        "role": "tool",
                        "tool_call_id": safe_tool_id(b.get("tool_use_id")),
                        "content": _an_tool_result_text(b),
                    })
                elif t == "text":
                    parts.append({"type": "text", "text": b.get("text", "")})
                elif t == "image":
                    if p := _an_image_to_oa(b):
                        parts.append(p)
                elif t == "document":
                    src = b.get("source") or {}
                    if src.get("type") == "text":
                        parts.append({"type": "text", "text": src.get("data", "")})
            if parts:
                if all(p["type"] == "text" for p in parts):
                    messages.append({"role": "user", "content": "\n".join(p["text"] for p in parts)})
                else:
                    messages.append({"role": "user", "content": parts})
        elif role == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [
                {
                    "id": safe_tool_id(b.get("id")),
                    "type": "function",
                    "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input") or {})},
                }
                for b in content
                if b.get("type") == "tool_use"
            ]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            if text or calls:
                messages.append(msg)

    out: dict[str, Any] = {"messages": messages}
    if body.get("max_tokens"):
        out["max_tokens"] = body["max_tokens"]
    for k in ("temperature", "top_p"):
        if body.get(k) is not None:
            out[k] = body[k]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("stream"):
        out["stream"] = True

    tools = []
    for t in body.get("tools") or []:
        if "input_schema" not in t:  # server tools (web_search, ...) have no OpenAI equivalent
            continue
        tools.append({
            "type": "function",
            "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]},
        })
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice") or {}
        mapping = {"auto": "auto", "any": "required", "none": "none"}
        if tc.get("type") in mapping:
            out["tool_choice"] = mapping[tc["type"]]
        elif tc.get("type") == "tool":
            out["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
        if tc.get("disable_parallel_tool_use"):
            out["parallel_tool_calls"] = False

    fmt = (body.get("output_config") or {}).get("format") or {}
    if fmt.get("type") == "json_schema" and fmt.get("schema"):
        out["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": fmt["schema"], "strict": True},
        }
    return out


# ---------------------------------------------------------------------------
# Anthropic messages request  ->  Gemini generateContent request
# ---------------------------------------------------------------------------

# Gemini 3 wants a thought signature on replayed function calls; this documented
# placeholder tells it the call came from elsewhere (another model / history edit).
GEMINI_SKIP_SIGNATURE = "skip_thought_signature_validator"


def anthropic_to_gemini_request(body: dict) -> dict:
    tool_names: dict[str, str] = {}
    contents: list[dict] = []

    def push(role: str, parts: list[dict]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for m in body.get("messages", []):
        content = m.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        content = content or []
        role = m.get("role")
        if role == "system":
            push("user", [{"text": _text_of(content)}])
            continue
        parts: list[dict] = []
        for b in content:
            t = b.get("type")
            if t == "text" and b.get("text"):
                parts.append({"text": b["text"]})
            elif t == "image":
                src = b.get("source") or {}
                if src.get("type") == "base64":
                    parts.append({"inlineData": {"mimeType": src.get("media_type"), "data": src.get("data")}})
                elif src.get("type") == "url":
                    parts.append({"fileData": {"fileUri": src.get("url")}})
            elif t == "tool_use":
                tool_names[b.get("id", "")] = b.get("name", "")
                parts.append({
                    "functionCall": {"name": b.get("name", ""), "args": b.get("input") or {}},
                    "thoughtSignature": GEMINI_SKIP_SIGNATURE,
                })
            elif t == "tool_result":
                name = tool_names.get(b.get("tool_use_id", ""), "tool")
                parts.append({"functionResponse": {"name": name, "response": {"content": _an_tool_result_text(b)}}})
        push("model" if role == "assistant" else "user", parts)

    out: dict[str, Any] = {"contents": contents}
    if body.get("system"):
        out["systemInstruction"] = {"parts": [{"text": _text_of(body["system"])}]}
    gen: dict[str, Any] = {}
    if body.get("max_tokens"):
        gen["maxOutputTokens"] = body["max_tokens"]
    for src, dst in (("temperature", "temperature"), ("top_p", "topP"), ("top_k", "topK")):
        if body.get(src) is not None:
            gen[dst] = body[src]
    if body.get("stop_sequences"):
        gen["stopSequences"] = body["stop_sequences"]
    fmt = (body.get("output_config") or {}).get("format") or {}
    if fmt.get("type") == "json_schema" and fmt.get("schema"):
        gen["responseMimeType"] = "application/json"
        gen["responseJsonSchema"] = fmt["schema"]
    if gen:
        out["generationConfig"] = gen

    decls = [
        {"name": t["name"], "description": t.get("description", ""), "parametersJsonSchema": t["input_schema"]}
        for t in body.get("tools") or []
        if "input_schema" in t
    ]
    if decls:
        out["tools"] = [{"functionDeclarations": decls}]
        tc = body.get("tool_choice") or {}
        mode = {"auto": "AUTO", "any": "ANY", "none": "NONE", "tool": "ANY"}.get(tc.get("type"))
        if mode:
            cfg: dict[str, Any] = {"mode": mode}
            if tc.get("type") == "tool":
                cfg["allowedFunctionNames"] = [tc.get("name", "")]
            out["toolConfig"] = {"functionCallingConfig": cfg}
    return out


# ---------------------------------------------------------------------------
# Responses (non-streaming)
# ---------------------------------------------------------------------------

_OA_FINISH_TO_AN = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                    "function_call": "tool_use", "content_filter": "refusal"}
_AN_STOP_TO_OA = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
                  "tool_use": "tool_calls", "refusal": "content_filter", "pause_turn": "stop",
                  "model_context_window_exceeded": "length"}


def openai_to_anthropic_response(resp: dict, model: str) -> dict:
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    text = _text_of(msg.get("content"))
    if text:
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        content.append({"type": "tool_use", "id": safe_tool_id(tc.get("id")), "name": fn.get("name", ""),
                        "input": _json_loads_obj(fn.get("arguments"))})
    finish = choice.get("finish_reason") or "stop"
    stop_reason = _OA_FINISH_TO_AN.get(finish, "end_turn")
    if stop_reason != "tool_use" and any(b["type"] == "tool_use" for b in content):
        stop_reason = "tool_use"
    usage = resp.get("usage") or {}
    return {
        "id": new_msg_id(), "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)},
    }


def gemini_to_anthropic_response(resp: dict, model: str) -> dict:
    cand = (resp.get("candidates") or [{}])[0]
    content: list[dict] = []
    for p in (cand.get("content") or {}).get("parts") or []:
        if p.get("thought"):
            continue
        if "text" in p and p["text"]:
            if content and content[-1]["type"] == "text":
                content[-1]["text"] += p["text"]
            else:
                content.append({"type": "text", "text": p["text"]})
        elif "functionCall" in p:
            fc = p["functionCall"]
            content.append({"type": "tool_use", "id": safe_tool_id(fc.get("id")), "name": fc.get("name", ""),
                            "input": fc.get("args") or {}})
    fr = cand.get("finishReason", "STOP")
    stop_reason = "tool_use" if any(b["type"] == "tool_use" for b in content) else {
        "MAX_TOKENS": "max_tokens", "SAFETY": "refusal", "RECITATION": "refusal",
        "PROHIBITED_CONTENT": "refusal", "BLOCKLIST": "refusal", "SPII": "refusal",
    }.get(fr, "end_turn")
    u = resp.get("usageMetadata") or {}
    return {
        "id": new_msg_id(), "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": u.get("promptTokenCount", 0),
                  "output_tokens": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)},
    }


def anthropic_to_openai_response(resp: dict, model: str) -> dict:
    text, reasoning, calls = [], [], []
    for b in resp.get("content") or []:
        t = b.get("type")
        if t == "text":
            text.append(b.get("text", ""))
        elif t == "thinking" and b.get("thinking"):
            reasoning.append(b["thinking"])
        elif t == "tool_use":
            calls.append({"id": b.get("id"), "type": "function",
                          "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input") or {})}})
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(text) if text or not calls else None}
    if reasoning:
        msg["reasoning_content"] = "\n".join(reasoning)
    if calls:
        msg["tool_calls"] = calls
    u = resp.get("usage") or {}
    prompt = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    out_t = u.get("output_tokens", 0)
    return {
        "id": new_chat_id(), "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": _AN_STOP_TO_OA.get(resp.get("stop_reason") or "end_turn", "stop")}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": out_t, "total_tokens": prompt + out_t},
    }


def an_stop_to_oa(stop_reason: str | None) -> str:
    return _AN_STOP_TO_OA.get(stop_reason or "end_turn", "stop")


def oa_finish_to_an(finish: str | None) -> str:
    return _OA_FINISH_TO_AN.get(finish or "stop", "end_turn")
