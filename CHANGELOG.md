# Changelog

## 0.5.2 (2026-11-28)

- Raw JSON buttons are grouped into Request and Response. The one being shown stays highlighted, and pressing it again hides the JSON.

## 0.5.1 (2026-11-28)

- The inspector's Raw JSON section shows the request and the response on their own, and each block's own answer when it was in a different format from the client's, besides the body each block was sent and the whole capture.

## 0.5.0 (2026-11-28)

- Captured streamed answers record when each part was produced: runs of thinking, answer text and tool calls, timed from when the request was sent (`output_timeline` in the capture file).
- The inspector shows a captured response broken into parts: how the output tokens split between thinking, answer and tool calls, the timeline of each part for streamed answers, and the tool calls with their arguments laid out as JSON.
- The inspector warns when an answer contains tool-call markup as plain text (`<tool_call>`, `<function=…>`, `[TOOL_CALLS]`, `<|python_tag|>`), which means the server didn't parse the model's tool call.

## 0.4.0 (2026-11-27)

- The request log (`state_dir/requests.jsonl`) is trimmed to `server.request_log.retention_days`, 7 days by default, at startup and then hourly. Before this it grew without limit. To keep more history, raise the setting.
- Docs: a screenshot of the inspector page, the inspector's unauthenticated static files and `POST /inspector/api/capture` in the API reference, and `switchboard captures --dir`.

## 0.3.0 (2026-11-27)

- Inspector page at `/inspector` (`server.inspector.enabled`, off by default, admin keys only):
  - requests in progress, with the block working on each and how long it has been waiting or answering;
  - finished requests, with failovers, timing and tokens;
  - for captured requests, the prompt broken into system prompt, tool definitions grouped by MCP prefix, documents, conversation history, the latest message and this turn's tool calls, plus the messages, response and raw JSON;
  - capture switches per client and for every client, saved in `state_dir/capture.json` and overriding the config until reset.

## 0.2.1 (2026-11-26)

- A client that disconnects before any output now cancels the upstream request at once and is not failed over. Before this, the upstream finished reading the prompt, which can take minutes, and a failure could even send the abandoned request on to the next block. Applies to streaming and non-streaming requests.

## 0.2.0 (2026-11-25)

- Opt-in prompt capture (`capture: true` per client, or the admin-only `X-Switchboard-Capture` header):
  - saves the client request, the exact body each tried block was sent, the upstream answer and the client's response, with streams reassembled;
  - stores only each turn's new messages and reuses identical system prompts and tools, gzip-compressed;
  - `server.capture.retention_days` and `max_mb` limit disk use;
  - `switchboard captures list` and `show` read them back.
- The request log gains a `capture` field when a request was captured.

## 0.1.0 (2026-11-25)

First release.

- OpenAI Chat Completions and Anthropic Messages client APIs, streaming and non-streaming; one model name per route.
- Upstream adapters for OpenAI-compatible servers, Anthropic and Gemini, with request, response and stream translation.
- Failover:
  - per-block connect, first-token, idle and total timeouts;
  - streams commit at the first output;
  - cooldown with health probes;
  - context-length-aware handling of HTTP 400.
- Context-window auto-detection (models list, Ollama `/api/ps`, llama.cpp `/props`, Anthropic and Gemini model APIs), advertised through `/v1/models`, `/props` and `/health`.
- Per-block pricing, daily spend caps and a JSON request log.
- Live config reload, `/status`, `switchboard check`, admin-only block forcing.
