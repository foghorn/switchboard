"""HTTP surface: OpenAI + Anthropic compatible endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import signal
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import __version__
from .config import Client, Config, ConfigError, load_config
from .engine import Engine, error_response
from .inspector import inspector_routes

log = logging.getLogger("switchboard")


def _fmt_for(request: Request) -> str:
    return "anthropic" if request.url.path.startswith("/v1/messages") else "openai"


def _authenticate(engine: Engine, request: Request) -> Client | None:
    token = request.headers.get("x-api-key", "")
    auth = request.headers.get("authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        return None
    found = None
    for c in engine.cfg.clients:
        if hmac.compare_digest(c.key.encode(), token.encode()):
            found = c
    return found


def _model_obj(route: Any, created: int, ctx: int | None) -> dict:
    # One object both OpenAI and Anthropic SDKs can parse, carrying the context window
    # under every name common auto-detecting clients look for (vLLM, llama.cpp,
    # OpenRouter, Anthropic, ...).
    obj = {"id": route.name, "object": "model", "type": "model", "display_name": route.name,
           "created": created, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created)),
           "owned_by": "switchboard"}
    if ctx:
        obj |= {"max_input_tokens": ctx, "context_length": ctx, "max_context_length": ctx,
                "max_model_len": ctx, "meta": {"n_ctx": ctx}, "limit": {"context": ctx}}
    return obj


def create_app(engine: Engine) -> Starlette:
    started = int(time.time())

    async def guard(request: Request) -> tuple[Client | None, Response | None]:
        client = _authenticate(engine, request)
        if client is None:
            fmt = _fmt_for(request)
            return None, error_response(fmt, 401, "invalid or missing API key", "authentication_error")
        return client, None

    async def read_json(request: Request) -> dict | Response:
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return error_response(_fmt_for(request), 400, "body is not valid JSON", "invalid_request_error")
        if not isinstance(body, dict):
            return error_response(_fmt_for(request), 400, "body must be a JSON object", "invalid_request_error")
        return body

    async def completions(request: Request) -> Response:
        client, denied = await guard(request)
        if denied:
            return denied
        body = await read_json(request)
        if isinstance(body, Response):
            return body
        fmt = _fmt_for(request)
        if not body.get("messages"):
            return error_response(fmt, 400, "messages is required", "invalid_request_error")
        force = request.headers.get("x-switchboard-force") or None
        if force and not client.admin:
            return error_response(fmt, 403, "x-switchboard-force needs an admin key", "permission_error")
        want = request.headers.get("x-switchboard-capture")
        if want is not None and not client.admin:
            return error_response(fmt, 403, "x-switchboard-capture needs an admin key", "permission_error")
        capture = engine.capture_for(client) if want is None else want.strip().lower() in ("1", "true", "yes", "on")
        inbound = {k: v for k, v in request.headers.items() if k in ("anthropic-version", "anthropic-beta")}
        return await _until_disconnect(
            request, engine.handle(fmt, body, client.name, force, inbound, capture=capture))

    async def count_tokens(request: Request) -> Response:
        _, denied = await guard(request)
        if denied:
            return denied
        body = await read_json(request)
        if isinstance(body, Response):
            return body
        inbound = {k: v for k, v in request.headers.items() if k in ("anthropic-version", "anthropic-beta")}
        return JSONResponse(await engine.count_tokens(body, inbound))

    async def models(request: Request) -> Response:
        _, denied = await guard(request)
        if denied:
            return denied
        data = [_model_obj(r, started, engine.route_context(r)) for r in engine.cfg.routes]
        return JSONResponse({"object": "list", "data": data, "has_more": False,
                             "first_id": data[0]["id"] if data else None,
                             "last_id": data[-1]["id"] if data else None})

    async def model_one(request: Request) -> Response:
        _, denied = await guard(request)
        if denied:
            return denied
        name = request.path_params["model"]
        for r in engine.cfg.routes:
            if r.name == name:
                return JSONResponse(_model_obj(r, started, engine.route_context(r)))
        return error_response(_fmt_for(request), 404, f"model '{name}' not found", "not_found_error")

    async def health(request: Request) -> Response:
        # max_context = the default route's advertised window (the field some llama.cpp-based servers use).
        body = {"status": "ok", "version": __version__}
        if ctx := engine.route_context(engine.cfg.routes[0]):
            body["max_context"] = ctx
        return JSONResponse(body)

    async def props(request: Request) -> Response:
        # llama.cpp-style /props so clients that read n_ctx from there auto-detect too.
        _, denied = await guard(request)
        if denied:
            return denied
        route = engine.cfg.routes[0]
        ctx = engine.route_context(route)
        return JSONResponse({"model_alias": route.name, "default_generation_settings": {"n_ctx": ctx},
                             "total_slots": None, "build_info": f"switchboard {__version__}"})

    async def status(request: Request) -> Response:
        client, denied = await guard(request)
        if denied:
            return denied
        routes = []
        for r in engine.cfg.routes:
            blocks = []
            for b in r.blocks:
                d = {"id": b.id, "provider": b.provider, "enabled": b.enabled}
                if client.admin:
                    d |= {"base_url": b.base_url, "model": b.model}
                d |= engine.state.snapshot(b)
                blocks.append(d)
            routes.append({"name": r.name, "context_tokens": engine.route_context(r), "blocks": blocks})
        return JSONResponse({"version": __version__, "routes": routes})

    routes = [
        Route("/health", health),
        Route("/status", status),
        Route("/props", props),
        Route("/v1/chat/completions", completions, methods=["POST"]),
        Route("/chat/completions", completions, methods=["POST"]),
        Route("/v1/messages", completions, methods=["POST"]),
        Route("/v1/messages/count_tokens", count_tokens, methods=["POST"]),
        Route("/v1/models", models),
        Route("/models", models),
        Route("/v1/models/{model:path}", model_one),
        *inspector_routes(engine),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        tasks = [asyncio.create_task(engine.probe_loop()), asyncio.create_task(engine.context_loop())]
        if engine.config_path:
            tasks.append(asyncio.create_task(_watch_config(engine)))
            with contextlib.suppress(NotImplementedError, RuntimeError):
                asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, lambda: _reload(engine, "SIGHUP"))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await engine.close()

    return Starlette(routes=routes, lifespan=lifespan)


async def _until_disconnect(request: Request, work: Any) -> Response:
    """Run `work`, cancelling it if the client hangs up first.

    Until a response starts, nothing is sent to the client, so the framework never
    notices a disconnect by itself. Without this, an aborted request keeps the
    upstream busy (a long prompt read can take minutes) and may even fail over to
    the next block for a client that is gone. Cancelling closes the upstream
    connection, which tells the upstream server to stop. Once a stream has
    started, its own send loop notices disconnects.
    """

    async def hangup() -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass

    task = asyncio.ensure_future(work)
    watcher = asyncio.ensure_future(hangup())
    try:
        await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
    if task.cancelled():
        return Response(status_code=499)  # nobody is listening any more
    return task.result()


def _reload(engine: Engine, why: str) -> None:
    try:
        new: Config = load_config(engine.config_path)
    except ConfigError as e:
        log.error("config reload (%s) failed, keeping the old config: %s", why, e)
        return
    engine.cfg = new
    asyncio.get_running_loop().create_task(engine.refresh_contexts())
    log.info("config reloaded (%s): %d route(s), %d block(s)", why, len(new.routes), len(new.all_blocks()))


async def _watch_config(engine: Engine) -> None:
    path = engine.config_path
    try:
        last = os.stat(path).st_mtime
    except OSError:
        last = 0.0
    while True:
        await asyncio.sleep(5)
        try:
            m = os.stat(path).st_mtime
        except OSError:
            continue
        if m != last:
            last = m
            _reload(engine, "file changed")
