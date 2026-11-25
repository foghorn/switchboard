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
