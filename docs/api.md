# API and compatibility

## Authentication

Every endpoint except `/health` and the inspector page's static files needs a client key from the config's `clients` list, sent either way:

```
Authorization: Bearer <key>
x-api-key: <key>
```

Both headers work on every endpoint, so OpenAI SDKs (which send Bearer) and Anthropic SDKs (which send `x-api-key`) work without extra settings. Errors come back in the format of the endpoint that was called.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/chat/completions` (also `/chat/completions`) | OpenAI Chat Completions. `"stream": true` for SSE. `stream_options.include_usage` is honored. |
| POST | `/v1/messages` | Anthropic Messages. `"stream": true` for SSE. `anthropic-version` and `anthropic-beta` headers are forwarded to Anthropic blocks. |
| POST | `/v1/messages/count_tokens` | Forwarded to the first available Anthropic block in the route, which gives a real count. Otherwise a ~4 characters per token estimate. |
| GET | `/v1/models` (also `/models`) | One object per route, shaped so both OpenAI and Anthropic SDKs can parse it, with the advertised context window under every common field name. |
| GET | `/v1/models/{name}` | One route. |
| GET | `/props` | llama.cpp-style: `default_generation_settings.n_ctx` for the first route, for tools that read the window from there. |
| GET | `/health` | Unauthenticated liveness check. Includes `max_context` for the first route. |
| GET | `/inspector` | The [inspector page](operations.md#inspector-page), when `server.inspector.enabled` is true (404 otherwise). The page and its script and stylesheet need no key; its JSON API under `/inspector/api/` (including `POST /inspector/api/capture` for the capture switches) needs an admin key. |
| GET | `/status` | Per-route and per-block health, cooldown, last error, counts, detected context window and its source, and today's spend. Upstream URLs and models are shown only to admin clients. |

### Request headers Switchboard reads

| Header | Effect |
|---|---|
| `X-Switchboard-Force: <block id>` | Admin keys only. Try exactly this block, ignoring cooldown, spend cap and size checks. |
| `X-Switchboard-Capture: 1` or `0` | Admin keys only. Capture this request, or don't, whatever the client's `capture` setting. See [capturing prompts](operations.md#capturing-prompts). |

### Response headers

| Header | When |
|---|---|
| `X-Switchboard-Block: <block id>` | Only with `server.expose_block_header: true`. |

## Model names

Clients send a route name as `model`, and every response carries that route name, whatever upstream model answered.

A model name that isn't a route goes to the first route by default (`server.unknown_model: default`), so clients with a hard-coded model name keep working. Set `reject` to return 404 instead.

## Translation

When the client format and the block's provider match (OpenAI client → `openai` block, Anthropic client → `anthropic` block), the body is passed through unchanged apart from `model` and the block's `defaults`, `overrides` and `drop_params`. Every field the upstream supports therefore works, including ones Switchboard knows nothing about.

Across formats, requests go through the Anthropic Messages shape as a hub:

| Feature | OpenAI ⇄ Anthropic | → Gemini |
|---|---|---|
| System prompt | `system`/`developer` messages ⇄ `system` | `systemInstruction` |
| Text and images | `image_url` (data URL or http URL) ⇄ `image` (base64 or url) | `inlineData` / `fileData` |
| Tools | `tools[].function` ⇄ `tools[].input_schema` | `functionDeclarations` (`parametersJsonSchema`) |
| Tool calls and results | `tool_calls` + `role: tool` ⇄ `tool_use` + `tool_result` | `functionCall` / `functionResponse` |
| Tool choice | `auto` / `required` / `none` / named ⇄ `auto` / `any` / `none` / `tool` | `functionCallingConfig` |
| Parallel tool calls off | `parallel_tool_calls: false` ⇄ `disable_parallel_tool_use` | n/a |
| Structured output | `response_format: json_schema` ⇄ `output_config.format` | `responseJsonSchema` |
| Max output tokens | `max_tokens` / `max_completion_tokens` ⇄ `max_tokens` | `maxOutputTokens` |
| Stop sequences | `stop` ⇄ `stop_sequences` | `stopSequences` |
| Temperature, top_p | passed | passed (and `top_k`) |
| Reasoning effort | `reasoning_effort` → `output_config.effort` | n/a |
| Stop / finish reason | mapped both ways (`tool_calls` ⇄ `tool_use`, `length` ⇄ `max_tokens`, ...) | mapped |
| Token usage | mapped both ways, including in streams | mapped |
| Streaming | full event translation, including incremental tool-call arguments | SSE (`alt=sse`) |

### Known limitations

- **Not implemented:** the OpenAI Responses API, embeddings, image generation, audio, and the legacy completions endpoint.
- **Cross-format requests:**
  - OpenAI-only parameters with no Anthropic counterpart (`n`, `logprobs`, `seed`, `presence_penalty`, `frequency_penalty`, `response_format: json_object`) are dropped. They work unchanged on OpenAI-compatible blocks.
  - Anthropic server tools (web search, code execution, ...) are dropped when the request goes to a non-Anthropic block.
  - Every OpenAI `system` message is merged into the single Anthropic `system` prompt, wherever it appeared in the conversation.
- **Reasoning:** reasoning text from OpenAI-compatible servers isn't turned into Anthropic thinking blocks, which would need a signature. Anthropic thinking shows up for OpenAI clients as `reasoning_content`, but only when the model returns readable thinking.
- **Gemini:**
  - `functionCall` parts are replayed with Gemini's documented placeholder thought signature, because the original signature may come from another model or be missing.
  - Thought parts are not passed to clients.
  - The Gemini adapter is covered by unit tests but has seen less real-world use than the other two.
