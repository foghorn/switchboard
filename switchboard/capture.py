"""Opt-in prompt capture: the full request, what each upstream was sent and what
came back, one gzip JSON file per request under state_dir/captures/<date>/.

Agent loops resend the whole conversation every turn, so a stored body only keeps
the messages a previous capture didn't already have. It points at that capture
with "$base": {"ref", "slot", "key", "messages": N, "same": [fields]}, meaning
"the first N items of <key> and these fields are the same as in that body".
`resolve()` puts the full bodies back together.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("switchboard")

LIST_KEYS = ("messages", "contents")  # the turn-by-turn list: OpenAI/Anthropic, Gemini
BIG_FIELD = 512  # top-level fields at least this long (JSON chars) are deduplicated too
CANDIDATES = 32  # recent bodies remembered per (client, slot)
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(s: str) -> str:
    return hashlib.blake2b(s.encode(), digest_size=12).hexdigest()


@dataclass
class _Digest:
    ref: str
    slot: str
    key: str | None
    msgs: list[str]
    fields: dict[str, str]


def _digest(ref: str, slot: str, body: dict) -> _Digest:
    key = next((k for k in LIST_KEYS if isinstance(body.get(k), list)), None)
    msgs = [_hash(_dumps(m)) for m in body[key]] if key else []
    fields = {}
    for k, v in body.items():
        if k != key:
            s = _dumps(v)
            if len(s) >= BIG_FIELD:
                fields[k] = _hash(s)
    return _Digest(ref, slot, key, msgs, fields)


def _encode(body: dict, d: _Digest, candidates: list[_Digest]) -> dict:
    """Store `body` relative to the candidate that shares the most with it."""
    best, best_score = None, (0, 0)
    for c in candidates:
        if c.key != d.key:
            continue
        k = 0
        for a, b in zip(c.msgs, d.msgs):
            if a != b:
                break
            k += 1
        same = [f for f, h in d.fields.items() if c.fields.get(f) == h]
        if (k, len(same)) > best_score:
            best, best_score = (c, k, same), (k, len(same))
    if best is None:
        return body
    c, k, same = best
    out = {f: v for f, v in body.items() if f not in same}
    if d.key:
        out[d.key] = body[d.key][k:]
    out["$base"] = {"ref": c.ref, "slot": c.slot, "key": d.key, "messages": k, "same": same}
    return out


# ---------------------------------------------------------------------------
# Stream reassembly: provider-native stream items -> one whole response
# ---------------------------------------------------------------------------


class Assembler:
    def __init__(self, fmt: str):
        self.fmt = fmt
        self.out: dict = {}
        self.choices: dict[int, dict] = {}
        self.blocks: dict[int, dict] = {}
        self.partial: dict[int, str] = {}
        self.parts: list[dict] = []
        self.finish: str | None = None

    def feed(self, item: Any) -> None:
        try:
            getattr(self, f"_{self.fmt}")(item)
        except Exception:  # noqa: BLE001 - capture must never break a stream
            log.debug("capture: could not assemble stream item", exc_info=True)

    def _openai(self, chunk: dict) -> None:
        for k in ("id", "created", "model", "system_fingerprint"):
            if k in chunk:
                self.out[k] = chunk[k]
        if chunk.get("usage"):
            self.out["usage"] = chunk["usage"]
        for ch in chunk.get("choices") or []:
            c = self.choices.setdefault(ch.get("index", 0), {
                "index": ch.get("index", 0), "message": {"role": "assistant", "content": ""},
                "finish_reason": None, "_tools": {}})
            m, d = c["message"], ch.get("delta") or {}
            for k in ("content", "reasoning_content", "reasoning", "refusal"):
                if isinstance(d.get(k), str):
                    m[k] = m.get(k, "") + d[k]
            for tc in d.get("tool_calls") or []:
                t = c["_tools"].setdefault(tc.get("index", 0), {
                    "id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    t["id"] = tc["id"]
                f = tc.get("function") or {}
                t["function"]["name"] += f.get("name") or ""
                t["function"]["arguments"] += f.get("arguments") or ""
            if ch.get("finish_reason"):
                c["finish_reason"] = ch["finish_reason"]

    def _anthropic(self, item: tuple[str, dict]) -> None:
        name, data = item
        t = data.get("type", name)
        if t == "message_start":
            self.out = copy.deepcopy(data.get("message") or {})
        elif t == "content_block_start":
            self.blocks[data["index"]] = copy.deepcopy(data.get("content_block") or {})
        elif t == "content_block_delta":
            b, d = self.blocks.setdefault(data["index"], {}), data.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
            elif dt == "input_json_delta":
                self.partial[data["index"]] = self.partial.get(data["index"], "") + d.get("partial_json", "")
            elif dt == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
            elif dt == "signature_delta":
                b["signature"] = d.get("signature")
            elif dt == "citations_delta":
                b.setdefault("citations", []).append(d.get("citation"))
        elif t == "message_delta":
            for k, v in (data.get("delta") or {}).items():
                self.out[k] = v
            if data.get("usage"):
                self.out.setdefault("usage", {}).update(data["usage"])

    def _gemini(self, chunk: dict) -> None:
        for k in ("usageMetadata", "modelVersion", "responseId"):
            if k in chunk:
                self.out[k] = chunk[k]
        cand = (chunk.get("candidates") or [{}])[0]
        for part in (cand.get("content") or {}).get("parts") or []:
            last = self.parts[-1] if self.parts else None
            if ("text" in part and last is not None and set(last) <= {"text", "thought"}
                    and set(part) <= {"text", "thought"} and bool(last.get("thought")) == bool(part.get("thought"))):
                last["text"] += part["text"]
            else:
                self.parts.append(copy.deepcopy(part))
        if cand.get("finishReason"):
            self.finish = cand["finishReason"]

    def result(self) -> dict:
        if self.fmt == "openai":
            choices = []
            for i in sorted(self.choices):
                c = dict(self.choices[i])
                tools = c.pop("_tools")
                if tools:
                    c["message"]["tool_calls"] = [tools[j] for j in sorted(tools)]
                choices.append(c)
            return {"object": "chat.completion", **self.out, "choices": choices}
        if self.fmt == "anthropic":
            content = []
            for i in sorted(self.blocks):
                b = self.blocks[i]
                if i in self.partial:
                    try:
                        b["input"] = json.loads(self.partial[i]) if self.partial[i] else {}
                    except ValueError:
                        b["input"] = {"_unparsed": self.partial[i]}
                content.append(b)
            return {"type": "message", "role": "assistant", **self.out, "content": content}
        cand = {"content": {"role": "model", "parts": self.parts}, "index": 0}
        if self.finish:
            cand["finishReason"] = self.finish
        return {"candidates": [cand], **self.out}


# ---------------------------------------------------------------------------
# One request being captured
# ---------------------------------------------------------------------------


@dataclass
class Record:
    store: CaptureStore
    ref: str
    client: str
    api: str
    request: dict
    digests: list[_Digest] = field(default_factory=list)
    bodies: dict[str, dict] = field(default_factory=dict)  # block id -> stored upstream body
    upstream_responses: dict[str, Any] = field(default_factory=dict)
    response: Any = None

    def upstream(self, block_id: str, body: dict) -> None:
        self.bodies[block_id] = self.store._encode(self, f"upstream:{block_id}", body)

    def responded(self, block_id: str, upstream: Any, client_view: Any, same_format: bool) -> None:
        self.response = client_view
        if not same_format:
            self.upstream_responses[block_id] = upstream


class CaptureStore:
    def __init__(self, root: Path, settings: Callable[[], Any]):
        self.root = root
        self.settings = settings
        self.index: OrderedDict[tuple[str, str], deque[_Digest]] = OrderedDict()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
        self.bytes = 0
        self.next_prune = 0.0
        self.full_warned = 0.0

    # -- recording (event loop) --------------------------------------------

    def start(self, rid: str, client: str, api: str, body: dict) -> Record:
        rec = Record(self, f"{datetime.now():%Y-%m-%d}/{rid}", client, api, {})
        rec.request = self._encode(rec, "request", body)
        return rec

    def _encode(self, rec: Record, slot: str, body: dict) -> dict:
        try:
            d = _digest(rec.ref, slot, body)
            with self.lock:
                cands = list(self.index.get((rec.client, slot), ()))
            # An upstream body is usually close to this request's own client body.
            cands += [x for x in rec.digests if x.slot == "request"]
            rec.digests.append(d)
            return _encode(body, d, cands)
        except Exception:  # noqa: BLE001
            log.warning("capture: could not deduplicate %s; storing it whole", slot, exc_info=True)
            return copy.deepcopy(body)

    def finish(self, rec: Record, entry: dict) -> None:
        bodies = rec.bodies
        attempts = []
        for a in entry.get("attempts", []):
            a = dict(a)
            if a.get("block") in bodies:
                a["body"] = bodies[a["block"]]
            if a.get("block") in rec.upstream_responses:
                a["response"] = rec.upstream_responses[a["block"]]
            attempts.append(a)
        doc = {k: v for k, v in entry.items() if k not in ("attempts", "capture")}
        doc |= {"ref": rec.ref, "request": rec.request, "attempts": attempts, "response": rec.response}
        self.pool.submit(self._write, rec, doc)

    # -- writer thread -----------------------------------------------------

    def _write(self, rec: Record, doc: dict) -> None:
        try:
            s = self.settings()
            now = time.time()
            if now >= self.next_prune:
                self.prune(s.retention_days, s.max_mb)
                self.next_prune = now + 3600
            if self.bytes > s.max_mb * 1024 * 1024:
                if now - self.full_warned > 3600:
                    log.warning("capture: %s is over max_mb (%g MB); not capturing until it shrinks",
                                self.root, s.max_mb)
                    self.full_warned = now
                return
            day, rid = rec.ref.split("/")
            d = self.root / day
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            data = gzip.compress(json.dumps(doc, ensure_ascii=False).encode(), compresslevel=6)
            path = d / f"{rid}.json.gz"
            tmp = d / f".{rid}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            tmp.replace(path)
            self.bytes += len(data)
            fd = os.open(d / "index.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(_summary(doc), ensure_ascii=False) + "\n")
            # Only bodies that are really on disk may be referenced by later captures.
            with self.lock:
                for dg in rec.digests:
                    key = (rec.client, dg.slot)
                    q = self.index.setdefault(key, deque(maxlen=CANDIDATES))
                    q.append(dg)
                    self.index.move_to_end(key)
                while len(self.index) > 256:
                    self.index.popitem(last=False)
        except Exception:  # noqa: BLE001
            log.exception("capture: could not write %s", rec.ref)

    def prune(self, retention_days: float, max_mb: float) -> None:
        if not self.root.is_dir():
            self.bytes = 0
            return
        cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        days = sorted(p for p in self.root.iterdir() if p.is_dir() and _DAY_RE.match(p.name))
        for p in [p for p in days if p.name < cutoff]:
            shutil.rmtree(p, ignore_errors=True)
            days.remove(p)
            log.info("capture: removed %s (older than %g days)", p.name, retention_days)
        sizes = {p: sum(f.stat().st_size for f in p.iterdir() if f.is_file()) for p in days}
        while len(days) > 1 and sum(sizes.values()) > max_mb * 1024 * 1024:
            p = days.pop(0)
            shutil.rmtree(p, ignore_errors=True)
            sizes.pop(p)
            log.info("capture: removed %s (over max_mb)", p.name)
        self.bytes = sum(sizes.values())

    def flush(self) -> None:
        self.pool.submit(lambda: None).result()

    def close(self) -> None:
        self.pool.shutdown(wait=True)


def _summary(doc: dict) -> dict:
    out = {k: doc.get(k) for k in ("ref", "ts", "client", "route", "api", "stream", "served_by", "status",
                                   "ms", "tokens_in", "tokens_out", "cost_usd")}
    req = doc.get("request") or {}
    msgs = req.get("messages") or []
    base = req.get("$base") or {}
    out["messages"] = base.get("messages", 0) + len(msgs)
    out["preview"] = next((_text(m.get("content"))[:100] for m in reversed(msgs)
                           if isinstance(m, dict) and m.get("role") == "user"), "")
    return out


# ---------------------------------------------------------------------------
# Reading captures back
# ---------------------------------------------------------------------------


class Reader:
    def __init__(self, root: Path):
        self.root = root
        self.cache: dict[str, dict | None] = {}

    def load(self, ref: str) -> dict | None:
        if ref not in self.cache:
            day, _, rid = ref.partition("/")
            if not rid:  # a bare id: search the day directories
                day, rid = "", ref
                for p in sorted(self.root.glob(f"*/{rid}*.json.gz"), reverse=True):
                    day, rid = p.parent.name, p.name.removesuffix(".json.gz")
                    break
            path = self.root / day / f"{rid}.json.gz"
            try:
                self.cache[ref] = json.loads(gzip.decompress(path.read_bytes()))
            except (OSError, ValueError):
                self.cache[ref] = None
        return self.cache[ref]

    def _stored(self, ref: str, slot: str) -> dict | None:
        doc = self.load(ref)
        if doc is None:
            return None
        if slot == "request":
            return doc.get("request")
        bid = slot.split(":", 1)[1]
        return next((a.get("body") for a in doc.get("attempts", []) if a.get("block") == bid), None)

    def body(self, ref: str, slot: str) -> dict | None:
        """A stored body with every $base reference resolved."""
        chain: list[dict] = []
        cur = self._stored(ref, slot)
        if cur is None:
            return None
        while cur is not None:
            chain.append(cur)
            base = cur.get("$base")
            cur = self._stored(base["ref"], base["slot"]) if base else None
            if len(chain) > 10000:
                break
        # Oldest first: the root has no $base (or a missing one).
        full: dict | None = None
        for stored in reversed(chain):
            base = stored.get("$base")
            new = {k: v for k, v in stored.items() if k != "$base"}
            if base:
                key, n = base.get("key"), base.get("messages", 0)
                if full is None:
                    if key:
                        gap = {"role": "switchboard", "content": f"[{n} earlier item(s) are in capture "
                               f"{base['ref']}, which no longer exists]"}
                        new[key] = ([gap] if n else []) + new.get(key, [])
                    for f in base.get("same", []):
                        new.setdefault(f, f"[same as capture {base['ref']}, which no longer exists]")
                else:
                    if key:
                        new[key] = list(full.get(key, [])[:n]) + new.get(key, [])
                    for f in base.get("same", []):
                        new[f] = full.get(f)
            full = new
        return full

    def resolve(self, ref: str) -> dict | None:
        doc = self.load(ref)
        if doc is None:
            return None
        doc = copy.deepcopy(doc)
        real = doc["ref"]
        doc["request"] = self.body(real, "request")
        for a in doc.get("attempts", []):
            if "body" in a:
                a["body"] = self.body(real, f"upstream:{a['block']}")
        return doc

    def summaries(self, client: str | None = None, day: str | None = None, limit: int = 20) -> list[dict]:
        out: list[dict] = []
        days = sorted((p for p in self.root.iterdir() if p.is_dir() and _DAY_RE.match(p.name)),
                      reverse=True) if self.root.is_dir() else []
        for p in days:
            if day and p.name != day:
                continue
            try:
                lines = (p / "index.jsonl").read_text().splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                if client and s.get("client") != client:
                    continue
                out.append(s)
                if len(out) >= limit:
                    return out
        return out


# ---------------------------------------------------------------------------
# Human-readable rendering (OpenAI and Anthropic client formats)
# ---------------------------------------------------------------------------


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(t for t in (_part(p) for p in content) if t)
    return json.dumps(content, ensure_ascii=False)


def _part(p: Any) -> str:
    if not isinstance(p, dict):
        return str(p)
    t = p.get("type")
    if t == "text":
        return p.get("text", "")
    if t in ("image", "image_url", "input_image"):
        return "[image]"
    if t == "thinking":
        return f"[thinking] {p.get('thinking', '')}"
    if t == "redacted_thinking":
        return "[redacted thinking]"
    if t in ("tool_use", "server_tool_use"):
        return f"[tool call {p.get('name')} id={p.get('id')}] {json.dumps(p.get('input'), ensure_ascii=False)}"
    if t == "tool_result":
        err = " (error)" if p.get("is_error") else ""
        return f"[tool result id={p.get('tool_use_id')}{err}]\n{_text(p.get('content'))}"
    return json.dumps(p, ensure_ascii=False)


def _message(m: dict) -> tuple[str, str]:
    role = m.get("role", "?")
    body = _text(m.get("content"))
    if m.get("reasoning_content"):
        body = f"[reasoning] {m['reasoning_content']}\n{body}"
    for tc in m.get("tool_calls") or []:
        f = tc.get("function") or {}
        body += f"\n[tool call {f.get('name')} id={tc.get('id')}] {f.get('arguments')}"
    if role == "tool":
        role = f"tool result id={m.get('tool_call_id')}"
    return role, body.strip("\n")


def _trim(s: str, n: int | None) -> str:
    if n and len(s) > n:
        return s[:n] + f" ... [{len(s) - n} more chars]"
    return s


def render(doc: dict, trim: int | None = None, show_tools: bool = False) -> str:
    out: list[str] = []
    w = out.append
    tok = f"  tokens {doc.get('tokens_in')}/{doc.get('tokens_out')}" if doc.get("tokens_in") is not None else ""
    cost = f"  ${doc['cost_usd']}" if doc.get("cost_usd") else ""
    w(f"capture {doc.get('ref')}  {doc.get('ts')}")
    w(f"client {doc.get('client')}  route {doc.get('route')}  api {doc.get('api')}"
      f"{'  stream' if doc.get('stream') else ''}  status {doc.get('status')}  {doc.get('ms')} ms{tok}{cost}")
    for a in doc.get("attempts", []):
        res = "ok" if a.get("ok") else (f"skipped: {a['skipped']}" if a.get("skipped") else f"failed: {a.get('error')}")
        w(f"  attempt {a.get('block')}: {res}")
    if doc.get("note"):
        w(f"  note: {doc['note']}")
    req = doc.get("request") or {}
    params = {k: v for k, v in req.items() if k not in ("messages", "system", "tools", "contents")}
    if params:
        w(f"params: {json.dumps(params, ensure_ascii=False)}")
    if req.get("system"):
        w("\n=== system ===")
        w(_trim(_text(req["system"]), trim))
    tools = req.get("tools") or []
    if tools:
        names = [t.get("name") or (t.get("function") or {}).get("name") or t.get("type") for t in tools]
        w(f"\n=== tools ({len(tools)}) === {', '.join(str(n) for n in names)}")
        if show_tools:
            w(json.dumps(tools, indent=2, ensure_ascii=False))
    msgs = req.get("messages") or []
    w(f"\n=== messages ({len(msgs)}) ===")
    for i, m in enumerate(msgs):
        role, body = _message(m if isinstance(m, dict) else {"content": m})
        w(f"\n--- [{i}] {role} ---")
        w(_trim(body, trim))
    resp = doc.get("response")
    w("\n=== response ===")
    if resp is None:
        w("(none)")
    elif isinstance(resp, dict) and resp.get("choices"):
        for c in resp["choices"]:
            role, body = _message(c.get("message") or {})
            w(body)
            w(f"[finish_reason {c.get('finish_reason')}]")
    elif isinstance(resp, dict) and "content" in resp:
        w(_text(resp.get("content")))
        w(f"[stop_reason {resp.get('stop_reason')}]")
    else:
        w(json.dumps(resp, indent=2, ensure_ascii=False))
    return "\n".join(out)
