# Configuration reference

Switchboard reads one YAML file. The default path is `/config/switchboard.yml`. Override it with `--config` or the `SWITCHBOARD_CONFIG` environment variable.

[`config/switchboard.example.yml`](../config/switchboard.example.yml) is a complete, commented example.

## Ground rules

- **Unknown keys are errors.** A typo such as `timeout:` instead of `timeouts:` fails validation instead of being silently ignored.
- **`${NAME}` substitution.** Any string value may contain `${NAME}`, which is replaced by the environment variable `NAME`. An unset variable is a config error. This lets you keep every secret out of the file. Literal values work too.
- **Self-contained blocks.** Nothing is inherited between blocks. If two blocks use the same provider key, write it, or the same `${VAR}`, in both.
- **Live reload.** The file is re-read when it changes (checked every 5 s) or when the process gets SIGHUP. An invalid edit is logged and the running config stays in force. Health and spend state is kept per block `id` across reloads.

## Top level

```yaml
server:  {...}   # optional
clients: [...]   # required, at least one
routes:  [...]   # required, at least one
```

## `server`

| Key | Default | Meaning |
|---|---|---|
| `unknown_model` | `default` | A request that names a model which isn't a route: `default` sends it to the first route, `reject` returns 404. `default` lets clients that hard-code a model name work unchanged. |
| `expose_block_header` | `false` | Adds `X-Switchboard-Block: <block id>` to responses. Off by default so the gateway stays transparent. |
| `probe_interval` | `15` | Seconds between health probes of blocks that are cooling down. |
| `context_refresh` | `600` | Seconds between context-window re-detections. |
| `state_dir` | `/state` | Where `spend.json`, `requests.jsonl` and `captures/` are written. If it isn't writable, spend is kept in memory only, the request log goes to stdout only, and nothing is captured. |
| `capture.retention_days` | `7` | [Captures](operations.md#capturing-prompts) older than this many days are deleted. |
| `capture.max_mb` | `1024` | When captures take more than this, the oldest days are deleted. If today's captures alone are over it, capturing pauses until they aren't. |

## `clients`

Every request must carry one of these keys, as `Authorization: Bearer <key>` or `x-api-key: <key>`. Give each application its own key; the request log records the client `name`.

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Label used in logs. Must be unique. |
| `key` | required | At least 16 characters, unique. `openssl rand -base64 24` makes a good one. |
| `admin` | `false` | May use `X-Switchboard-Force` and `X-Switchboard-Capture`, and sees upstream URLs and models in `/status`. |
| `capture` | `false` | Save this client's full requests and responses to `state_dir/captures/`. See [capturing prompts](operations.md#capturing-prompts). |

## `routes`

A route is one public model name.

| Key | Default | Meaning |
|---|---|---|
| `name` | required | The model name clients send and see. Unique. |
| `context_tokens` | `auto` | The context window advertised to clients: `auto` or `min` is the smallest known window among the route's enabled blocks, `max` is the largest, a number fixes it, `null` advertises nothing. See [context windows](failover.md#context-windows). |
| `blocks` | required | The ordered fallback list (at least one block). |

## Blocks

| Key | Default | Meaning |
|---|---|---|
| `id` | required | Unique across **all** routes. Letters, digits, `.`, `_`, `-`. Used in logs, `/status` and `X-Switchboard-Force`. |
| `provider` | required | `openai` (any OpenAI-compatible server), `anthropic` or `gemini`. |
| `base_url` | required | See the base URL rules below. |
| `api_key` | `""` | Sent as `Authorization: Bearer` (openai), `x-api-key` (anthropic) or `x-goog-api-key` (gemini). Leave empty for servers without auth. |
| `model` | required | The upstream model name. |
| `enabled` | `true` | `false` keeps the block in the file but never uses it. |
| `max_context_tokens` | `auto` | The block's context window. `auto` asks the server, a number pins it, `null` means unknown (no size-based skipping). Prompts clearly larger than the window skip the block. |
| `context_fallback` | none | Window to assume while `auto` has nothing, for example before an Ollama model has been loaded once. |
| `default_max_tokens` | none | Output limit used when the client sends none. Anthropic requires one; without this setting, 8192 is used for Anthropic blocks. |
| `timeouts.connect` | `5` | Seconds to open the TCP/TLS connection. |
| `timeouts.first_token` | `120` | Streaming only: seconds from sending the request to the first content, reasoning or tool-call output. |
| `timeouts.idle` | `120` | Streaming only: longest silence allowed once output has started. |
| `timeouts.total` | `900` | The whole request. For non-streaming requests this is the only read deadline. |
| `cooldown.after_failures` | `2` | Consecutive failures before the block is put in cooldown. |
| `cooldown.seconds` | `30` | How long a cooling-down block is skipped. |
| `cooldown.probe` | `true` | While cooling down, probe the block's models endpoint every `probe_interval`; a success ends the cooldown early. |
| `failover_on_400` | `context` | What to do with HTTP 400/422 from this block: `context` fails over only when the error looks like a context-length error, `always` always fails over, `never` returns it to the client. |
| `defaults` | `{}` | Merged into the outgoing provider-native body **only where the key is absent**. Deep-merged for nested objects. |
| `overrides` | `{}` | Always merged into the outgoing body, replacing client values. Deep-merged. |
| `drop_params` | `[]` | Top-level keys removed from the outgoing body, for example sampling parameters a model rejects. |
| `headers` | `{}` | Extra HTTP headers sent upstream. |
| `pricing` | none | USD per million tokens, used for the request log's `cost_usd` and for `spend_cap_usd_per_day`: `input_per_mtok`, `output_per_mtok`, and optionally `long_context_threshold`, `long_input_per_mtok`, `long_output_per_mtok` for a second rate card above a prompt size. |
| `spend_cap_usd_per_day` | none | Once today's computed spend reaches this, the block is skipped until local midnight. Requires `pricing`. |

`defaults`, `overrides` and `drop_params` act on the body **in the upstream's own format**, after translation. For an `anthropic` block that means Anthropic field names (`output_config`, `thinking`, ...); for a `gemini` block, Gemini's (`generationConfig`, ...).

### Base URL rules

| Provider | Write | Requests go to |
|---|---|---|
| `openai` | the URL up to and including `/v1` | `{base_url}/chat/completions`, `{base_url}/models` |
| `anthropic` | the host, with or without `/v1` | `.../v1/messages`, `.../v1/models` |
| `gemini` | the host, with or without `/v1beta` | `.../v1beta/models/{model}:generateContent` |

### Recipes

Turn off reasoning on a llama.cpp-based server for one block:

```yaml
overrides: {chat_template_kwargs: {enable_thinking: false}}
```

Set a default effort level for an Anthropic block (clients may still override it):

```yaml
defaults: {output_config: {effort: low}}
```

Expose two model names with the same upstreams, one with reasoning off. Block ids must be unique, so the second route repeats the blocks under new ids:

```yaml
routes:
  - name: default
    blocks: [ {id: gpu, ...}, {id: cloud, ...} ]
  - name: fast
    blocks:
      - {id: gpu-fast, ..., overrides: {chat_template_kwargs: {enable_thinking: false}}}
      - {id: cloud-fast, ...}
```
