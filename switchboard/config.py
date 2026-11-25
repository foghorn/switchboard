"""Config file loading and validation.

Every block is self-contained: provider, URL, key, model, timeouts, cooldown,
parameter tweaks and pricing all live inside it, even when that means repeating
a key. Any string may reference an environment variable as ${NAME}.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Timeouts(_Strict):
    connect: float = 5.0  # TCP/TLS connect
    first_token: float = 120.0  # streaming: request sent -> first content/progress event
    idle: float = 120.0  # streaming: max gap between events once started
    total: float = 900.0  # whole request (non-streaming: the only read deadline)


class Cooldown(_Strict):
    after_failures: int = Field(2, ge=1)
    seconds: float = Field(30.0, ge=0)
    probe: bool = True  # poll the block's models endpoint while cooling down


class Pricing(_Strict):
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    # Optional second rate card for long prompts (e.g. Haiku 5.5 above 100k tokens).
    long_context_threshold: int | None = None
    long_input_per_mtok: float | None = None
    long_output_per_mtok: float | None = None

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        inp, out = self.input_per_mtok, self.output_per_mtok
        if self.long_context_threshold and input_tokens > self.long_context_threshold:
            inp = self.long_input_per_mtok if self.long_input_per_mtok is not None else inp
            out = self.long_output_per_mtok if self.long_output_per_mtok is not None else out
        return (input_tokens * inp + output_tokens * out) / 1_000_000


class Block(_Strict):
    id: str
    provider: Literal["openai", "anthropic", "gemini"]
    base_url: str
    api_key: str = ""
    model: str
    enabled: bool = True
    # Context window: "auto" asks the server (models endpoint, Ollama /api/ps, llama.cpp
    # /props, Anthropic/Gemini model info); a number pins it; null = unknown/no limit.
    # Prompts clearly bigger than the window skip the block.
    max_context_tokens: int | Literal["auto"] | None = "auto"
    context_fallback: int | None = None  # used while "auto" has nothing (e.g. Ollama model not loaded yet)
    default_max_tokens: int | None = None  # used when the client sends none (Anthropic requires one)
    timeouts: Timeouts = Timeouts()
    cooldown: Cooldown = Cooldown()
    # HTTP 400 from this block: "context" = fail over only for context-length errors,
    # "always" = always fail over, "never" = return it to the client.
    failover_on_400: Literal["context", "always", "never"] = "context"
    defaults: dict[str, Any] = {}  # merged into the provider-native body when the key is absent
    overrides: dict[str, Any] = {}  # always merged into the provider-native body
    drop_params: list[str] = []  # top-level keys removed from the provider-native body
    headers: dict[str, str] = {}
    pricing: Pricing | None = None
    spend_cap_usd_per_day: float | None = None

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", v):
            raise ValueError("block id may only contain letters, digits, '.', '_' and '-'")
        return v

    @field_validator("base_url")
    @classmethod
    def _url_ok(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v.rstrip("/")

    @model_validator(mode="after")
    def _spend_needs_price(self) -> Block:
        if self.spend_cap_usd_per_day is not None and self.pricing is None:
            raise ValueError("spend_cap_usd_per_day needs a pricing section")
        return self


class Route(_Strict):
    name: str
    # Advertised to clients (/v1/models, /props, /health). "auto"/"min" = smallest window
    # in the chain (every block can serve it), "max" = largest, or a fixed number.
    context_tokens: int | Literal["auto", "min", "max"] | None = "auto"
    blocks: list[Block] = Field(min_length=1)


class Client(_Strict):
    name: str
    key: str = Field(min_length=16)
    admin: bool = False


class Server(_Strict):
    # What to do when a request names a model that isn't a route.
    unknown_model: Literal["default", "reject"] = "default"
    expose_block_header: bool = False  # X-Switchboard-Block on responses
    probe_interval: float = 15.0
    context_refresh: float = 600.0  # seconds between context-window re-detection
    state_dir: str = "/state"  # spend.json and requests.jsonl


class Config(_Strict):
    server: Server = Server()
    clients: list[Client] = Field(min_length=1)
    routes: list[Route] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Config:
        def dupes(items: list[str]) -> set[str]:
            seen, out = set(), set()
            for i in items:
                (out if i in seen else seen).add(i)
            return out

        if d := dupes([r.name for r in self.routes]):
            raise ValueError(f"duplicate route names: {sorted(d)}")
        if d := dupes([b.id for r in self.routes for b in r.blocks]):
            raise ValueError(f"duplicate block ids (must be unique across routes): {sorted(d)}")
        if d := dupes([c.name for c in self.clients]):
            raise ValueError(f"duplicate client names: {sorted(d)}")
        if dupes([c.key for c in self.clients]):
            raise ValueError("two clients share the same key")
        return self

    def route(self, name: str | None) -> Route | None:
        for r in self.routes:
            if r.name == name:
                return r
        if self.server.unknown_model == "default":
            return self.routes[0]
        return None

    def all_blocks(self) -> list[Block]:
        return [b for r in self.routes for b in r.blocks]


def _interpolate(node: Any, path: str = "") -> Any:
    if isinstance(node, str):

        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise ConfigError(f"{path or 'config'}: environment variable ${{{name}}} is not set")
            return os.environ[name]

        return _ENV_RE.sub(sub, node)
    if isinstance(node, dict):
        return {k: _interpolate(v, f"{path}.{k}" if path else str(k)) for k, v in node.items()}
    if isinstance(node, list):
        return [_interpolate(v, f"{path}[{i}]") for i, v in enumerate(node)]
    return node


def load_config(path: str | Path) -> Config:
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {p}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{p}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: top level must be a mapping")
    raw = _interpolate(raw)
    try:
        return Config.model_validate(raw)
    except Exception as e:  # pydantic.ValidationError
        raise ConfigError(f"{p}: {e}") from e
