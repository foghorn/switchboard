"""The /inspector page: live request feed and prompt breakdown (admin keys only).

Off unless server.inspector.enabled is true; every route then answers 404.
Prompt contents are shown only for requests that were captured, so the page
never reveals more than the capture settings already keep on disk.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from importlib import resources
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import __version__
from .anatomy import analyze, response_view
from .capture import Reader
from .engine import Engine

_STATIC = {"": ("inspector.html", "text/html; charset=utf-8"),
           "app.js": ("inspector.js", "text/javascript; charset=utf-8"),
           "app.css": ("inspector.css", "text/css; charset=utf-8")}
_SECURITY = {
    "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}
_STALE = 6 * 3600  # an in-flight entry this old lost its request (e.g. a stream nobody read)


def _public(live: dict) -> dict:
    out = {k: v for k, v in live.items() if not k.startswith("_")}
    out["attempts"] = [dict(a) for a in live.get("attempts", [])]
    out["elapsed_ms"] = int((time.time() - live["started"]) * 1000)
    return out


def inspector_routes(engine: Engine) -> list[Route]:
    static: dict[str, bytes] = {}

    def enabled() -> bool:
        return engine.cfg.server.inspector.enabled

    def not_found() -> Response:
        return Response("Not Found", status_code=404, media_type="text/plain")

    def check(request: Request) -> Response | None:
        if not enabled():
            return not_found()
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        client = None
        for c in engine.cfg.clients:
            if token and hmac.compare_digest(c.key.encode(), token.encode()):
                client = c
        if client is None:
            return JSONResponse({"error": "Enter an admin key."}, status_code=401, headers=_SECURITY)
        if not client.admin:
            return JSONResponse({"error": "That key works, but it isn't an admin key."}, status_code=403,
                                headers=_SECURITY)
        return None

    async def page(request: Request) -> Response:
        if not enabled():
            return not_found()
        name = request.path_params.get("file", "")
        if name not in _STATIC:
            return not_found()
        fname, ctype = _STATIC[name]
        if fname not in static:
            static[fname] = resources.files("switchboard").joinpath("static", fname).read_bytes()
        return Response(static[fname], media_type=ctype, headers=_SECURITY)

    async def feed(request: Request) -> Response:
        if denied := check(request):
            return denied
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError:
            after = 0
        now = time.time()
        for rid in [r for r, v in engine.active.items() if now - v["started"] > _STALE]:
            engine.active.pop(rid, None)
        entries = [e for e in engine.recent if e["seq"] > after]
        return JSONResponse({
            "version": __version__,
            "seq": engine.seq,
            "entries": entries,
            "active": [_public(v) for v in sorted(engine.active.values(), key=lambda v: v["started"])],
            "capture_all": engine.capture_overrides["all"],
            "clients": [{"name": c.name, "capture": engine.capture_for(c), "config": c.capture,
                         "override": engine.capture_overrides["clients"].get(c.name)} for c in engine.cfg.clients],
            "blocks": [{"id": b.id, "route": r.name, "provider": b.provider}
                       for r in engine.cfg.routes for b in r.blocks],
        }, headers=_SECURITY)

    def _find(rid: str) -> dict | None:
        for e in reversed(engine.recent):
            if e.get("id") == rid:
                return e
        return None

    async def detail(request: Request) -> Response:
        if denied := check(request):
            return denied
        rid = request.path_params["rid"]
        if live := engine.active.get(rid):
            out: dict[str, Any] = {"state": "active", "entry": _public(live)}
            if "_body" in live:
                out["anatomy"] = await asyncio.to_thread(analyze, live["_body"], None)
            return JSONResponse(out, headers=_SECURITY)
        entry = _find(rid)
        if entry is None:
            return JSONResponse({"error": "That request is no longer in memory."}, status_code=404,
                                headers=_SECURITY)
        out = {"state": "done", "entry": entry}
        if ref := entry.get("capture"):
            doc = await asyncio.to_thread(_load_capture, engine, ref)
            if doc is None:
                out["capture_missing"] = True
            else:
                out["anatomy"] = await asyncio.to_thread(analyze, doc.get("request") or {}, doc.get("tokens_in"))
                out["response"] = response_view(doc.get("response"))
                out["upstream_blocks"] = [a["block"] for a in doc.get("attempts", []) if "body" in a]
        return JSONResponse(out, headers=_SECURITY)

    async def raw(request: Request) -> Response:
        """The captured request (or what one block was sent) as JSON."""
        if denied := check(request):
            return denied
        entry = _find(request.path_params["rid"])
        if entry is None or not entry.get("capture"):
            return JSONResponse({"error": "No capture for that request."}, status_code=404, headers=_SECURITY)
        doc = await asyncio.to_thread(_load_capture, engine, entry["capture"])
        if doc is None:
            return JSONResponse({"error": "The capture file is gone."}, status_code=404, headers=_SECURITY)
        block = request.query_params.get("block")
        if block:
            body = next((a.get("body") for a in doc.get("attempts", []) if a.get("block") == block), None)
            if body is None:
                return JSONResponse({"error": f"No body recorded for {block}."}, status_code=404, headers=_SECURITY)
            return JSONResponse(body, headers=_SECURITY)
        return JSONResponse(doc, headers=_SECURITY)

    async def set_capture(request: Request) -> Response:
        """{"client": name or null for every client, "on": true/false, or null = back to the config file}."""
        if denied := check(request):
            return denied
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or "on" not in body or body.get("on") not in (True, False, None):
            return JSONResponse({"error": 'Send {"client": name or null, "on": true, false or null}.'},
                                status_code=400, headers=_SECURITY)
        client = body.get("client")
        if client is None and body["on"] is None:
            return JSONResponse({"error": "The capture-everything switch is on or off."}, status_code=400,
                                headers=_SECURITY)
        if client is not None and client not in {c.name for c in engine.cfg.clients}:
            return JSONResponse({"error": f"No client named {client!r}."}, status_code=404, headers=_SECURITY)
        engine.set_capture(client, body["on"])
        return JSONResponse({"capture_all": engine.capture_overrides["all"],
                             "clients": {c.name: engine.capture_for(c) for c in engine.cfg.clients}},
                            headers=_SECURITY)

    return [
        Route("/inspector", page),
        Route("/inspector/api/capture", set_capture, methods=["POST"]),
        Route("/inspector/{file}", page),
        Route("/inspector/api/feed", feed),
        Route("/inspector/api/request/{rid}", detail),
        Route("/inspector/api/request/{rid}/raw", raw),
    ]


def _load_capture(engine: Engine, ref: str) -> dict | None:
    engine.captures.flush()  # the file is written on a background thread
    return Reader(engine.captures.root).resolve(ref)
