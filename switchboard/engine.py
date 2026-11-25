"""The failover engine: try a route's blocks in order until one answers."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import translate as tr
from .config import Block, Config, Route
from .providers import PROVIDERS, Provider, UpstreamError, Usage, classify_http_error
from .state import StateStore
from .streams import (
    SSE_OPENAI_DONE,
    AnthropicToOpenAI,
    GeminiToAnthropic,
    OpenAIToAnthropic,
    error_payload,
    sse_anthropic,
    sse_openai,
)

log = logging.getLogger("switchboard")


def estimate_tokens(body: dict) -> int:
    """Rough prompt size (~4 chars/token) for skipping blocks that are too small."""
    parts = [body.get("messages"), body.get("system"), body.get("tools")]
    return len(json.dumps(parts, ensure_ascii=False)) // 4


def _deep_merge(dst: dict, src: dict, only_missing: bool) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v, only_missing)
        elif not only_missing or k not in dst:
            dst[k] = copy.deepcopy(v)


def error_response(fmt: str, status: int, message: str, kind: str = "api_error",
                   headers: dict | None = None) -> JSONResponse:
    return JSONResponse(error_payload(fmt, message, kind), status_code=status, headers=headers)


# ---------------------------------------------------------------------------
# Output pipelines: provider-native stream items -> client SSE bytes
# ---------------------------------------------------------------------------


class Pipeline:
    def feed(self, item: Any) -> list[bytes]:
        raise NotImplementedError

    def finish(self) -> list[bytes]:
        raise NotImplementedError

    def error(self, message: str) -> list[bytes]:
        raise NotImplementedError


class PassOpenAI(Pipeline):
    def __init__(self, alias: str, strip_usage: bool):
        self.alias, self.strip_usage = alias, strip_usage

    def feed(self, chunk: dict) -> list[bytes]:
        chunk["model"] = self.alias
        if self.strip_usage:
            if not chunk.get("choices"):
                return []
            chunk.pop("usage", None)
        return [sse_openai(chunk)]

    def finish(self) -> list[bytes]:
        return [SSE_OPENAI_DONE]

    def error(self, message: str) -> list[bytes]:
        return [sse_openai(error_payload("openai", message)), SSE_OPENAI_DONE]


class PassAnthropic(Pipeline):
    def __init__(self, alias: str):
        self.alias = alias

    def feed(self, item: tuple[str, dict]) -> list[bytes]:
        name, data = item
        if name == "message_start" and isinstance(data.get("message"), dict):
            data["message"]["model"] = self.alias
        return [sse_anthropic(name, data)]

    def finish(self) -> list[bytes]:
        return []

    def error(self, message: str) -> list[bytes]:
        return [sse_anthropic("error", error_payload("anthropic", message))]


class Converted(Pipeline):
    """Upstream -> Anthropic events -> client format."""

    def __init__(self, upstream_fmt: str, client_fmt: str, alias: str, include_usage: bool):
        self.to_an = {"openai": OpenAIToAnthropic, "gemini": GeminiToAnthropic}.get(upstream_fmt)
        self.to_an = self.to_an(alias) if self.to_an else None
        self.client_fmt = client_fmt
        self.a2o = AnthropicToOpenAI(alias, include_usage) if client_fmt == "openai" else None
        self.done_sent = False

    def _emit(self, events: list[tuple[str, dict]]) -> list[bytes]:
        if self.a2o is None:
            return [sse_anthropic(n, d) for n, d in events]
        out = [sse_openai(c) for e in events for c in self.a2o.feed(e)]
        if self.a2o.done and not self.done_sent:
            self.done_sent = True
            out.append(SSE_OPENAI_DONE)
        return out

    def feed(self, item: Any) -> list[bytes]:
        events = self.to_an.feed(item) if self.to_an else [item]
        return self._emit(events)

    def finish(self) -> list[bytes]:
        out = self._emit(self.to_an.finish_events() if self.to_an else [])
        if self.a2o is not None and not self.done_sent:
            out += [sse_openai(c) for c in self.a2o.finish_chunks()]
            out.append(SSE_OPENAI_DONE)
            self.done_sent = True
        return out

    def error(self, message: str) -> list[bytes]:
        if self.client_fmt == "anthropic":
            return [sse_anthropic("error", error_payload("anthropic", message))]
        self.done_sent = True
        return [sse_openai(error_payload("openai", message)), SSE_OPENAI_DONE]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class Engine:
    def __init__(self, cfg: Config, config_path: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.config_path = config_path
        self.state = StateStore(Path(cfg.server.state_dir))
        self.http = httpx.AsyncClient(transport=transport, limits=httpx.Limits(max_connections=200))

    async def close(self) -> None:
        await self.http.aclose()

    # -- block selection --------------------------------------------------

    def plan(self, route: Route, body: dict, force: str | None) -> tuple[list[Block], list[dict]]:
        """Blocks to try in order, plus the skipped ones with reasons."""
        if force:
            return [b for b in route.blocks if b.id == force], []
        est = estimate_tokens(body)
        tryable, cooling, skipped = [], [], []
        for b in route.blocks:
            if not b.enabled:
                continue
            ctx = self.state.effective_context(b)
            if ctx and est > ctx:
                skipped.append({"block": b.id, "skipped": f"prompt ~{est} tokens > context {ctx}"})
            elif self.state.over_cap(b):
                skipped.append({"block": b.id, "skipped": f"daily spend cap ${b.spend_cap_usd_per_day} reached"})
            elif self.state.get(b.id).cooling():
                cooling.append(b)
                skipped.append({"block": b.id, "skipped": "cooling down"})
            else:
                tryable.append(b)
        if not tryable and cooling:
            # Everything usable is cooling down: try them anyway rather than fail outright.
            skipped = [s for s in skipped if s["skipped"] != "cooling down"]
            tryable = cooling
        return tryable, skipped

    # -- body building ----------------------------------------------------

    def build_body(self, block: Block, client_fmt: str, body: dict, stream: bool) -> dict:
        p = PROVIDERS[block.provider]
        if p.fmt == client_fmt:
            nb = copy.deepcopy(body)
        else:
            canon = body if client_fmt == "anthropic" else tr.openai_to_anthropic_request(body)
            if p.fmt == "anthropic":
                nb = copy.deepcopy(canon)
            elif p.fmt == "openai":
                nb = tr.anthropic_to_openai_request(canon)
            else:
                nb = tr.anthropic_to_gemini_request(canon)

        if p.fmt == "gemini":
            nb.pop("model", None)
            nb.pop("stream", None)
            if block.default_max_tokens:
                nb.setdefault("generationConfig", {}).setdefault("maxOutputTokens", block.default_max_tokens)
        else:
            nb["model"] = block.model
            if stream:
                nb["stream"] = True
            else:
                nb.pop("stream", None)
            if p.fmt == "openai":
                nb.pop("stream_options", None)
                if stream:
                    nb["stream_options"] = {"include_usage": True}
                if block.default_max_tokens and not (nb.get("max_tokens") or nb.get("max_completion_tokens")):
                    nb["max_tokens"] = block.default_max_tokens
            else:
                if not nb.get("max_tokens"):
                    nb["max_tokens"] = block.default_max_tokens or 8192

        _deep_merge(nb, block.defaults, only_missing=True)
        _deep_merge(nb, block.overrides, only_missing=False)
        for k in block.drop_params:
            nb.pop(k, None)
        return nb

    def convert_response(self, p: Provider, client_fmt: str, data: dict, alias: str) -> dict:
        if p.fmt == client_fmt:
            data["model"] = alias
            return data
        if p.fmt == "anthropic":
            canon = data
            canon["model"] = alias
        elif p.fmt == "openai":
            canon = tr.openai_to_anthropic_response(data, alias)
        else:
            canon = tr.gemini_to_anthropic_response(data, alias)
        return canon if client_fmt == "anthropic" else tr.anthropic_to_openai_response(canon, alias)

    # -- main entry -------------------------------------------------------

    async def handle(self, client_fmt: str, body: dict, client: str, force: str | None,
                     inbound_headers: dict[str, str]) -> Response:
        cfg = self.cfg
        route = cfg.route(body.get("model"))
        if route is None:
            return error_response(client_fmt, 404, f"model '{body.get('model')}' not found",
                                  "not_found_error" if client_fmt == "anthropic" else "invalid_request_error")
        stream = bool(body.get("stream"))
        rid = uuid.uuid4().hex[:12]
        t0 = time.monotonic()
        blocks, attempts = self.plan(route, body, force)
        if force and not blocks:
            return error_response(client_fmt, 400, f"forced block '{force}' is not in route '{route.name}'",
                                  "invalid_request_error")

        entry: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "id": rid, "client": client,
                                 "route": route.name, "api": client_fmt, "stream": stream}

        for block in blocks:
            a0 = time.monotonic()
            try:
                if stream:
                    resp = await self._try_stream(block, client_fmt, body, route.name, inbound_headers,
                                                  entry, attempts, t0)
                else:
                    resp = await self._try_once(block, client_fmt, body, route.name, inbound_headers,
                                                entry, attempts, t0)
            except UpstreamError as e:
                ms = int((time.monotonic() - a0) * 1000)
                attempts.append({"block": block.id, "error": e.message, "status": e.status, "ms": ms})
                if e.counts_against_block:
                    self.state.failure(block, e.message)
                log.warning("[%s] block %s failed (%s ms): %s", rid, block.id, ms, e.message)
                if not e.failover:
                    self._log(entry, attempts, None, t0, status=e.status)
                    return error_response(client_fmt, e.status or 400, e.message, "invalid_request_error")
                continue
            if cfg.server.expose_block_header:
                resp.headers["x-switchboard-block"] = block.id
            return resp

        summary = "; ".join(f"{a['block']}: {a.get('error') or a.get('skipped')}" for a in attempts)
        self._log(entry, attempts, None, t0, status=503)
        return error_response(client_fmt, 503, f"all providers failed - {summary or 'no usable blocks'}",
                              "overloaded_error" if client_fmt == "anthropic" else "api_error")

    def _log(self, entry: dict, attempts: list, block: Block | None, t0: float,
             status: int = 200, usage: Usage | None = None, ttft_ms: int | None = None,
             note: str | None = None) -> None:
        e = dict(entry, attempts=attempts, served_by=block.id if block else None, status=status,
                 ms=int((time.monotonic() - t0) * 1000))
        if ttft_ms is not None:
            e["ttft_ms"] = ttft_ms
        if usage is not None:
            e["tokens_in"], e["tokens_out"] = usage.input, usage.output
            if block and block.pricing:
                cost = block.pricing.cost(usage.input, usage.output)
                e["cost_usd"] = round(cost, 6)
                self.state.add_spend(block, cost)
        if note:
            e["note"] = note
        self.state.log_request(e)

    # -- non-streaming ----------------------------------------------------

    async def _try_once(self, block: Block, client_fmt: str, body: dict, alias: str,
                        inbound: dict[str, str], entry: dict, attempts: list, t0: float) -> Response:
        p = PROVIDERS[block.provider]
        nb = self.build_body(block, client_fmt, body, stream=False)
        to = block.timeouts
        timeout = httpx.Timeout(connect=to.connect, read=to.total, write=to.total, pool=to.connect)
        try:
            r = await asyncio.wait_for(
                self.http.post(p.url(block, False), json=nb, headers=p.headers(block, inbound), timeout=timeout),
                timeout=to.total)
        except TimeoutError as e:
            raise UpstreamError(f"no response within {to.total:g}s") from e
        except httpx.TimeoutException as e:
            raise UpstreamError(f"timeout: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise UpstreamError(f"{type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            raise classify_http_error(block, r.status_code, r.text)
        try:
            data = r.json()
        except ValueError as e:
            raise UpstreamError("response is not JSON") from e
        if err := p.response_error(data):
            raise UpstreamError(f"bad response: {err}")
        usage = Usage(p.fmt)
        usage.observe_response(data)
        out = self.convert_response(p, client_fmt, data, alias)
        self.state.success(block)
        attempts.append({"block": block.id, "ok": True})
        self._log(entry, attempts, block, t0, usage=usage)
        return JSONResponse(out)

    # -- streaming --------------------------------------------------------

    async def _try_stream(self, block: Block, client_fmt: str, body: dict, alias: str,
                          inbound: dict[str, str], entry: dict, attempts: list, t0: float) -> Response:
        p = PROVIDERS[block.provider]
        nb = self.build_body(block, client_fmt, body, stream=True)
        to = block.timeouts
        start = time.monotonic()
        first_deadline = start + to.first_token
        total_deadline = start + to.total
        client_wants_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        if p.fmt == client_fmt == "openai":
            pipe: Pipeline = PassOpenAI(alias, strip_usage=not client_wants_usage)
        elif p.fmt == client_fmt == "anthropic":
            pipe = PassAnthropic(alias)
        else:
            pipe = Converted(p.fmt, client_fmt, alias, client_wants_usage)

        req = self.http.build_request(
            "POST", p.url(block, True), json=nb, headers=p.headers(block, inbound),
            timeout=httpx.Timeout(connect=to.connect, read=None, write=to.total, pool=to.connect))
        resp: httpx.Response | None = None
        try:
            resp = await asyncio.wait_for(self.http.send(req, stream=True), timeout=to.first_token)
            if resp.status_code >= 400:
                text = (await asyncio.wait_for(resp.aread(), timeout=to.connect + 10)).decode("utf-8", "replace")
                raise classify_http_error(block, resp.status_code, text)
            items = p.items(resp).__aiter__()
            usage = Usage(p.fmt)
            buffered: list[bytes] = []
            while True:
                remaining = first_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    item = await asyncio.wait_for(items.__anext__(), timeout=remaining)
                except StopAsyncIteration as e:
                    raise UpstreamError("stream ended before any output") from e
                if err := p.item_error(item):
                    raise UpstreamError(f"stream error: {err}")
                usage.observe_item(item)
                buffered += pipe.feed(item)
                if p.is_progress(item):
                    break
        except UpstreamError:
            if resp is not None:
                await resp.aclose()
            raise
        except TimeoutError as e:
            if resp is not None:
                await resp.aclose()
            raise UpstreamError(f"no output within first_token timeout ({to.first_token:g}s)") from e
        except httpx.HTTPError as e:
            if resp is not None:
                await resp.aclose()
            raise UpstreamError(f"{type(e).__name__}: {e}") from e

        ttft_ms = int((time.monotonic() - start) * 1000)
        self.state.success(block)
        attempts.append({"block": block.id, "ok": True})
        assert resp is not None

        async def gen() -> AsyncIterator[bytes]:
            status, note = 200, None
            try:
                for b in buffered:
                    yield b
                while True:
                    remaining = min(to.idle, total_deadline - time.monotonic())
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        item = await asyncio.wait_for(items.__anext__(), timeout=remaining)
                    except StopAsyncIteration:
                        break
                    if err := p.item_error(item):
                        note = f"upstream stream error: {err}"
                        for b in pipe.error(note):
                            yield b
                        status = 502
                        return
                    usage.observe_item(item)
                    for b in pipe.feed(item):
                        yield b
                for b in pipe.finish():
                    yield b
            except TimeoutError:
                note = f"stream stalled (idle {to.idle:g}s / total {to.total:g}s)"
                status = 504
                for b in pipe.error(note):
                    yield b
            except httpx.HTTPError as e:
                note = f"upstream connection lost mid-stream: {type(e).__name__}"
                status = 502
                for b in pipe.error(note):
                    yield b
            except asyncio.CancelledError:
                note, status = "client disconnected", 499
                raise
            finally:
                await resp.aclose()
                if status != 200:
                    log.warning("[%s] %s: %s", entry["id"], block.id, note)
                self._log(entry, attempts, block, t0, status=status, usage=usage, ttft_ms=ttft_ms, note=note)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache", "x-accel-buffering": "no"})

    # -- probes -----------------------------------------------------------

    async def probe(self, block: Block) -> tuple[bool, str]:
        p = PROVIDERS[block.provider]
        try:
            r = await self.http.get(p.probe_url(block), headers=p.headers(block, {}),
                                    timeout=httpx.Timeout(block.timeouts.connect + 5))
        except httpx.HTTPError as e:
            return False, f"{type(e).__name__}: {e}"
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        return True, "ok"

    async def probe_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.server.probe_interval)
            for b in self.cfg.all_blocks():
                if b.enabled and b.cooldown.probe and self.state.get(b.id).cooling():
                    ok, _ = await self.probe(b)
                    if ok:
                        log.info("probe: %s is reachable again; ending its cooldown", b.id)
                        self.state.probe_ok(b)

    # -- context window detection -----------------------------------------

    async def _get_json(self, url: str, headers: dict[str, str]) -> Any:
        try:
            r = await self.http.get(url, headers=headers, timeout=httpx.Timeout(8.0))
            if r.status_code == 200:
                return r.json()
        except (httpx.HTTPError, ValueError):
            pass
        return None

    async def detect_context(self, block: Block) -> tuple[int | None, str | None]:
        """Ask the upstream how big its context window is. Best effort, never raises."""
        p = PROVIDERS[block.provider]
        h = p.headers(block, {})
        h.pop("content-type", None)

        def first_int(*vals: Any) -> int | None:
            for v in vals:
                if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                    return v
            return None

        if p.fmt == "anthropic":
            d = await self._get_json(f"{p.probe_url(block)}/{block.model}", h)
            v = first_int((d or {}).get("max_input_tokens"))
            return (v, "anthropic models API") if v else (None, None)

        if p.fmt == "gemini":
            d = await self._get_json(p.probe_url(block), h)
            v = first_int((d or {}).get("inputTokenLimit"))
            return (v, "gemini models API") if v else (None, None)

        # OpenAI-compatible: the models list first (vLLM, llama.cpp router, LM Studio, ...)
        d = await self._get_json(f"{block.base_url}/models", h)
        entries = d.get("data") if isinstance(d, dict) else None
        for m in entries if isinstance(entries, list) else []:
            if isinstance(m, dict) and m.get("id") == block.model:
                meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
                limit = m.get("limit") if isinstance(m.get("limit"), dict) else {}
                v = first_int(m.get("context_length"), m.get("max_context_length"), m.get("max_model_len"),
                              m.get("max_input_tokens"), meta.get("n_ctx"), limit.get("context"),
                              m.get("context_window"))
                if v:
                    return v, "models endpoint"
        root = block.base_url[:-3] if block.base_url.endswith("/v1") else block.base_url
        # Ollama: the window the model is actually loaded with (absent while unloaded;
        # /api/show would give the architecture maximum instead, which is not what runs).
        ps = await self._get_json(f"{root}/api/ps", h)
        if isinstance(ps, dict):
            for m in ps.get("models") or []:
                if isinstance(m, dict) and block.model in (m.get("name"), m.get("model")):
                    if v := first_int(m.get("context_length")):
                        return v, "ollama /api/ps"
        # llama.cpp server: one model per server, n_ctx per slot.
        props = await self._get_json(f"{root}/props", h)
        if isinstance(props, dict):
            gen = props.get("default_generation_settings") or {}
            if v := first_int(gen.get("n_ctx") if isinstance(gen, dict) else None, props.get("n_ctx")):
                return v, "llama.cpp /props"
        return None, None

    async def refresh_contexts(self) -> None:
        blocks = [b for b in self.cfg.all_blocks() if b.enabled and b.max_context_tokens == "auto"]
        results = await asyncio.gather(*(self.detect_context(b) for b in blocks))
        for b, (v, src) in zip(blocks, results, strict=True):
            s = self.state.get(b.id)
            if v:
                if v != s.ctx_detected:
                    log.info("context window of %s: %d (from %s)", b.id, v, src)
                s.ctx_detected, s.ctx_source, s.ctx_detected_at = v, src, time.time()
            elif s.ctx_detected is None:
                log.info("context window of %s: not reported%s", b.id,
                         f"; using context_fallback {b.context_fallback}" if b.context_fallback else "")
            # else: keep the last good value (e.g. Ollama model unloaded since)

    async def context_loop(self) -> None:
        while True:
            try:
                await self.refresh_contexts()
            except Exception:  # noqa: BLE001 - never let detection kill the server
                log.exception("context detection failed")
            await asyncio.sleep(self.cfg.server.context_refresh)

    def route_context(self, route: Route) -> int | None:
        if isinstance(route.context_tokens, int):
            return route.context_tokens
        if route.context_tokens is None:
            return None
        vals = [c for b in route.blocks if b.enabled and (c := self.state.effective_context(b))]
        if not vals:
            return None
        return max(vals) if route.context_tokens == "max" else min(vals)

    # -- count_tokens -----------------------------------------------------

    async def count_tokens(self, body: dict, inbound: dict[str, str]) -> dict:
        route = self.cfg.route(body.get("model"))
        if route:
            for b in route.blocks:
                if b.provider != "anthropic" or not b.enabled or self.state.get(b.id).cooling():
                    continue
                p = PROVIDERS["anthropic"]
                nb = {k: v for k, v in body.items() if k in ("messages", "system", "tools", "tool_choice", "thinking")}
                nb["model"] = b.model
                try:
                    r = await self.http.post(p.url(b, False) + "/count_tokens", json=nb, headers=p.headers(b, inbound),
                                             timeout=httpx.Timeout(30))
                    if r.status_code == 200:
                        return r.json()
                except httpx.HTTPError:
                    pass
        return {"input_tokens": estimate_tokens(body)}
