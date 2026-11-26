# Changelog

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
