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
switchboard captures [list [--client NAME] [--day YYYY-MM-DD] [-n 20]]
switchboard captures show ID [--trim N] [--tools] [--json] [--upstream BLOCK]
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
| `note` | Mid-stream problems (stall, upstream error) and client disconnects, before or during output. Status 499 means the client hung up. |
| `capture` | Only when the request was [captured](#capturing-prompts): its capture id |

The request log never contains prompts or responses. Those are only saved by capture, which is off unless you turn it on.

## Capturing prompts

Capture saves the exact messages that pass through the gateway, for debugging system prompts, tool definitions and agent loops. It is off by default. Turn it on for one client in the config (applied live, no restart):

```yaml
clients:
  - name: my-agent
    key: ${SWITCHBOARD_MY_AGENT_KEY}
    capture: true
```

An admin key can also capture a single request with `X-Switchboard-Capture: 1`, or skip one with `0`. The [inspector page](#inspector-page) has switches for each client and one for every client at once.

Each captured request is one file, `state_dir/captures/<day>/<id>.json.gz`, holding:

| Part | What it is |
|---|---|
| `request` | The body exactly as the client sent it |
| `attempts[].body` | What each tried block was actually sent: after translation, `defaults`, `overrides` and `drop_params`. Failed attempts are included, with their error. |
| `attempts[].response` | The upstream's own answer, when its format differs from the client's |
| `response` | What the client received. Streamed answers are put back together into one response; a stream cut off mid-way keeps what arrived. |
| everything else | The same fields as the request log line |

Read them with the CLI; inside Docker, run it with `docker exec`:

```bash
docker exec switchboard switchboard captures list --client my-agent
docker exec switchboard switchboard captures show 53bb024bd378            # readable transcript
docker exec switchboard switchboard captures show 53bb024bd378 --trim 500 # shorten long messages
docker exec switchboard switchboard captures show 53bb024bd378 --upstream claude-haiku   # exact upstream body (JSON)
docker exec switchboard switchboard captures show 53bb024bd378 --json | jq .request.system
```

The request log's `capture` field gives the id to look up.

**Disk use.** Agent loops resend the whole conversation on every turn. A capture therefore stores only the messages that an earlier capture of the same client doesn't already have, plus a reference to that capture, and it also reuses identical large fields such as the system prompt and tool list. `show` puts the full request back together. Files are gzip-compressed. Old days are deleted after `server.capture.retention_days`, and the oldest go first when `server.capture.max_mb` is exceeded. If a capture refers to one that has been deleted, `show` marks the missing part.

**Privacy.** Captures contain everything the client sent and received, which may include personal or confidential data. Files are readable only by the service user (mode 0600). Capture only the clients you are debugging, and turn it off afterwards. API keys and request headers are never captured.

`requests.jsonl` isn't rotated by Switchboard. Rotate it externally if the volume matters to you; `docker logs` rotation is set by the Docker log driver.

## Inspector page

A live web view of the traffic, for diagnosing prompts and routing while you work on them. Turn it on in the config (applied live):

```yaml
server:
  inspector:
    enabled: true
```

Then open `http://<switchboard host>:<port>/inspector` and sign in with an admin client key. The key is kept in that browser tab only (`sessionStorage`).

The page shows:

- **Requests in progress:** which block is working on each, whether it is still waiting for the first token or already answering, and for how long. Updated every two seconds.
- **Finished requests:** status, the block that answered, failovers, time and tokens. The last `server.inspector.history` requests are kept, and refilled from `requests.jsonl` after a restart.
- **For each request:**
  - where the time went: failed attempts, waiting for the first token (prompt reading), then answering;
  - time to first token and output speed.
- **For captured requests only, the prompt broken into parts,** with token estimates:
  - the system prompt;
  - tool definitions, grouped by name prefix (an MCP server's tools share one), with each tool's description, schema and copied `$defs` sizes;
  - documents (RAG `<source>` blocks and document attachments);
  - earlier conversation, the latest user message, and this turn's tool calls and results.

  You can also read every message, the response, and the raw JSON as received and as sent to each block.

Requests from clients that aren't captured show timing and routing only, never prompt contents. The **Capture** panel turns capture on or off for each client, or for every client at once. These switches are saved in `state_dir/capture.json`, survive restarts, and win over `capture:` in the config until you reset them on the page. Each change is logged as a warning.

Token figures for the parts are estimates: the upstream's reported input-token count is divided between them by size, or about 4 characters per token before an answer arrives.

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
- **Captures:** when [capture](#capturing-prompts) is on, `state_dir/captures/` holds full prompts and responses. Treat the state volume as sensitive and back it up or share it accordingly. Request headers and API keys are never captured.
- **Inspector page:** when enabled, `/inspector` serves only static files without a key; all data and the capture switches need an admin key, sent as a bearer token (no cookies). The page shows captured prompts, so it is as sensitive as the captures themselves. It sends a strict Content-Security-Policy and renders every captured value as text.
- **Unauthenticated access:** `/health` is the only endpoint open without a key. It reveals the version and the advertised context window.
