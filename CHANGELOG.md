# Changelog

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
