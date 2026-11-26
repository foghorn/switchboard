# Failover behavior

For each request, Switchboard picks the route named by `model`, builds the list of usable blocks, and tries them in order. The first block that produces a good response serves the request. If none does, the client gets one error that lists every attempt.

## Which blocks are tried

A block is **skipped** without being called when:

- `enabled: false`;
- the prompt's estimated size is larger than the block's context window. The estimate is about 4 characters per token, over messages, system prompt and tools;
- its daily spend cap has been reached;
- it is cooling down.

If every block that is otherwise usable is cooling down, they are all tried anyway, in order. A request fails only if the upstreams really fail now.

An admin client can send `X-Switchboard-Force: <block id>` to try exactly one block and ignore cooldown, caps and size checks. That's useful for testing.

## What counts as a failure

| Outcome | Fail over? | Counts toward cooldown? |
|---|---|---|
| Connection refused or reset, DNS failure, connect timeout | yes | yes |
| HTTP 5xx, 429, 408, 409, 413 | yes | yes |
| HTTP 401, 403, 404 (a misconfigured block: bad key, wrong model name) | yes | yes |
| HTTP 400/422 that reads like a context-length error | yes | no: the prompt was too big for this block, not its fault |
| Any other HTTP 400/422 | **no**: returned to the client (configurable per block with `failover_on_400`) | no |
| HTTP 200 whose body is an error, or has no choices or candidates | yes | yes |
| Streaming: no output before `first_token` | yes | yes |
| Streaming: the stream ends or sends an error event before any output | yes | yes |
| Non-streaming: no complete response within `total` | yes | yes |

Other 400s aren't retried because a malformed request, such as an invalid tool schema, would fail on every block, and a paid block would charge for the attempt. If a block legitimately rejects a parameter that others accept, fix it in that block with `drop_params`, or set `failover_on_400: always`.

## Streaming: the commit point

For streaming requests Switchboard reads the upstream stream and **buffers** events until the first real output arrives: content, reasoning or a tool call.
- **Before that point:** a failure throws away the buffer, and the next block is tried. The client sees nothing of the failed attempt.
- **At that point:** the request is committed to the block. Buffered events are sent, and the rest is relayed as it arrives.
- **After that point:** if the upstream fails (connection lost, silence longer than `idle`, past `total`, or an error event), the stream ends with an error event in the client's format: an `error` event for Anthropic clients, or an `{"error": ...}` chunk followed by `[DONE]` for OpenAI clients. Switchboard never splices output from two models into one response.

`first_token` therefore covers connection, queueing, prompt processing and, for reasoning models, the start of thinking. Size it for the slowest normal case: a cold model load, or a long prompt on slow hardware.

## Client hang-ups

If the client disconnects before any output has been sent, for example when a chat UI's stop button is pressed while the model is still reading a long prompt:
- Switchboard cancels the upstream request at once. Closing that connection is how OpenAI-compatible servers are told to stop.
- No other block is tried.
- The block isn't counted as failing.

The request log shows status 499 with `client disconnected` on that attempt. After a stream has started, a disconnect ends it the same way, and the log line says `client disconnected`.

## Non-streaming requests

A non-streaming upstream call has no first-token signal; the response arrives all at once. Its only deadline is `total`. Fast failures (refused connection, 5xx) still fail over at once, but a block that accepts the request and then hangs holds it until `total`. Set `total` with that in mind, and set the client's own timeout to at least the sum of the `total`s it should be willing to wait through.

A busy but healthy server that queues requests doesn't trigger failover for non-streaming calls until `total`. That's usually what batch jobs want.

## Cooldown and probes

Each failure that counts toward cooldown adds 1 to the block's consecutive-failure count; a success resets it.
- When the count reaches `cooldown.after_failures`, the block is skipped for `cooldown.seconds`.
- With `cooldown.probe: true`, its models endpoint is polled every `server.probe_interval` seconds, and the first successful probe ends the cooldown early. Probes are free list calls; no tokens are generated.

`/status` shows each block's failure count, cooldown state, last error and success and error totals.

## Spend caps

When a block has `pricing`, every successful response's token usage is priced and added to that block's spend for the day. `spend_cap_usd_per_day` makes the block skip once today's spend reaches the cap.
- A request that starts under the cap always completes, so the cap can be exceeded by the cost of one request.
- Spend is persisted in `state_dir/spend.json`, so restarts don't reset it.
- The day rolls over at local midnight in the container's `TZ`.

## Context windows

With `max_context_tokens: auto`, Switchboard asks each block for its context window at startup, after every config reload, and every `server.context_refresh` seconds:

| Upstream | Source |
|---|---|
| OpenAI-compatible | the model's entry in `GET /v1/models`: `context_length`, `max_context_length`, `max_model_len`, `max_input_tokens`, `meta.n_ctx`, `limit.context`, `context_window` |
| Ollama | `GET /api/ps` → `context_length`: the window the model is loaded with, reported only while it is loaded |
| llama.cpp server | `GET /props` → `default_generation_settings.n_ctx` |
| Anthropic | `GET /v1/models/{model}` → `max_input_tokens` |
| Gemini | `GET /v1beta/models/{model}` → `inputTokenLimit` |

The last good value is kept when a server stops reporting, for example after Ollama unloads the model. `context_fallback` covers the time before the first successful detection.

Ollama's `/api/show` is deliberately not used. It reports the model architecture's maximum, not the window Ollama actually runs it with.

The route advertises a window to clients according to `context_tokens`. The default, `auto`, gives the smallest window in the chain, because any block may end up serving a request. Clients that size their prompts from it therefore fit everywhere. A prompt that is too big for an early block is routed to a later, larger one.

## Mixed conversations

A multi-turn conversation can be served by different blocks on different turns. Switchboard rebuilds each request from the full history the client sends:
- Anthropic thinking blocks pass through unchanged when the conversation reaches an Anthropic block.
- They are dropped when it goes to a non-Anthropic block. They are signed and bound to the model that produced them, and no other provider can use them.
- Reasoning text from OpenAI-compatible servers (`reasoning_content`) is passed to OpenAI clients and is not sent to Anthropic clients.
- Tool-call ids are normalized so a `tool_use` id and its `tool_result` id stay matched across formats.
