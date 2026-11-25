"""CLI: `switchboard serve` runs the gateway, `switchboard check` validates the
config and probes every block (exit 2 = invalid config, 1 = a block is
unreachable, 0 = all good), `switchboard captures` lists and shows captured
prompts."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .config import ConfigError, load_config

DEFAULT_CONFIG = os.environ.get("SWITCHBOARD_CONFIG", "/config/switchboard.yml")


def captures_main(argv: list[str]) -> None:
    import json
    from pathlib import Path

    from .capture import Reader, render

    ap = argparse.ArgumentParser(prog="switchboard captures",
                                 description="List and show captured prompts and responses.")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--dir", help="captures directory (default: <state_dir>/captures from the config)")
    sub = ap.add_subparsers(dest="cmd")
    ls = sub.add_parser("list", help="recent captures, newest first (the default)")
    ls.add_argument("--client")
    ls.add_argument("--day", help="YYYY-MM-DD")
    ls.add_argument("-n", "--limit", type=int, default=20)
    sh = sub.add_parser("show", help="one capture as a readable transcript")
    sh.add_argument("ref", help="capture id (or <day>/<id>), as in the request log's \"capture\" field")
    sh.add_argument("--json", action="store_true", help="the whole capture, every body reconstructed, as JSON")
    sh.add_argument("--upstream", metavar="BLOCK", help="print exactly what this block was sent, as JSON")
    sh.add_argument("--trim", type=int, help="cut each message to this many characters")
    sh.add_argument("--tools", action="store_true", help="include full tool definitions")
    args = ap.parse_args(argv)

    if args.dir:
        root = Path(args.dir)
    else:
        try:
            root = Path(load_config(args.config).server.state_dir) / "captures"
        except ConfigError as e:
            print(f"CONFIG ERROR: {e}", file=sys.stderr)
            sys.exit(2)
    reader = Reader(root)

    if args.cmd in (None, "list"):
        rows = reader.summaries(getattr(args, "client", None), getattr(args, "day", None),
                                getattr(args, "limit", 20))
        if not rows:
            print(f"no captures in {root}")
            return
        for r in rows:
            toks = f"{r.get('tokens_in') or '-'}/{r.get('tokens_out') or '-'}"
            print(f"{r['ref']:<26} {(r.get('ts') or '')[11:19]:<8} {r.get('client') or '':<14} "
                  f"{r.get('served_by') or '-':<18} {r.get('status')!s:<4} {toks:<14} {r.get('messages')!s:>4} msgs  "
                  f"{(r.get('preview') or '').replace(chr(10), ' ')[:60]}")
        return

    doc = reader.resolve(args.ref)
    if doc is None:
        print(f"capture {args.ref} not found in {root}", file=sys.stderr)
        sys.exit(1)
    if args.upstream:
        a = next((a for a in doc["attempts"] if a.get("block") == args.upstream and "body" in a), None)
        if a is None:
            print(f"block {args.upstream} was not tried for this request", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(a["body"], indent=2, ensure_ascii=False))
    elif args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    else:
        print(render(doc, trim=args.trim, show_tools=args.tools))


def main() -> None:
    if sys.argv[1:2] == ["captures"]:
        captures_main(sys.argv[2:])
        return
    ap = argparse.ArgumentParser(prog="switchboard")
    ap.add_argument("command", choices=["serve", "check", "captures"], nargs="?", default="serve")
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
