"""Live smoke test of a running Switchboard, using the official OpenAI and
Anthropic Python SDKs as clients.

For each block it forces that block (X-Switchboard-Force, admin key needed) and
checks plain and streaming chat, a two-turn tool call and JSON-schema output in
the OpenAI format, plus plain and streaming tool use in the Anthropic format.
These calls reach your real upstreams and cost whatever they cost.

    pip install openai anthropic
    SWITCHBOARD_URL=http://localhost:8000 SWITCHBOARD_ADMIN_KEY=... \
        python examples/smoke_test.py <model> <block-id> [<block-id> ...]
"""
import json
import os
import sys
import time

import anthropic
import openai

if len(sys.argv) < 3:
    sys.exit(__doc__)
BASE = os.environ.get("SWITCHBOARD_URL", "http://localhost:8000").rstrip("/")
ADMIN = os.environ["SWITCHBOARD_ADMIN_KEY"]
MODEL = sys.argv[1]
BLOCKS = sys.argv[2:]

TOOLS_OA = [{"type": "function", "function": {
    "name": "get_weather", "description": "Get current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
TOOLS_AN = [{"name": "get_weather", "description": "Get current weather for a city",
             "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]

results = []


def check(name, fn):
    t = time.time()
    try:
        detail = fn()
        results.append((name, "PASS", f"{time.time() - t:5.1f}s  {detail}"))
    except Exception as e:  # noqa: BLE001
        results.append((name, "FAIL", f"{time.time() - t:5.1f}s  {type(e).__name__}: {str(e)[:300]}"))
    print(f"{results[-1][1]}  {name:<44} {results[-1][2]}", flush=True)


for block in BLOCKS:
    hdr = {"x-switchboard-force": block}
    oa = openai.OpenAI(base_url=f"{BASE}/v1", api_key=ADMIN, default_headers=hdr, timeout=600, max_retries=0)
    an = anthropic.Anthropic(base_url=BASE, api_key=ADMIN, default_headers=hdr, timeout=600, max_retries=0)

    def oa_plain():
        r = oa.chat.completions.with_raw_response.create(model=MODEL, max_tokens=4000, temperature=0.2, messages=[
            {"role": "system", "content": "Answer in one short sentence."},
            {"role": "user", "content": "What is the capital of France?"}])
        c = r.parse()
        assert c.model == MODEL, c.model
        text = c.choices[0].message.content or ""
        assert "paris" in text.lower(), text
        return f"{text.strip()[:60]!r} usage={c.usage.prompt_tokens}/{c.usage.completion_tokens}"

    def oa_stream():
        s = oa.chat.completions.create(model=MODEL, max_tokens=4000, stream=True,
                                       stream_options={"include_usage": True},
                                       messages=[{"role": "user", "content": "Count from 1 to 5, digits separated by spaces, nothing else."}])
        text, usage, models = "", None, set()
        for ch in s:
            models.add(ch.model)
            if ch.choices and ch.choices[0].delta.content:
                text += ch.choices[0].delta.content
            if ch.usage:
                usage = ch.usage
        assert models == {MODEL}, models
        assert "1 2 3 4 5" in text, text
        assert usage is not None
        return f"{text.strip()!r} usage={usage.prompt_tokens}/{usage.completion_tokens}"

    def oa_tools():
        msgs = [{"role": "user", "content": "What's the weather in Boston? Use the tool."}]
        r = oa.chat.completions.create(model=MODEL, max_tokens=4000, messages=msgs, tools=TOOLS_OA)
        m = r.choices[0].message
        assert r.choices[0].finish_reason == "tool_calls" and m.tool_calls, (r.choices[0].finish_reason, m)
        call = m.tool_calls[0]
        args = json.loads(call.function.arguments)
        assert "boston" in args["city"].lower(), args
        msgs += [{"role": "assistant", "content": m.content, "tool_calls": [call.model_dump()]},
                 {"role": "tool", "tool_call_id": call.id, "content": "72F and sunny"}]
        r2 = oa.chat.completions.create(model=MODEL, max_tokens=4000, messages=msgs, tools=TOOLS_OA)
        t2 = r2.choices[0].message.content or ""
        assert "72" in t2, t2
        return f"call {call.function.name}({args}) -> {t2.strip()[:50]!r}"

    def oa_json():
        schema = {"type": "object", "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
                  "required": ["city", "country"], "additionalProperties": False}
        r = oa.chat.completions.create(model=MODEL, max_tokens=4000, messages=[
            {"role": "user", "content": "Give the capital city of Japan and its country."}],
            response_format={"type": "json_schema", "json_schema": {"name": "cap", "schema": schema, "strict": True}})
        data = json.loads(r.choices[0].message.content)
        assert data["city"].lower() == "tokyo", data
        return json.dumps(data)

    def an_plain():
        m = an.messages.create(model=MODEL, max_tokens=4000, system="Answer in one short sentence.",
                               messages=[{"role": "user", "content": "What is the capital of Italy?"}])
        assert m.model == MODEL, m.model
        text = "".join(b.text for b in m.content if b.type == "text")
        assert "rome" in text.lower(), text
        return f"{text.strip()[:60]!r} stop={m.stop_reason} usage={m.usage.input_tokens}/{m.usage.output_tokens}"

    def an_stream_tools():
        with an.messages.stream(model=MODEL, max_tokens=4000, tools=TOOLS_AN, messages=[
                {"role": "user", "content": "What's the weather in Denver? Use the tool."}]) as s:
            final = s.get_final_message()
        assert final.model == MODEL, final.model
        tu = [b for b in final.content if b.type == "tool_use"]
        assert final.stop_reason == "tool_use" and tu, (final.stop_reason, final.content)
        assert "denver" in tu[0].input["city"].lower(), tu[0].input
        # second turn: send back the full assistant content (incl. any thinking blocks) + tool_result
        with an.messages.stream(model=MODEL, max_tokens=4000, tools=TOOLS_AN, messages=[
                {"role": "user", "content": "What's the weather in Denver? Use the tool."},
                {"role": "assistant", "content": final.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tu[0].id, "content": "41F, snow"}]}]) as s:
            f2 = s.get_final_message()
        t2 = "".join(b.text for b in f2.content if b.type == "text")
        assert "41" in t2, t2
        return f"tool_use {tu[0].input} -> {t2.strip()[:50]!r}"

    print(f"--- block {block}", flush=True)
    for name, fn in [("openai non-stream", oa_plain), ("openai stream+usage", oa_stream),
                     ("openai tools 2-turn", oa_tools), ("openai json_schema", oa_json),
                     ("anthropic non-stream", an_plain), ("anthropic stream tools 2-turn", an_stream_tools)]:
        check(f"[{block}] {name}", fn)

# Models endpoint through both SDKs (no force header)
oa = openai.OpenAI(base_url=f"{BASE}/v1", api_key=ADMIN, max_retries=0)
an = anthropic.Anthropic(base_url=BASE, api_key=ADMIN, max_retries=0)
check("openai models.list", lambda: [m.id for m in oa.models.list()])
check("anthropic models.list", lambda: [m.id for m in an.models.list()])
check("anthropic count_tokens", lambda: an.messages.count_tokens(
    model=MODEL, messages=[{"role": "user", "content": "hello there"}]).input_tokens)

fails = [r for r in results if r[1] == "FAIL"]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
