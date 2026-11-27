"""Break one request body into the parts that make up the prompt.

Used by the inspector page. Works on OpenAI chat and Anthropic Messages bodies.
Sizes are characters of the JSON the client sent; token figures share the
request's real input-token count (when the upstream reported one) across the
parts in proportion to their size, otherwise they assume ~4 characters per
token. Either way they are estimates, and the page says so.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .capture import _text

# Open-WebUI (and other RAG front ends) wrap retrieved files in <source> tags.
# The innermost tag wins: Open-WebUI's own instructions mention "<source>" before the real ones.
_SOURCE_RE = re.compile(r"<source\b([^>]*)>((?:(?!<source\b).)*?)</source>", re.S)
_ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')
_MAX_TEXT = 400_000  # per message, so one giant paste cannot freeze the page

SEGMENTS = [
    ("system", "System prompt"),
    ("tools", "Tool definitions"),
    ("documents", "Documents"),
    ("history", "Earlier conversation"),
    ("latest", "Latest user message"),
    ("turn", "Tool calls and results this turn"),
    ("images", "Images"),
    ("other", "Parameters and framing"),
]


def _size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def _is_image(part: Any) -> bool:
    return isinstance(part, dict) and part.get("type") in ("image", "image_url", "input_image")


def _is_document(part: Any) -> bool:
    return isinstance(part, dict) and part.get("type") in ("document", "file", "input_file")


def _tool_results_only(m: dict) -> bool:
    """An Anthropic user turn that only carries tool results (part of the agent loop)."""
    c = m.get("content")
    return isinstance(c, list) and bool(c) and all(isinstance(p, dict) and p.get("type") == "tool_result" for p in c)


def _sources(text: str) -> list[dict]:
    out = []
    for m in _SOURCE_RE.finditer(text):
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        name = attrs.get("name") or attrs.get("id") or "source"
        out.append({"name": name, "chars": len(m.group(0)), "text": m.group(2)})
    return out


def _tool_calls(m: dict) -> list[dict]:
    calls = []
    for tc in m.get("tool_calls") or []:
        f = tc.get("function") or {}
        calls.append({"name": f.get("name"), "id": tc.get("id"), "arguments": f.get("arguments")})
    c = m.get("content")
    if isinstance(c, list):
        for p in c:
            if isinstance(p, dict) and p.get("type") in ("tool_use", "server_tool_use"):
                calls.append({"name": p.get("name"), "id": p.get("id"),
                              "arguments": json.dumps(p.get("input"), ensure_ascii=False)})
    return calls


def _message_text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        # Tool calls are listed separately; keep the rest readable.
        c = [p for p in c if not (isinstance(p, dict) and p.get("type") in ("tool_use", "server_tool_use"))]
    text = _text(c)
    if m.get("reasoning_content"):
        text = f"[reasoning] {m['reasoning_content']}\n{text}"
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + f"\n… [{len(text) - _MAX_TEXT:,} more characters not shown]"
    return text


def _tool_entry(t: dict) -> dict:
    f = t.get("function") if isinstance(t.get("function"), dict) else t
    name = f.get("name") or t.get("type") or "?"
    schema = f.get("parameters") or f.get("input_schema") or {}
    defs = 0
    if isinstance(schema, dict):
        defs = sum(_size(schema[k]) for k in ("$defs", "definitions") if k in schema)
    return {"name": name, "chars": _size(t), "description_chars": len(f.get("description") or ""),
            "schema_chars": _size(schema) if schema else 0, "defs_chars": defs}


def _group_tools(tools: list[dict]) -> list[dict]:
    """Group namespaced tools (an MCP server's tools share a prefix and arrive together).

    A prefix counts as a namespace when at least four tools in a row share it;
    everything else lands in "Other tools". Verb prefixes such as list_ or
    search_ are scattered across servers, so they don't form runs.
    """

    def prefix(n: str) -> str:
        return n.split("_", 1)[0] if "_" in n else ""

    group_of = ["" for _ in tools]
    i = 0
    while i < len(tools):
        p = prefix(tools[i]["name"])
        j = i
        while j < len(tools) and p and prefix(tools[j]["name"]) == p:
            j += 1
        if p and j - i >= 4:
            for k in range(i, j):
                group_of[k] = p
            i = j
        else:
            i += 1
    groups: dict[str, dict] = {}
    for t, g in zip(tools, group_of):
        key = g or "Other tools"
        d = groups.setdefault(key, {"name": key, "count": 0, "chars": 0, "tools": []})
        d["count"] += 1
        d["chars"] += t["chars"]
        d["tools"].append(t)
    out = sorted(groups.values(), key=lambda d: (d["name"] == "Other tools", -d["chars"]))
    for d in out:
        d["tools"].sort(key=lambda t: -t["chars"])
    return out


def analyze(body: dict, tokens_in: int | None = None) -> dict:
    """Composition of one request body, for the inspector page."""
    total = _size(body)
    ratio = (tokens_in / total) if tokens_in and total else 0.25
    seg = dict.fromkeys((k for k, _ in SEGMENTS), 0)
    messages_out: list[dict] = []
    documents: list[dict] = []

    # Anthropic keeps the system prompt outside the messages.
    system = body.get("system")
    if system:
        s = _size(system)
        text = _text(system)
        for d in _sources(text):
            documents.append({"name": d["name"], "chars": d["chars"], "where": "system prompt"})
            seg["documents"] += d["chars"]
            s -= d["chars"]
        seg["system"] += max(s, 0)
        messages_out.append({"index": None, "role": "system", "category": "system", "chars": _size(system),
                             "text": text[:_MAX_TEXT], "tool_calls": [], "documents": [d["name"] for d in _sources(text)]})

    msgs = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    latest = next((i for i in range(len(msgs) - 1, -1, -1)
                   if msgs[i].get("role") == "user" and not _tool_results_only(msgs[i])), None)

    for i, m in enumerate(msgs):
        role = m.get("role", "?")
        if role in ("system", "developer"):
            cat = "system"
        elif latest is None or i < latest:
            cat = "history"
        elif i == latest:
            cat = "latest"
        else:
            cat = "turn"
        size = _size(m)
        rest = size
        docs_here: list[str] = []
        content = m.get("content")
        parts = content if isinstance(content, list) else []
        for p in parts:
            if _is_image(p):
                n = _size(p)
                seg["images"] += n
                rest -= n
            elif _is_document(p):
                n = _size(p)
                name = (p.get("title") or (p.get("source") or {}).get("title") or p.get("filename")
                        or (p.get("file") or {}).get("filename") or "document")
                documents.append({"name": name, "chars": n, "where": f"message {i}"})
                docs_here.append(name)
                seg["documents"] += n
                rest -= n
        text = _message_text(m)
        for d in _sources(_text(content)):
            documents.append({"name": d["name"], "chars": d["chars"], "where": f"message {i}"})
            docs_here.append(d["name"])
            seg["documents"] += d["chars"]
            rest -= d["chars"]
        seg[cat] += max(rest, 0)
        out = {"index": i, "role": role, "category": cat, "chars": size, "text": text,
               "tool_calls": _tool_calls(m), "documents": docs_here}
        if m.get("tool_call_id"):
            out["tool_call_id"] = m["tool_call_id"]
        messages_out.append(out)

    tools = [_tool_entry(t) for t in (body.get("tools") or []) if isinstance(t, dict)]
    seg["tools"] = _size(body["tools"]) if body.get("tools") else 0
    seg["other"] = max(total - sum(v for k, v in seg.items() if k != "other"), 0)

    def tok(chars: int) -> int:
        return round(chars * ratio)

    for m in messages_out:
        m["tokens"] = tok(m["chars"])
    for d in documents:
        d["tokens"] = tok(d["chars"])
    groups = _group_tools(tools)
    for g in groups:
        g["tokens"] = tok(g["chars"])
        for t in g["tools"]:
            t["tokens"] = tok(t["chars"])
    params = {k: v for k, v in body.items() if k not in ("messages", "system", "tools", "contents")}
    return {
        "total_chars": total,
        "tokens": tokens_in or tok(total),
        "tokens_measured": bool(tokens_in),
        "segments": [{"key": k, "label": label, "chars": seg[k], "tokens": tok(seg[k])}
                     for k, label in SEGMENTS if seg[k]],
        "tool_count": len(tools),
        "tool_groups": groups,
        "documents": documents,
        "messages": messages_out,
        "params": params,
    }


def response_view(resp: Any) -> dict | None:
    """The answer, whichever client format it was returned in."""
    if not isinstance(resp, dict):
        return None
    if resp.get("choices"):
        c = resp["choices"][0] or {}
        m = c.get("message") or {}
        return {"text": _text(m.get("content")), "reasoning": m.get("reasoning_content") or "",
                "tool_calls": _tool_calls(m), "finish": c.get("finish_reason")}
    if "content" in resp:
        content = resp.get("content") or []
        thinking = "\n".join(p.get("thinking", "") for p in content
                             if isinstance(p, dict) and p.get("type") == "thinking")
        text = _text([p for p in content if isinstance(p, dict) and p.get("type") == "text"])
        return {"text": text, "reasoning": thinking, "tool_calls": _tool_calls({"content": content}),
                "finish": resp.get("stop_reason")}
    return None
