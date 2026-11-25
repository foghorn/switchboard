# Operations

## Deploying

The included [`docker-compose.yml`](../docker-compose.yml) is a hardened single-container setup:
- read-only root filesystem, all capabilities dropped, `no-new-privileges`;
- config mounted read-only;
- state in a named volume.

The container listens on port 8000 and runs as uid 10001, so the mounted config must be readable by that uid.

Mount the config **directory**, not the file. Many editors save by writing a new file and renaming it over the old one. A single-file bind mount keeps pointing at the old file, so the live reload never sees the change.

| Environment variable | Default | Meaning |
|---|---|---|
| `SWITCHBOARD_CONFIG` | `/config/switchboard.yml` | Config path |
| `TZ` | `UTC` | Local midnight in this zone resets the daily spend caps |
| anything your config references as `${NAME}` | | |

CLI:

```
switchboard serve [--config PATH] [--host 0.0.0.0] [--port 8000]
switchboard check [--config PATH]
```

## Pre-flight check

`switchboard check` validates the config, then probes every enabled block's models endpoint and prints the context window each one reports:

```
route default:
  local-gpu                openai    my-local-model                     OK  ctx 131072 (models endpoint)
  ollama                   openai    my-ollama-model:latest             OK  ctx 131072 (fallback)
  claude-haiku             anthropic claude-haiku-5-5                   OK  ctx 1000000 (anthropic models API)
```

| Exit code | Meaning | Suggested deploy action |
|---|---|---|
| 0 | Config valid, every block reachable | deploy |
| 1 | Config valid, at least one block unreachable right now | deploy anyway (that's what failover is for), but look |
| 2 | Config invalid | keep the old container |

## Request log

Every request writes one JSON line to stdout (`docker logs`) and to `state_dir/requests.jsonl`:

```json
{"ts":"2026-01-15T03:00:12+0000","id":"53bb024bd378","client":"my-app","route":"default","api":"openai",
 "stream":false,"attempts":[{"block":"local-gpu","error":"ConnectError: All connection attempts failed","status":null,"ms":99},
 {"block":"claude-haiku","ok":true}],"served_by":"claude-haiku","status":200,"ms":1610,"tokens_in":59,"tokens_out":35,"cost_usd":0.000024}
```

| Field | Meaning |
|---|---|
| `api` | The client format: `openai` or `anthropic` |
| `attempts` | Every block tried or skipped, in order, with the error or skip reason |
| `served_by` | The block that answered, or `null` if all failed |
| `ttft_ms` | Streaming only: time until the request committed to a block |
| `cost_usd` | Only for blocks with `pricing` |
| `note` | Mid-stream problems: stall, upstream error, client disconnect |

Prompts and responses are never logged.

`requests.jsonl` isn't rotated by Switchboard. Rotate it externally if the volume matters to you; `docker logs` rotation is set by the Docker log driver.

## Status

```bash
curl -s -H "Authorization: Bearer $KEY" http://localhost:8000/status | jq
```

For each block, `/status` shows:
- `cooling_down` and `cooldown_remaining_s`;
- `failures` (consecutive), `ok_count` and `error_count`;
- `last_error` and `last_error_at`, `last_ok_at`;
- `context_tokens` and `context_source`;
- `spend_usd_today`.

Admin keys also see each block's `base_url` and `model`.

## Testing one block

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $ADMIN_KEY" -H "X-Switchboard-Force: claude-haiku" \
  -H "Content-Type: application/json" \
  -d '{"model":"default","messages":[{"role":"user","content":"ping"}]}'
```

[`examples/smoke_test.py`](../examples/smoke_test.py) does this for each block you name. It exercises plain, streaming, tool-calling and JSON-schema requests through the official OpenAI and Anthropic SDKs.

## Changing config

Edit the file. Within 5 seconds the log shows `config reloaded (file changed)`, or the reason the edit was rejected, in which case the old config stays active. `docker kill -s HUP <container>` reloads at once.

Block state (cooldown, counts, spend, detected context) is keyed by block `id`. Renaming a block starts it fresh, and its spend for the day starts over.

## Security notes

- **TLS:** Switchboard serves plain HTTP. Keep it on a trusted network, or put a TLS-terminating reverse proxy in front of it.
- **Keys:**
  - Give every client its own key; revoke one by deleting it from the config (applied live).
  - Keys are compared in constant time.
  - Upstream keys never leave the gateway. Clients only ever see their own Switchboard key.
- **Config file:** it holds client keys and possibly upstream keys. Keep it out of version control (the repository's `.gitignore` already does), or keep only `${VARS}` in it and supply the values through the environment.
- **Unauthenticated access:** `/health` is the only endpoint open without a key. It reveals the version and the advertised context window.
