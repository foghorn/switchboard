# Switchboard

Switchboard is a small failover gateway for LLM APIs. Your applications talk to **one model name** over the **OpenAI** or **Anthropic** API. Behind that name, Switchboard tries an ordered list of upstream models, local or cloud, until one answers.

```
            ┌──────────────────────────── Switchboard ───────────────────────────┐
 OpenAI  ──▶│  /v1/chat/completions ─┐                                          │
 clients    │                        ├─▶ route "default" ─▶ 1. local GPU server  ─┼─▶ llama.cpp / vLLM
 Anthropic ▶│  /v1/messages ─────────┘      (one name)      2. Ollama box        ─┼─▶ Ollama
 clients    │                                               3. Claude Haiku      ─┼─▶ api.anthropic.com
            └────────────────────────────────────────────────────────────────────┘
```

A typical chain is "use my local model, fall back to a second local box, and only pay for a cloud model when both are down":
- Connection refused, 5xx errors, rate limits, timeouts and stalled streams all move the request to the next block.
- Clients never see which model answered. Every response says the same model name, and the formats are translated both ways.

## Features

- **Two client APIs, one name.** OpenAI Chat Completions and Anthropic Messages are both supported, streaming and non-streaming. `/v1/models` lists only your route names, and every response reports the route name as its model.
- **Three upstream protocols.** Any OpenAI-compatible server (llama.cpp, vLLM, Ollama, LM Studio, OpenAI, ...), the Anthropic Messages API, and Google Gemini (`generateContent`).
- **Full translation.** Messages, system prompts, images, tools and tool results, tool choice, JSON-schema output, stop sequences and token usage are translated between the formats, both for whole responses and for streaming events. When client and upstream speak the same format, the body is passed through with only the model name changed.
- **Failover rules.**
  - Per-block timeouts (connect, first token, idle gap, total).
  - Streams commit to a block only once real output arrives, so the client never sees half of a failed attempt.
  - A circuit breaker with cooldown and health probes.
  - Genuine client errors (a malformed request) are returned instead of being retried on every block. Context-length errors move on to the next block.
- **Self-contained blocks.** Each fallback step carries its own URL, key, model, timeouts, parameter overrides and pricing.
- **Context-window auto-detection.** Switchboard asks each upstream for its window: the models list, Ollama `/api/ps`, llama.cpp `/props`, the Anthropic and Gemini model APIs. It skips blocks that are too small for a prompt, and advertises the smallest window in the chain to clients under all the common field names.
- **Cost guard.** Per-block pricing, a daily spend cap per paid block, and a request log with cost.
- **Operations.**
  - Live config reload on save or SIGHUP; an invalid edit is ignored and logged.
  - `/status` shows per-block health.
  - A `check` command validates the config and probes every upstream before deploy.
  - One JSON log line per request. Prompts are never logged.
  - Opt-in prompt capture per client: the exact request, what each upstream was sent after translation, and the full response, with conversation turns deduplicated on disk.
- **Small.** Python, Starlette and httpx; no provider SDKs; one container with a read-only root filesystem.

## Quick start

```bash
git clone https://github.com/foghorn/switchboard.git && cd switchboard
cp config/switchboard.example.yml config/switchboard.yml   # edit: your upstreams and client keys
cp .env.example .env                                       # fill in the ${VARS} the config uses
docker compose up -d --build
docker compose run --rm switchboard check                  # validate config, probe every upstream
```

Then point any client at it:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="<a client key>")
client.chat.completions.create(model="default", messages=[{"role": "user", "content": "Hello"}])
```

```python
from anthropic import Anthropic
client = Anthropic(base_url="http://localhost:8000", api_key="<a client key>")
client.messages.create(model="default", max_tokens=1024, messages=[{"role": "user", "content": "Hello"}])
```

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer <a client key>" -H "Content-Type: application/json" \
  -d '{"model": "default", "messages": [{"role": "user", "content": "Hello"}], "stream": true}'
```

This works the same for any tool that lets you set an OpenAI- or Anthropic-compatible base URL (chat UIs, editors, agent frameworks).

Without Docker:

```bash
pip install .
switchboard serve --config config/switchboard.yml --port 8000
```

## Configuration at a glance

```yaml
clients:
  - name: my-app
    key: ${SWITCHBOARD_MY_APP_KEY}        # any string of 16+ chars; ${VAR} reads the environment

routes:
  - name: default                         # the one model name clients see
    blocks:                               # tried top to bottom
      - id: local-gpu
        provider: openai
        base_url: http://gpu-server.lan:8080/v1
        model: my-local-model
        timeouts: {first_token: 90, total: 1800}
      - id: claude-haiku
        provider: anthropic
        base_url: https://api.anthropic.com
        api_key: ${ANTHROPIC_API_KEY}
        model: claude-haiku-5-5
        spend_cap_usd_per_day: 5.00
        pricing: {input_per_mtok: 0.10, output_per_mtok: 0.50}
```

[`config/switchboard.example.yml`](config/switchboard.example.yml) is a complete, commented example.

## Documentation

| Page | What's in it |
|---|---|
| [Configuration reference](docs/configuration.md) | Every setting, its default, and the `${VAR}` and live-reload rules |
| [Failover behavior](docs/failover.md) | What triggers failover, timeouts, streaming commit, cooldown, spend caps, context windows |
| [API and compatibility](docs/api.md) | Endpoints, authentication, the translation matrix, known limitations |
| [Operations](docs/operations.md) | Deployment, `check`, logs, prompt capture, `/status`, forcing a block, security notes |

## Development

```bash
pip install -e '.[dev]'
pytest                      # unit tests against fake upstreams; no network, no keys
pip install -e '.[smoke]'   # then, against a running instance and real upstreams:
SWITCHBOARD_URL=http://localhost:8000 SWITCHBOARD_ADMIN_KEY=... python examples/smoke_test.py default local-gpu claude-haiku
```

## License

MIT. See [LICENSE](LICENSE).
