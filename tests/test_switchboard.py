"""Switchboard tests against fake upstreams (httpx.MockTransport)."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict

import httpx
import pytest
from starlette.testclient import TestClient

from switchboard import translate as tr
from switchboard.app import create_app
from switchboard.config import Config, ConfigError, load_config
from switchboard.engine import Engine

KEY = "test-client-key-0123456789"
ADMIN = "test-admin-key-0123456789"


def make_cfg(tmp_path, blocks, **server):
    return Config.model_validate({
        "server": {"state_dir": str(tmp_path / "state"), "probe_interval": 3600, **server},
        "clients": [{"name": "tester", "key": KEY}, {"name": "admin", "key": ADMIN, "admin": True}],
        "routes": [{"name": "gateway", "context_tokens": 200000, "blocks": blocks}],
    })


def oa_block(id_, host, **kw):
    return {"id": id_, "provider": "openai", "base_url": f"http://{host}/v1", "api_key": "k",
            "model": f"{id_}-model", "timeouts": {"connect": 1, "first_token": 0.5, "idle": 1, "total": 5}, **kw}


def an_block(id_="haiku", **kw):
    return {"id": id_, "provider": "anthropic", "base_url": "https://api.anthropic.com", "api_key": "test-anthropic-key",
            "model": "claude-haiku-5-5", "default_max_tokens": 1000,
            "timeouts": {"connect": 1, "first_token": 0.5, "idle": 1, "total": 5}, **kw}


def gem_block(id_="gem", **kw):
    return {"id": id_, "provider": "gemini", "base_url": "https://generativelanguage.googleapis.com",
            "api_key": "AIza-x", "model": "gemini-x", "timeouts": {"connect": 1, "first_token": 0.5, "idle": 1, "total": 5},
            **kw}


class Fake:
    """Routes requests by host to per-host handlers and records them."""

    def __init__(self):
        self.handlers = {}
        self.calls = defaultdict(list)
        self.gets = {}  # (host, path) -> JSON for GET requests (context detection, probes)

    def on(self, host, handler):
        self.handlers[host] = handler

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.method == "GET":
            data = self.gets.get((host, request.url.path))
            return httpx.Response(200, json=data) if data is not None else httpx.Response(404, json={})
        body = json.loads(request.content) if request.content else None
        self.calls[host].append({"url": str(request.url), "body": body, "headers": dict(request.headers)})
        h = self.handlers.get(host)
        if h is None:
            raise httpx.ConnectError("connection refused", request=request)
        r = h(request, body)
        if asyncio.iscoroutine(r):
            r = await r
        return r


def client_for(tmp_path, blocks, fake, **server):
    eng = Engine(make_cfg(tmp_path, blocks, **server), transport=httpx.MockTransport(fake))
    return TestClient(create_app(eng)), eng


def oa_resp(text="hello", tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "x", "object": "chat.completion", "created": 1, "model": "upstream-model",
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}


def an_resp(content=None, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": content or [{"type": "text", "text": "from haiku"}], "stop_reason": stop,
            "stop_sequence": None, "usage": {"input_tokens": 20, "output_tokens": 5}}


def sse_stream(lines, delay=0.0):
    async def gen():
        for line in lines:
            if delay:
                await asyncio.sleep(delay)
            yield line.encode()
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())


def oa_sse(chunks):
    return [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]


def an_sse(events):
    return [f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events]


H = {"authorization": f"Bearer {KEY}"}
CHAT = {"model": "gateway", "messages": [{"role": "user", "content": "hi"}]}


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


def test_auth_and_models(tmp_path):
    fake = Fake()
    tc, _ = client_for(tmp_path, [oa_block("local", "gpu-box")], fake)
    with tc:
        assert tc.get("/health").status_code == 200
        assert tc.get("/v1/models").status_code == 401
        r = tc.get("/v1/models", headers={"x-api-key": KEY})
        assert r.status_code == 200
        ids = [m["id"] for m in r.json()["data"]]
        assert ids == ["gateway"]
        assert r.json()["data"][0]["max_input_tokens"] == 200000
        assert tc.post("/v1/chat/completions", json=CHAT, headers={"authorization": "Bearer wrong-key-xxxxxxxx"}).status_code == 401


def test_openai_passthrough_rewrites_model(tmp_path):
    fake = Fake()
    fake.on("gpu-box", lambda req, body: httpx.Response(200, json=oa_resp()))
    tc, _ = client_for(tmp_path, [oa_block("local", "gpu-box")], fake)
    with tc:
        r = tc.post("/v1/chat/completions", json=CHAT | {"temperature": 0.2}, headers=H)
    assert r.status_code == 200
    assert r.json()["model"] == "gateway"
    assert r.json()["choices"][0]["message"]["content"] == "hello"
    sent = fake.calls["gpu-box"][0]
    assert sent["body"]["model"] == "local-model"
    assert sent["body"]["temperature"] == 0.2
    assert sent["headers"]["authorization"] == "Bearer k"
    assert sent["url"] == "http://gpu-box/v1/chat/completions"


def test_failover_chain_to_anthropic(tmp_path):
    fake = Fake()
    # gpu-box: connection refused (no handler); backup: 503; anthropic: ok
    fake.on("ollama-box", lambda req, body: httpx.Response(503, json={"error": {"message": "engine exited"}}))
    fake.on("api.anthropic.com", lambda req, body: httpx.Response(200, json=an_resp()))
    tc, _ = client_for(tmp_path, [oa_block("local", "gpu-box"), oa_block("ollama-box", "ollama-box"), an_block()], fake,
                       expose_block_header=True)
    with tc:
        r = tc.post("/v1/chat/completions", json=CHAT | {"temperature": 0}, headers=H)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["model"] == "gateway"
    assert j["choices"][0]["message"]["content"] == "from haiku"
    assert r.headers["x-switchboard-block"] == "haiku"
    sent = fake.calls["api.anthropic.com"][0]
    assert sent["body"]["model"] == "claude-haiku-5-5"
    assert sent["body"]["max_tokens"] == 1000
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    log = (tmp_path / "state" / "requests.jsonl").read_text().strip().splitlines()
    entry = json.loads(log[-1])
    assert entry["served_by"] == "haiku"
    assert [a["block"] for a in entry["attempts"]] == ["local", "ollama-box", "haiku"]


def test_all_fail_returns_503_with_reasons(tmp_path):
    fake = Fake()
    fake.on("ollama-box", lambda req, body: httpx.Response(500, text="boom"))
    tc, _ = client_for(tmp_path, [oa_block("local", "gpu-box"), oa_block("ollama-box", "ollama-box")], fake)
    with tc:
        r = tc.post("/v1/messages", json={"model": "gateway", "max_tokens": 10,
                                          "messages": [{"role": "user", "content": "hi"}]}, headers=H)
    assert r.status_code == 503
    j = r.json()
    assert j["type"] == "error"
    assert "local" in j["error"]["message"] and "ollama-box" in j["error"]["message"]


def test_400_client_error_does_not_fail_over(tmp_path):
    fake = Fake()
    fake.on("gpu-box", lambda req, body: httpx.Response(400, json={"error": {"message": "invalid tool schema"}}))
    fake.on("ollama-box", lambda req, body: httpx.Response(200, json=oa_resp()))
    tc, eng = client_for(tmp_path, [oa_block("local", "gpu-box"), oa_block("ollama-box", "ollama-box")], fake)
    with tc:
        r = tc.post("/v1/chat/completions", json=CHAT, headers=H)
    assert r.status_code == 400
    assert "invalid tool schema" in r.json()["error"]["message"]
    assert not fake.calls["ollama-box"]
    assert eng.state.get("local").failures == 0  # a bad request isn't the block's fault


def test_400_context_error_fails_over(tmp_path):
    fake = Fake()
    fake.on("gpu-box", lambda req, body: httpx.Response(400, json={"error": {"message": "the request exceeds the available context size"}}))
    fake.on("ollama-box", lambda req, body: httpx.Response(200, json=oa_resp("backup answer")))
    tc, _ = client_for(tmp_path, [oa_block("local", "gpu-box"), oa_block("ollama-box", "ollama-box")], fake)
    with tc:
        r = tc.post("/v1/chat/completions", json=CHAT, headers=H)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "backup answer"


def test_cooldown_skips_block_then_all_cooling_tries_anyway(tmp_path):
    fake = Fake()
    fake.on("ollama-box", lambda req, body: httpx.Response(200, json=oa_resp("ollama-box")))
    b1 = oa_block("local", "gpu-box", cooldown={"after_failures": 2, "seconds": 60, "probe": False})
    tc, eng = client_for(tmp_path, [b1, oa_block("ollama-box", "ollama-box")], fake)
    with tc:
        for _ in range(3):
            assert tc.post("/v1/chat/completions", json=CHAT, headers=H).status_code == 200
        # local tried twice, then cooling down and skipped on the third request
        assert len(fake.calls["gpu-box"]) == 2
        assert eng.state.get("local").cooling()
        # Now take backup away too: backup fails (counts), local is cooling -> still tried? backup is tryable first.
        fake.handlers.pop("ollama-box")
        for _ in range(2):
            tc.post("/v1/chat/completions", json=CHAT, headers=H)
        assert eng.state.get("ollama-box").cooling()
        n_gpu = len(fake.calls["gpu-box"])
        r = tc.post("/v1/chat/completions", json=CHAT, headers=H)
        assert r.status_code == 503
        assert len(fake.calls["gpu-box"]) == n_gpu + 1  # everything cooling -> tried anyway


def test_non_stream_timeout_fails_over(tmp_path):
    fake = Fake()

    async def slow(req, body):
        await asyncio.sleep(3)
        return httpx.Response(200, json=oa_resp())

    fake.on("gpu-box", slow)
    fake.on("ollama-box", lambda req, body: httpx.Response(200, json=oa_resp("ollama-box")))
    b1 = oa_block("local", "gpu-box")
    b1["timeouts"]["total"] = 0.3
    tc, _ = client_for(tmp_path, [b1, oa_block("ollama-box", "ollama-box")], fake)
    with tc:
        r = tc.post("/v1/chat/completions", json=CHAT, headers=H)
    assert r.json()["choices"][0]["message"]["content"] == "ollama-box"


def test_force_block_needs_admin(tmp_path):
    fake = Fake()
    fake.on("gpu-box", lambda req, body: httpx.Response(200, json=oa_resp("gpu-box")))
    fake.on("ollama-box", lambda req, body: httpx.Response(200, json=oa_resp("ollama-box")))
    tc, _ = client_for(tmp_path, [oa_block("local", "gpu-box"), oa_block("ollama-box", "ollama-box")], fake)
    with tc:
        assert tc.post("/v1/chat/completions", json=CHAT, headers=H | {"x-switchboard-force": "ollama-box"}).status_code == 403
        r = tc.post("/v1/chat/completions", json=CHAT,
                    headers={"authorization": f"Bearer {ADMIN}", "x-switchboard-force": "ollama-box"})
        assert r.json()["choices"][0]["message"]["content"] == "ollama-box"


def test_unknown_model_default_and_reject(tmp_path):
    fake = Fake()
    fake.on("gpu-box", lambda req, body: httpx.Response(200, json=oa_resp()))
    tc, _ = client_for(tmp_path, [oa_block("local", "gpu-box")], fake)
    with tc:
        r = tc.post("/v1/chat/completions", json=CHAT | {"model": "gpt-whatever"}, headers=H)
        assert r.status_code == 200 and r.json()["model"] == "gateway"
    tc, _ = client_for(tmp_path, [oa_block("local", "gpu-box")], fake, unknown_model="reject")
    with tc:
        assert tc.post("/v1/chat/completions", json=CHAT | {"model": "gpt-whatever"}, headers=H).status_code == 404


def test_overrides_defaults_drop_params(tmp_path):
    fake = Fake()
    fake.on("api.anthropic.com", lambda req, body: httpx.Response(200, json=an_resp()))
    blk = an_block(drop_params=["temperature", "top_p"], defaults={"output_config": {"effort": "low"}},
                   overrides={"metadata": {"user_id": "switchboard"}})
    tc, _ = client_for(tmp_path, [blk], fake)
    with tc:
        tc.post("/v1/chat/completions", json=CHAT | {"temperature": 0, "top_p": 0.5}, headers=H)
        tc.post("/v1/chat/completions", json=CHAT | {"reasoning_effort": "high"}, headers=H)
    b0, b1 = (c["body"] for c in fake.calls["api.anthropic.com"])
    assert "temperature" not in b0 and "top_p" not in b0
    assert b0["output_config"] == {"effort": "low"}
    assert b0["metadata"] == {"user_id": "switchboard"}
    assert b1["output_config"] == {"effort": "high"}  # client value beats a default


def test_spend_cap(tmp_path):
    fake = Fake()
    fake.on("api.anthropic.com", lambda req, body: httpx.Response(
        200, json=an_resp() | {"usage": {"input_tokens": 1_000_000, "output_tokens": 0}}))
    blk = an_block(pricing={"input_per_mtok": 1.0, "output_per_mtok": 5.0}, spend_cap_usd_per_day=1.5)
    tc, eng = client_for(tmp_path, [blk], fake)
    with tc:
        assert tc.post("/v1/chat/completions", json=CHAT, headers=H).status_code == 200
        assert tc.post("/v1/chat/completions", json=CHAT, headers=H).status_code == 200
        r = tc.post("/v1/chat/completions", json=CHAT, headers=H)
        assert r.status_code == 503 and "spend cap" in r.json()["error"]["message"]
    assert len(fake.calls["api.anthropic.com"]) == 2
    assert json.loads((tmp_path / "state" / "spend.json").read_text())["haiku"]["usd"] == 2.0


def test_context_skip(tmp_path):
    fake = Fake()
    fake.on("ollama-box", lambda req, body: httpx.Response(200, json=oa_resp("ollama-box")))
    tc, _ = client_for(tmp_path, [oa_block("local", "gpu-box", max_context_tokens=100), oa_block("ollama-box", "ollama-box")], fake)
    big = {"model": "gateway", "messages": [{"role": "user", "content": "x" * 2000}]}
    with tc:
        r = tc.post("/v1/chat/completions", json=big, headers=H)
    assert r.status_code == 200 and not fake.calls["gpu-box"]


# ---------------------------------------------------------------------------
# Cross-format, non-streaming
# ---------------------------------------------------------------------------


def test_anthropic_client_openai_upstream_tools(tmp_path):
    fake = Fake()
    tc_ = [{"id": "call:1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"NYC"}'}}]
    fake.on("gpu-box", lambda req, body: httpx.Response(200, json=oa_resp(None, tc_, "tool_calls")))
    tc, _ = client_for(tmp_path, [oa_block("local", "gpu-box")], fake)
    body = {
        "model": "gateway", "max_tokens": 200, "system": "be brief",
        "tools": [{"name": "get_weather", "description": "w", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}},
                  {"type": "web_search_20260209", "name": "web_search"}],
        "messages": [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm", "signature": "sig"},
                                              {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "LA"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "sunny"},
                                         {"type": "text", "text": "and NYC?"}]},
        ],
    }
    with tc:
        r = tc.post("/v1/messages", json=body, headers={"x-api-key": KEY})
    j = r.json()
    assert j["type"] == "message" and j["model"] == "gateway"
    assert j["stop_reason"] == "tool_use"
    assert j["content"] == [{"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "NYC"}}]
    sent = fake.calls["gpu-box"][0]["body"]
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert sent["messages"][2]["tool_calls"][0]["id"] == "toolu_1"
    assert sent["messages"][3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "sunny"}
    assert len(sent["tools"]) == 1  # server tool dropped
    assert "hmm" not in json.dumps(sent)  # thinking dropped


def test_openai_client_anthropic_upstream_tools(tmp_path):
    fake = Fake()
    content = [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "checking"},
               {"type": "tool_use", "id": "toolu_9", "name": "lookup", "input": {"q": "x"}}]
    fake.on("api.anthropic.com", lambda req, body: httpx.Response(200, json=an_resp(content, "tool_use")))
    tc, _ = client_for(tmp_path, [an_block()], fake)
    body = {"model": "gateway", "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
            "tool_choice": "required", "parallel_tool_calls": False,
            "response_format": {"type": "json_schema", "json_schema": {"name": "o", "schema": {"type": "object"}}},
            "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
                         {"role": "assistant", "content": None, "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
                         {"role": "tool", "tool_call_id": "call_a", "content": "r1"},
                         {"role": "user", "content": [{"type": "text", "text": "more"},
                                                      {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}
    with tc:
        r = tc.post("/v1/chat/completions", json=body, headers=H)
    j = r.json()
    assert j["choices"][0]["finish_reason"] == "tool_calls"
    msg = j["choices"][0]["message"]
    assert msg["content"] == "checking"
    assert msg["tool_calls"][0]["function"] == {"name": "lookup", "arguments": '{"q": "x"}'}
    assert j["usage"] == {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
    sent = fake.calls["api.anthropic.com"][0]["body"]
    assert sent["system"] == "sys"
    assert sent["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    # tool result and the following user text are merged into one user turn
    assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "user"]
    assert sent["messages"][2]["content"][0]["type"] == "tool_result"
    assert sent["messages"][2]["content"][2]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}


def test_gemini_non_stream(tmp_path):
    fake = Fake()
    gresp = {"candidates": [{"content": {"role": "model", "parts": [{"text": "thinking...", "thought": True},
                                                                    {"text": "gemini says hi"}]},
                             "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 4}}
    fake.on("generativelanguage.googleapis.com", lambda req, body: httpx.Response(200, json=gresp))
    tc, _ = client_for(tmp_path, [gem_block(default_max_tokens=99)], fake)
    with tc:
        r = tc.post("/v1/chat/completions", json=CHAT | {"messages": [{"role": "system", "content": "s"},
                                                                        {"role": "user", "content": "hi"}]}, headers=H)
    assert r.json()["choices"][0]["message"]["content"] == "gemini says hi"
    call = fake.calls["generativelanguage.googleapis.com"][0]
    assert call["url"].endswith("/v1beta/models/gemini-x:generateContent")
    assert call["headers"]["x-goog-api-key"] == "AIza-x"
    assert call["body"]["systemInstruction"] == {"parts": [{"text": "s"}]}
    assert call["body"]["generationConfig"]["maxOutputTokens"] == 99


def test_gemini_request_tools_roundtrip():
    canon = {"messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}],
        "tools": [{"name": "f", "input_schema": {"type": "object"}}], "tool_choice": {"type": "tool", "name": "f"}}
    g = tr.anthropic_to_gemini_request(canon)
    assert g["contents"][1]["parts"][0]["functionCall"] == {"name": "f", "args": {"a": 1}}
    assert g["contents"][2]["parts"][0]["functionResponse"] == {"name": "f", "response": {"content": "ok"}}
    assert g["toolConfig"]["functionCallingConfig"] == {"mode": "ANY", "allowedFunctionNames": ["f"]}


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def _chunks(text_parts, finish="stop", usage=True):
    out = [{"id": "c", "object": "chat.completion.chunk", "model": "u", "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}]
    for t in text_parts:
        out.append({"id": "c", "object": "chat.completion.chunk", "model": "u", "choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}]})
    out.append({"id": "c", "object": "chat.completion.chunk", "model": "u", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    if usage:
        out.append({"id": "c", "object": "chat.completion.chunk", "model": "u", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    return out


def _read_sse(resp):
    data = []
    for line in resp.iter_lines():
        if line.startswith("data: "):
            data.append(line[6:])
    return data


def test_stream_passthrough_failover_on_first_token_timeout(tmp_path):
    fake = Fake()

    async def hang(req, body):
        async def gen():
            await asyncio.sleep(5)
            yield b"data: {}\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())

    fake.on("gpu-box", hang)
    fake.on("ollama-box", lambda req, body: sse_stream(oa_sse(_chunks(["Hel", "lo"]))))
    tc, eng = client_for(tmp_path, [oa_block("local", "gpu-box"), oa_block("ollama-box", "ollama-box")], fake)
    with tc:
        with tc.stream("POST", "/v1/chat/completions", json=CHAT | {"stream": True}, headers=H) as r:
            data = _read_sse(r)
    assert data[-1] == "[DONE]"
    chunks = [json.loads(d) for d in data[:-1]]
    assert all(c["model"] == "gateway" for c in chunks)
    assert "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks) == "Hello"
    assert all(c["choices"] for c in chunks)  # usage-only chunk stripped (client didn't ask)
    assert fake.calls["ollama-box"][0]["body"]["stream_options"] == {"include_usage": True}
    assert "first_token" in eng.state.get("local").last_error


def test_stream_usage_kept_when_requested(tmp_path):
    fake = Fake()
    fake.on("gpu-box", lambda req, body: sse_stream(oa_sse(_chunks(["a"]))))
    tc, _ = client_for(tmp_path, [oa_block("local", "gpu-box")], fake)
    with tc:
        with tc.stream("POST", "/v1/chat/completions",
                       json=CHAT | {"stream": True, "stream_options": {"include_usage": True}}, headers=H) as r:
            data = _read_sse(r)
    assert json.loads(data[-2])["usage"]["prompt_tokens"] == 5


def test_stream_anthropic_client_from_openai_upstream_with_tools(tmp_path):
    fake = Fake()
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "thinking"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "Let me check."}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_x", "type": "function", "function": {"name": "f", "arguments": ""}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"a":'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '1}'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 6}},
    ]
    fake.on("gpu-box", lambda req, body: sse_stream(oa_sse(chunks)))
    tc, _ = client_for(tmp_path, [oa_block("local", "gpu-box")], fake)
    body = {"model": "gateway", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "x"}]}
    with tc:
        with tc.stream("POST", "/v1/messages", json=body, headers={"x-api-key": KEY}) as r:
            events = []
            name = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    events.append((name, json.loads(line[6:])))
    names = [n for n, _ in events]
    assert names == ["message_start", "content_block_start", "content_block_delta", "content_block_stop",
                     "content_block_start", "content_block_delta", "content_block_delta", "content_block_stop",
                     "message_delta", "message_stop"]
    assert events[0][1]["message"]["model"] == "gateway"
    assert events[4][1]["content_block"] == {"type": "tool_use", "id": "call_x", "name": "f", "input": {}}
    args = "".join(d["delta"]["partial_json"] for n, d in events if d.get("delta", {}).get("type") == "input_json_delta")
    assert json.loads(args) == {"a": 1}
    assert events[-2][1]["delta"]["stop_reason"] == "tool_use"
    assert events[-2][1]["usage"]["output_tokens"] == 6


def test_stream_openai_client_from_anthropic_upstream(tmp_path):
    fake = Fake()
    evs = [
        ("message_start", {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": [], "usage": {"input_tokens": 9, "output_tokens": 1}}}),
        ("ping", {"type": "ping"}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hi "}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "there"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("content_block_start", {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"k":2}'}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 2}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    fake.on("api.anthropic.com", lambda req, body: sse_stream(an_sse(evs)))
    tc, eng = client_for(tmp_path, [an_block(pricing={"input_per_mtok": 0.1, "output_per_mtok": 0.5})], fake)
    with tc:
        with tc.stream("POST", "/v1/chat/completions",
                       json=CHAT | {"stream": True, "stream_options": {"include_usage": True}}, headers=H) as r:
            data = _read_sse(r)
    assert data[-1] == "[DONE]" and data.count("[DONE]") == 1
    chunks = [json.loads(d) for d in data[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
    assert text == "Hi there"
    tcs = [c["choices"][0]["delta"]["tool_calls"][0] for c in chunks if c["choices"] and c["choices"][0]["delta"].get("tool_calls")]
    assert tcs[0]["id"] == "toolu_1" and tcs[0]["function"]["name"] == "f"
    assert "".join(t["function"].get("arguments", "") for t in tcs) == '{"k":2}'
    finals = [c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]]
    assert finals[0]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"] == {"prompt_tokens": 9, "completion_tokens": 12, "total_tokens": 21}
    entry = json.loads((tmp_path / "state" / "requests.jsonl").read_text().splitlines()[-1])
    assert entry["tokens_in"] == 9 and entry["tokens_out"] == 12 and entry["cost_usd"] > 0
    assert "ttft_ms" in entry


def test_stream_anthropic_overloaded_event_fails_over(tmp_path):
    fake = Fake()
    evs = [("message_start", {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": "x", "content": [], "usage": {"input_tokens": 1}}}),
           ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})]
    fake.on("api.anthropic.com", lambda req, body: sse_stream(an_sse(evs)))
    fake.on("ollama-box", lambda req, body: sse_stream(oa_sse(_chunks(["backup"]))))
    tc, _ = client_for(tmp_path, [an_block(), oa_block("ollama-box", "ollama-box")], fake)
    body = {"model": "gateway", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "x"}]}
    with tc:
        with tc.stream("POST", "/v1/messages", json=body, headers={"x-api-key": KEY}) as r:
            raw = "".join(r.iter_text())
    assert "backup" in raw and "Overloaded" not in raw
    assert raw.count("event: message_start") == 1  # haiku's buffered message_start was discarded


def test_stream_mid_stream_stall_reports_error(tmp_path):
    fake = Fake()

    def stalling(req, body):
        async def gen():
            yield f"data: {json.dumps(_chunks(['first'])[1])}\n\n".encode()
            await asyncio.sleep(5)
            yield b"data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())

    fake.on("gpu-box", stalling)
    blk = oa_block("local", "gpu-box")
    blk["timeouts"]["idle"] = 0.3
    tc, _ = client_for(tmp_path, [blk], fake)
    with tc:
        with tc.stream("POST", "/v1/chat/completions", json=CHAT | {"stream": True}, headers=H) as r:
            data = _read_sse(r)
    assert json.loads(data[0])["choices"][0]["delta"]["content"] == "first"
    assert "stalled" in json.loads(data[1])["error"]["message"]
    assert data[-1] == "[DONE]"
    entry = json.loads((tmp_path / "state" / "requests.jsonl").read_text().splitlines()[-1])
    assert entry["status"] == 504


def test_stream_gemini_to_anthropic(tmp_path):
    fake = Fake()
    g = [{"candidates": [{"content": {"parts": [{"text": "Hel"}]}}]},
         {"candidates": [{"content": {"parts": [{"text": "lo"}, {"functionCall": {"name": "f", "args": {"z": 1}}}]},
                          "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4}}]
    fake.on("generativelanguage.googleapis.com", lambda req, body: sse_stream([f"data: {json.dumps(c)}\r\n\r\n" for c in g]))
    tc, _ = client_for(tmp_path, [gem_block()], fake)
    body = {"model": "gateway", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "x"}]}
    with tc:
        with tc.stream("POST", "/v1/messages", json=body, headers={"x-api-key": KEY}) as r:
            raw = "".join(r.iter_text())
    assert fake.calls["generativelanguage.googleapis.com"][0]["url"].endswith(":streamGenerateContent?alt=sse")
    assert '"text":"Hel"' in raw and '"text":"lo"' in raw
    assert '"stop_reason":"tool_use"' in raw and '"partial_json":"{\\"z\\": 1}"' in raw


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_env_and_validation(tmp_path, monkeypatch):
    p = tmp_path / "c.yml"
    p.write_text("""
clients: [{name: a, key: "0123456789abcdef0"}]
routes:
  - name: r
    blocks:
      - {id: x, provider: openai, base_url: "http://h/v1/", api_key: "${SB_TEST_KEY}", model: m}
""")
    monkeypatch.setenv("SB_TEST_KEY", "secret")
    cfg = load_config(p)
    assert cfg.routes[0].blocks[0].api_key == "secret"
    assert cfg.routes[0].blocks[0].base_url == "http://h/v1"
    monkeypatch.delenv("SB_TEST_KEY")
    with pytest.raises(ConfigError, match="SB_TEST_KEY"):
        load_config(p)
    p.write_text("""
clients: [{name: a, key: "0123456789abcdef0"}]
routes:
  - name: r
    blocks:
      - {id: x, provider: openai, base_url: "http://h/v1", model: m}
      - {id: x, provider: openai, base_url: "http://h/v1", model: m, typo_field: 1}
""")
    with pytest.raises(ConfigError):
        load_config(p)


def test_example_config_is_valid(monkeypatch):
    from pathlib import Path
    for v in ("SWITCHBOARD_ADMIN_KEY", "SWITCHBOARD_MY_APP_KEY", "LOCAL_GPU_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(v, f"{v.lower()}-test-value")
    ex = Path(__file__).parent.parent / "config" / "switchboard.example.yml"
    cfg = load_config(ex)
    assert [b.id for b in cfg.routes[0].blocks] == ["local-gpu", "ollama", "claude-haiku"]


# ---------------------------------------------------------------------------
# Context window auto-detection
# ---------------------------------------------------------------------------


def _detect(tmp_path, blocks, gets):
    fake = Fake()
    fake.gets.update(gets)
    eng = Engine(make_cfg(tmp_path, blocks), transport=httpx.MockTransport(fake))

    async def run():
        await eng.refresh_contexts()
        await eng.close()

    asyncio.run(run())
    return eng


def test_detect_context_each_source(tmp_path):
    gets = {
        # OpenAI-compatible server that lists the window in its models list
        ("gpu-box", "/v1/models"): {"data": [{"id": "other", "context_length": 1},
                                             {"id": "local-model", "context_length": 262144, "meta": {"n_ctx": 262144}}]},
        # Ollama: models list has no context, /api/ps does (only while loaded)
        ("ollama-box", "/v1/models"): {"data": [{"id": "ollama-box-model", "object": "model"}]},
        ("ollama-box", "/api/ps"): {"models": [{"name": "ollama-box-model", "model": "ollama-box-model", "context_length": 131072}]},
        # plain llama.cpp server
        ("llama", "/props"): {"default_generation_settings": {"n_ctx": 65536}},
        ("api.anthropic.com", "/v1/models/claude-haiku-5-5"): {"id": "claude-haiku-5-5", "max_input_tokens": 1000000},
        ("generativelanguage.googleapis.com", "/v1beta/models/gemini-x"): {"name": "models/gemini-x", "inputTokenLimit": 2000000},
    }
    blocks = [oa_block("local", "gpu-box"), oa_block("ollama-box", "ollama-box"), oa_block("llama", "llama"),
              an_block(), gem_block(), oa_block("silent", "silent", context_fallback=32768),
              oa_block("pinned", "gpu-box", max_context_tokens=4096), oa_block("unknown", "nowhere")]
    eng = _detect(tmp_path, blocks, gets)
    got = {b.id: (eng.state.effective_context(b), eng.state.context_source(b)) for b in eng.cfg.routes[0].blocks}
    assert got == {
        "local": (262144, "models endpoint"),
        "ollama-box": (131072, "ollama /api/ps"),
        "llama": (65536, "llama.cpp /props"),
        "haiku": (1000000, "anthropic models API"),
        "gem": (2000000, "gemini models API"),
        "silent": (32768, "context_fallback"),
        "pinned": (4096, "config"),
        "unknown": (None, None),
    }
    route = eng.cfg.routes[0]
    route.context_tokens = "auto"
    assert eng.route_context(route) == 4096  # smallest known window
    route.context_tokens = "max"
    assert eng.route_context(route) == 2000000
    route.context_tokens = 777
    assert eng.route_context(route) == 777


def test_detected_context_kept_when_ollama_unloads(tmp_path):
    fake = Fake()
    fake.gets[("ollama-box", "/api/ps")] = {"models": [{"name": "ollama-box-model", "context_length": 131072}]}
    eng = Engine(make_cfg(tmp_path, [oa_block("ollama-box", "ollama-box")]), transport=httpx.MockTransport(fake))

    async def run():
        await eng.refresh_contexts()
        fake.gets[("ollama-box", "/api/ps")] = {"models": []}  # model unloaded
        await eng.refresh_contexts()
        await eng.close()

    asyncio.run(run())
    b = eng.cfg.routes[0].blocks[0]
    assert eng.state.effective_context(b) == 131072


def test_context_advertised_and_used_for_skipping(tmp_path):
    fake = Fake()
    fake.gets[("gpu-box", "/v1/models")] = {"data": [{"id": "local-model", "context_length": 300}]}
    fake.gets[("ollama-box", "/api/ps")] = {"models": [{"name": "ollama-box-model", "context_length": 500000}]}
    fake.on("ollama-box", lambda req, body: httpx.Response(200, json=oa_resp("backup")))
    cfg = make_cfg(tmp_path, [oa_block("local", "gpu-box"), oa_block("ollama-box", "ollama-box")])
    cfg.routes[0].context_tokens = "auto"
    eng = Engine(cfg, transport=httpx.MockTransport(fake))
    with TestClient(create_app(eng)) as tc:
        asyncio.run(eng.refresh_contexts())  # lifespan also runs it; make sure it finished
        m = tc.get("/v1/models", headers=H).json()["data"][0]
        assert m["context_length"] == m["max_input_tokens"] == m["max_model_len"] == m["meta"]["n_ctx"] == 300
        assert tc.get("/health").json()["max_context"] == 300
        assert tc.get("/props", headers=H).json()["default_generation_settings"]["n_ctx"] == 300
        assert tc.get("/props").status_code == 401
        st = tc.get("/status", headers=H).json()["routes"][0]
        assert st["context_tokens"] == 300
        assert st["blocks"][0]["context_source"] == "models endpoint"
        # ~500-token prompt: too big for the first block's detected 300, fits the second
        r = tc.post("/v1/chat/completions", headers=H,
                    json={"model": "gateway", "messages": [{"role": "user", "content": "x" * 2000}]})
        assert r.json()["choices"][0]["message"]["content"] == "backup"
        assert not fake.calls["gpu-box"]


# ---------------------------------------------------------------------------
# Prompt capture
# ---------------------------------------------------------------------------

from switchboard.capture import Reader, render  # noqa: E402


def _cap_client(tmp_path, blocks, fake, capture=True):
    cfg = make_cfg(tmp_path, blocks)
    cfg.clients[0].capture = capture
    eng = Engine(cfg, transport=httpx.MockTransport(fake))
    return TestClient(create_app(eng)), eng


def _log_lines(tmp_path):
    return [json.loads(x) for x in (tmp_path / "state" / "requests.jsonl").read_text().splitlines()]


def test_capture_off_by_default_and_admin_header(tmp_path):
    fake = Fake()
    fake.on("gpu-box", lambda req, body: httpx.Response(200, json=oa_resp()))
    tc, eng = _cap_client(tmp_path, [oa_block("local", "gpu-box")], fake, capture=False)
    with tc:
        assert tc.post("/v1/chat/completions", json=CHAT, headers=H).status_code == 200
        r = tc.post("/v1/chat/completions", json=CHAT, headers=H | {"x-switchboard-capture": "1"})
        assert r.status_code == 403
        r = tc.post("/v1/chat/completions", json=CHAT,
                    headers={"authorization": f"Bearer {ADMIN}", "x-switchboard-capture": "1"})
        assert r.status_code == 200
        eng.captures.flush()
    lines = _log_lines(tmp_path)
    assert "capture" not in lines[0] and lines[-1]["capture"]
    assert [s["client"] for s in Reader(tmp_path / "state" / "captures").summaries()] == ["admin"]


def test_capture_failover_turns_are_deduplicated(tmp_path):
    fake = Fake()
    fake.on("gpu-box", lambda req, body: httpx.Response(503, text="down"))
    fake.on("api.anthropic.com", lambda req, body: httpx.Response(200, json=an_resp()))
    tc, eng = _cap_client(tmp_path, [oa_block("local", "gpu-box", cooldown={"after_failures": 99}), an_block()], fake)
    system = "You are a careful assistant. " * 40  # big enough to be deduplicated
    tools = [{"type": "function", "function": {"name": f"tool{i}", "description": "x" * 100,
                                                "parameters": {"type": "object"}}} for i in range(5)]
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": "first question"}]
    refs = []
    with tc:
        for turn in range(3):
            sent = msgs
            r = tc.post("/v1/chat/completions", json={"model": "gateway", "messages": sent, "tools": tools}, headers=H)
            assert r.status_code == 200
            msgs = msgs + [r.json()["choices"][0]["message"], {"role": "user", "content": f"follow-up {turn}"}]
        eng.captures.flush()
        refs = [x["capture"] for x in _log_lines(tmp_path)]
    root = tmp_path / "state" / "captures"
    reader = Reader(root)
    raw3 = reader.load(refs[2])
    # Turn 3 stores only its new messages and points at turn 2 for the rest and for the tools.
    assert raw3["request"]["$base"]["ref"] == refs[1]
    assert raw3["request"]["$base"]["messages"] == 4 and "tools" in raw3["request"]["$base"]["same"]
    assert len(raw3["request"]["messages"]) == 2 and "tools" not in raw3["request"]
    # ...and the full request comes back on read.
    full = reader.resolve(refs[2].split("/")[1])
    assert full["request"]["messages"] == sent
    assert full["request"]["messages"][0]["content"] == system and full["request"]["tools"] == tools
    # Both attempts carry what that block was actually sent, translated for Anthropic.
    local, haiku = full["attempts"]
    assert local["block"] == "local" and "503" in local["error"] and local["body"]["model"] == "local-model"
    assert haiku["ok"] and haiku["body"]["system"] == system and haiku["body"]["model"] == "claude-haiku-5-5"
    assert len(haiku["body"]["messages"]) == 5
    assert haiku["response"]["content"][0]["text"] == "from haiku"  # raw upstream answer
    assert full["response"]["choices"][0]["message"]["content"] == "from haiku"  # what the client got
    text = render(full)
    assert "=== tools (5) ===" in text and "follow-up 1" in text and "from haiku" in text
    # Files are private to the service user.
    f = next(root.glob("*/*.json.gz"))
    assert oct(f.stat().st_mode & 0o777) == "0o600"


def test_capture_stream_reassembled(tmp_path):
    fake = Fake()
    fake.on("gpu-box", lambda req, body: sse_stream(oa_sse(_chunks(["Hel", "lo"]))))
    tc, eng = _cap_client(tmp_path, [oa_block("local", "gpu-box")], fake)
    body = {"model": "gateway", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "x"}]}
    with tc:
        with tc.stream("POST", "/v1/messages", json=body, headers={"x-api-key": KEY}) as r:
            list(r.iter_lines())
        eng.captures.flush()
    ref = _log_lines(tmp_path)[0]["capture"]
    doc = Reader(tmp_path / "state" / "captures").resolve(ref)
    assert doc["attempts"][0]["response"]["choices"][0]["message"]["content"] == "Hello"
    assert doc["response"]["content"][0]["text"] == "Hello" and doc["response"]["model"] == "gateway"
    assert doc["attempts"][0]["body"]["messages"] == [{"role": "user", "content": "x"}]


def test_capture_prune_and_missing_base(tmp_path):
    from switchboard.capture import CaptureStore

    root = tmp_path / "caps"
    for day in ("2000-01-01", "2999-01-01"):
        (root / day).mkdir(parents=True)
        (root / day / "a.json.gz").write_bytes(b"x" * 10)
    store = CaptureStore(root, lambda: None)
    store.prune(retention_days=7, max_mb=1)
    assert not (root / "2000-01-01").exists() and (root / "2999-01-01").exists()
    # A capture whose base was deleted still reads, with a marker for the missing part.
    import gzip as _gz
    doc = {"ref": "2999-01-01/b", "attempts": [], "request": {
        "messages": [{"role": "user", "content": "new"}],
        "$base": {"ref": "2000-01-01/a", "slot": "request", "key": "messages", "messages": 3, "same": []}}}
    (root / "2999-01-01" / "b.json.gz").write_bytes(_gz.compress(json.dumps(doc).encode()))
    full = Reader(root).resolve("b")
    assert "3 earlier item(s)" in full["request"]["messages"][0]["content"]
    assert full["request"]["messages"][1]["content"] == "new"
    store.close()


# ---------------------------------------------------------------------------
# Client hang-ups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [True, False])
def test_client_disconnect_before_output_cancels_upstream(tmp_path, stream):
    """A client that hangs up while the upstream is still reading the prompt must
    stop the upstream request and must not fail over to the next block."""
    fake = Fake()
    closed = asyncio.Event()

    async def slow(req, body):
        async def gen():
            try:
                await asyncio.sleep(5)  # "reading the prompt"
                yield b"data: {}\n\n"
            finally:
                closed.set()
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())
        try:
            await asyncio.sleep(5)
        finally:
            closed.set()
        return httpx.Response(200, json=oa_resp())

    fake.on("gpu-box", slow)
    fake.on("ollama-box", lambda req, body: httpx.Response(200, json=oa_resp("should not be called")))
    blocks = [oa_block("local", "gpu-box", timeouts={"connect": 1, "first_token": 10, "idle": 10, "total": 10}),
              oa_block("ollama-box", "ollama-box")]
    eng = Engine(make_cfg(tmp_path, blocks), transport=httpx.MockTransport(fake))
    app = create_app(eng)
    payload = json.dumps(CHAT | {"stream": stream}).encode()
    sent = []

    async def run():
        msgs = [{"type": "http.request", "body": payload, "more_body": False}]

        async def receive():
            if msgs:
                return msgs.pop(0)
            await asyncio.sleep(0.3)  # the client hangs up shortly after sending
            return {"type": "http.disconnect"}

        async def send(m):
            sent.append(m)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
                 "query_string": b"", "root_path": "", "server": ("test", 80), "client": ("test", 1),
                 "headers": [(b"authorization", f"Bearer {KEY}".encode()), (b"content-type", b"application/json")]}
        t0 = asyncio.get_running_loop().time()
        await asyncio.wait_for(app(scope, receive, send), timeout=4)
        elapsed = asyncio.get_running_loop().time() - t0
        await asyncio.wait_for(closed.wait(), timeout=1)
        return elapsed

    elapsed = asyncio.run(run())
    assert elapsed < 2  # did not wait for the upstream
    assert not fake.calls["ollama-box"]  # no failover for a client that left
    assert eng.state.get("local").failures == 0  # the upstream did nothing wrong
    line = json.loads((tmp_path / "state" / "requests.jsonl").read_text().splitlines()[-1])
    assert line["status"] == 499 and line["attempts"][0]["error"] == "client disconnected"


# ---------------------------------------------------------------------------
# Inspector page
# ---------------------------------------------------------------------------

AH = {"authorization": f"Bearer {ADMIN}"}


def _insp_client(tmp_path, fake, enabled=True, capture=False):
    cfg = make_cfg(tmp_path, [oa_block("local", "gpu-box")], inspector={"enabled": enabled})
    cfg.clients[0].capture = capture
    eng = Engine(cfg, transport=httpx.MockTransport(fake))
    return TestClient(create_app(eng)), eng


def test_inspector_off_by_default_and_admin_only(tmp_path):
    fake = Fake()
    tc, eng = _insp_client(tmp_path, fake, enabled=False)
    with tc:
        for path in ("/inspector", "/inspector/app.js", "/inspector/api/feed"):
            assert tc.get(path, headers=AH).status_code == 404
        eng.cfg.server.inspector.enabled = True  # what a config reload does
        page = tc.get("/inspector")
        assert page.status_code == 200 and "Switchboard Inspector" in page.text
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert tc.get("/inspector/app.js").status_code == 200
        assert tc.get("/inspector/../app.py").status_code == 404
        assert tc.get("/inspector/api/feed").status_code == 401
        assert tc.get("/inspector/api/feed", headers=H).status_code == 403
        assert tc.get("/inspector/api/feed", headers=AH).status_code == 200


def test_inspector_feed_and_uncaptured_detail(tmp_path):
    fake = Fake()
    fake.on("gpu-box", lambda req, body: httpx.Response(200, json=oa_resp()))
    tc, eng = _insp_client(tmp_path, fake)
    with tc:
        tc.post("/v1/chat/completions", json=CHAT, headers=H)
        feed = tc.get("/inspector/api/feed", headers=AH).json()
        assert [e["served_by"] for e in feed["entries"]] == ["local"] and feed["active"] == []
        rid = feed["entries"][0]["id"]
        assert tc.get(f"/inspector/api/feed?after={feed['seq']}", headers=AH).json()["entries"] == []
        d = tc.get(f"/inspector/api/request/{rid}", headers=AH).json()
        assert d["state"] == "done" and "anatomy" not in d  # not captured: no prompt contents
        assert tc.get(f"/inspector/api/request/{rid}/raw", headers=AH).status_code == 404
    # A restart refills the list from requests.jsonl.
    eng2 = Engine(make_cfg(tmp_path, [oa_block("local", "gpu-box")], inspector={"enabled": True}))
    assert [e["id"] for e in eng2.recent] == [rid]


def test_inspector_capture_switches(tmp_path):
    fake = Fake()
    fake.on("gpu-box", lambda req, body: httpx.Response(200, json=oa_resp()))
    tc, eng = _insp_client(tmp_path, fake)
    with tc:
        assert tc.post("/inspector/api/capture", json={"client": "tester", "on": True}, headers=H).status_code == 403
        assert tc.post("/inspector/api/capture", json={"client": "nobody", "on": True}, headers=AH).status_code == 404
        assert tc.post("/inspector/api/capture", json={"client": None, "on": None}, headers=AH).status_code == 400
        r = tc.post("/inspector/api/capture", json={"client": "tester", "on": True}, headers=AH)
        assert r.json()["clients"] == {"tester": True, "admin": False}
        tc.post("/v1/chat/completions", json=CHAT, headers=H)
        tc.post("/inspector/api/capture", json={"client": "tester", "on": None}, headers=AH)  # back to config (off)
        tc.post("/v1/chat/completions", json=CHAT, headers=H)
        tc.post("/inspector/api/capture", json={"client": None, "on": True}, headers=AH)  # everything
        tc.post("/v1/chat/completions", json=CHAT, headers=H)
        feed = tc.get("/inspector/api/feed", headers=AH).json()
    assert [bool(e.get("capture")) for e in feed["entries"]] == [True, False, True]
    assert feed["capture_all"] is True and all(c["capture"] for c in feed["clients"])
    # The switches are saved and survive a restart.
    eng2 = Engine(make_cfg(tmp_path, [oa_block("local", "gpu-box")]))
    assert eng2.capture_overrides == {"all": True, "clients": {}}


def test_inspector_breakdown_of_a_captured_request(tmp_path):
    fake = Fake()
    fake.on("gpu-box", lambda req, body: httpx.Response(200, json=oa_resp("the answer")))
    tc, eng = _insp_client(tmp_path, fake, capture=True)
    doc = "line of the document\n" * 40
    tools = ([{"type": "function", "function": {"name": f"tick_t{i}", "description": "d" * 50,
                                                "parameters": {"type": "object", "$defs": {"X": {"type": "string"}}}}}
              for i in range(5)]
             + [{"type": "function", "function": {"name": "list_files", "parameters": {"type": "object"}}}])
    body = {"model": "gateway", "tools": tools, "messages": [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": 'Cite only when the <source> tag has an id (e.g. <source id="1">).\n'
                                    f'<context><source id="1" name="notes.md">{doc}</source></context> new question'},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
    ]}
    with tc:
        tc.post("/v1/chat/completions", json=body, headers=H)
        rid = eng.recent[-1]["id"]
        d = tc.get(f"/inspector/api/request/{rid}", headers=AH).json()
        assert tc.get(f"/inspector/api/request/{rid}/raw?block=local", headers=AH).json()["model"] == "local-model"
        raw = f"/inspector/api/request/{rid}/raw"
        assert tc.get(f"{raw}?part=request", headers=AH).json()["messages"] == body["messages"]
        assert tc.get(f"{raw}?part=response", headers=AH).json()["choices"][0]["message"]["content"] == "the answer"
        assert tc.get(f"{raw}?block=local&part=response", headers=AH).status_code == 404  # same format: not stored twice
        assert tc.get(raw, headers=AH).json()["ref"]
    an = d["anatomy"]
    seg = {s["key"]: s for s in an["segments"]}
    assert {"system", "tools", "documents", "history", "latest", "turn"} <= set(seg)
    assert sum(s["tokens"] for s in an["segments"]) == pytest.approx(10, abs=3)  # shares the reported 10 tokens
    assert [d["name"] for d in an["documents"]] == ["notes.md"]
    assert len(doc) < an["documents"][0]["chars"] < len(doc) + 60
    assert [m["category"] for m in an["messages"]] == ["system", "history", "history", "latest", "turn", "turn"]
    assert an["messages"][4]["tool_calls"][0]["name"] == "list_files"
    groups = {g["name"]: g for g in an["tool_groups"]}
    assert groups["tick"]["count"] == 5 and groups["Other tools"]["count"] == 1
    assert groups["tick"]["tools"][0]["defs_chars"] > 0
    assert d["response"]["text"] == "the answer" and d["upstream_blocks"] == ["local"]
    assert d["has_response"] and d["upstream_responses"] == []


def test_inspector_shows_requests_in_flight(tmp_path):
    fake = Fake()
    gate = asyncio.Event()

    async def slow(req, body):
        await gate.wait()
        return httpx.Response(200, json=oa_resp())

    fake.on("gpu-box", slow)
    eng = Engine(make_cfg(tmp_path, [oa_block("local", "gpu-box")], inspector={"enabled": True}),
                 transport=httpx.MockTransport(fake))
    eng.cfg.clients[0].capture = True

    async def run():
        task = asyncio.create_task(eng.handle("openai", CHAT, "tester", None, {}, capture=True))
        await asyncio.sleep(0.05)
        live = list(eng.active.values())
        gate.set()
        await task
        return live

    live = asyncio.run(run())
    assert len(live) == 1 and live[0]["block"] == "local" and live[0]["phase"] == "waiting"
    assert "_body" in live[0] and eng.active == {}


def test_request_log_retention(tmp_path):
    from datetime import datetime, timedelta

    from switchboard.state import StateStore

    st = StateStore(tmp_path / "state")
    path = tmp_path / "state" / "requests.jsonl"

    def line(days_ago, rid):
        ts = (datetime.now().astimezone() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S%z")
        return json.dumps({"ts": ts, "id": rid}) + "\n"

    path.write_text(line(10, "a") + line(8, "b") + line(6, "c") + line(0.1, "d"))
    assert st.prune_requests(7) == 2
    assert [json.loads(x)["id"] for x in path.read_text().splitlines()] == ["c", "d"]
    # Pruning is hourly from log_request; a new line is appended without rewriting.
    st.log_request({"ts": "x", "id": "e"}, 7)
    assert [json.loads(x)["id"] for x in path.read_text().splitlines()] == ["c", "d", "e"]
    # The engine trims on startup using server.request_log.retention_days.
    path.write_text(line(3, "f") + line(1, "g"))
    Engine(make_cfg(tmp_path, [oa_block("local", "gpu-box")], request_log={"retention_days": 2}))
    assert [json.loads(x)["id"] for x in path.read_text().splitlines()] == ["g"]


def test_inspector_response_parts_and_timeline(tmp_path):
    def chunk(delta, finish=None):
        return {"id": "c", "object": "chat.completion.chunk", "model": "u",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    stream = [chunk({"role": "assistant"}),
              chunk({"reasoning_content": "Let me think. " * 20}),
              chunk({"reasoning_content": "Done thinking."}),
              chunk({"tool_calls": [{"index": 0, "id": "t1", "type": "function",
                                     "function": {"name": "get_weather", "arguments": ""}}]}),
              chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city": "Raleigh"}'}}]}),
              chunk({}, "tool_calls"),
              {"id": "c", "object": "chat.completion.chunk", "model": "u", "choices": [],
               "usage": {"prompt_tokens": 5, "completion_tokens": 100,
                         "completion_tokens_details": {"reasoning_tokens": 80}}}]
    fake = Fake()
    fake.on("gpu-box", lambda req, body: sse_stream(oa_sse(stream), delay=0.01))
    tc, eng = _insp_client(tmp_path, fake, capture=True)
    body = {"model": "gateway", "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "weather?"}]}
    with tc:
        with tc.stream("POST", "/v1/chat/completions", json=body, headers=H) as r:
            "".join(r.iter_text())
        d = tc.get(f"/inspector/api/request/{eng.recent[-1]['id']}", headers=AH).json()
    r = d["response"]
    assert r["tool_calls"][0]["name"] == "get_weather" and r["reasoning"].endswith("Done thinking.")
    parts = {p["key"]: p["tokens"] for p in r["parts"]}
    assert r["reasoning_measured"] and parts["thinking"] == 80 and parts["tool"] == 20 and "answer" not in parts
    tl = d["output_timeline"]
    assert [s["kind"] for s in tl] == ["thinking", "tool"]  # consecutive chunks of one kind merge
    assert tl[0]["chars"] == len(r["reasoning"]) and tl[1]["start_ms"] >= tl[0]["end_ms"]
    assert not r["text_tool_call"]


def test_response_view_flags_tool_calls_written_as_text():
    from switchboard.anatomy import response_view

    resp = {"choices": [{"message": {"content": "<tool_call>\n<function=search_memories>\n<parameter=query>\nx\n"
                                                "</parameter>\n</function>\n</tool_call>"}, "finish_reason": "stop"}]}
    v = response_view(resp, 40)
    assert v["text_tool_call"] and v["tool_calls"] == []
    assert v["parts"] == [{"key": "answer", "label": "Answer", "chars": len(resp["choices"][0]["message"]["content"]),
                           "tokens": 40}]
    assert not response_view({"choices": [{"message": {"content": "plain"}}]})["text_tool_call"]
