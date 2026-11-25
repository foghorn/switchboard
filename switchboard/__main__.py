"""CLI: `switchboard serve` runs the gateway, `switchboard check` validates the
config and probes every block (exit 2 = invalid config, 1 = a block is
unreachable, 0 = all good)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .config import ConfigError, load_config

DEFAULT_CONFIG = os.environ.get("SWITCHBOARD_CONFIG", "/config/switchboard.yml")


def main() -> None:
    ap = argparse.ArgumentParser(prog="switchboard")
    ap.add_argument("command", choices=["serve", "check"], nargs="?", default="serve")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    from .engine import Engine

    if args.command == "check":
        cfg.server.state_dir = os.environ.get("SWITCHBOARD_CHECK_STATE", "/tmp/switchboard-check")
        engine = Engine(cfg)

        async def run() -> int:
            bad = 0
            for r in cfg.routes:
                print(f"route {r.name}:")
                for b in r.blocks:
                    if not b.enabled:
                        print(f"  {b.id:<24} disabled")
                        continue
                    ok, msg = await engine.probe(b)
                    bad += not ok
                    ctx = ""
                    if ok and b.max_context_tokens == "auto":
                        v, src = await engine.detect_context(b)
                        ctx = f"  ctx {v} ({src})" if v else (
                            f"  ctx {b.context_fallback} (fallback)" if b.context_fallback else "  ctx not reported")
                    elif isinstance(b.max_context_tokens, int):
                        ctx = f"  ctx {b.max_context_tokens} (config)"
                    print(f"  {b.id:<24} {b.provider:<9} {b.model:<34} {'OK' if ok else 'FAIL ' + msg}{ctx}")
            await engine.close()
            return 1 if bad else 0

        sys.exit(asyncio.run(run()))

    import uvicorn

    from .app import create_app

    engine = Engine(cfg, config_path=args.config)
    blocks = ", ".join(f"{r.name}=[{' > '.join(b.id for b in r.blocks)}]" for r in cfg.routes)
    logging.getLogger("switchboard").info("starting: %s", blocks)
    uvicorn.run(create_app(engine), host=args.host, port=args.port, log_level="warning",
                proxy_headers=False, timeout_keep_alive=75)


if __name__ == "__main__":
    main()
