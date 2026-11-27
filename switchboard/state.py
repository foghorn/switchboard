"""Per-block health (cooldown / circuit breaker), daily spend, request log."""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Block

log = logging.getLogger("switchboard")


@dataclass
class BlockState:
    failures: int = 0
    cooldown_until: float = 0.0
    last_error: str | None = None
    last_error_at: float | None = None
    last_ok_at: float | None = None
    ok_count: int = 0
    error_count: int = 0
    spend_day: str = ""
    spend_usd: float = 0.0
    ctx_detected: int | None = None
    ctx_source: str | None = None
    ctx_detected_at: float | None = None

    def cooling(self, now: float | None = None) -> bool:
        return self.cooldown_until > (now or time.time())


@dataclass
class StateStore:
    state_dir: Path
    blocks: dict[str, BlockState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.writable = True
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            data = json.loads((self.state_dir / "spend.json").read_text())
            today = _today()
            for bid, s in data.items():
                if s.get("day") == today:
                    self.get(bid).spend_day, self.get(bid).spend_usd = today, float(s.get("usd", 0))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("state dir %s unusable (%s); spend and request log kept in memory/stdout only",
                        self.state_dir, e)
            self.writable = False

    def get(self, block_id: str) -> BlockState:
        return self.blocks.setdefault(block_id, BlockState())

    # health --------------------------------------------------------------

    def success(self, block: Block) -> None:
        s = self.get(block.id)
        s.failures, s.cooldown_until = 0, 0.0
        s.last_ok_at = time.time()
        s.ok_count += 1

    def failure(self, block: Block, message: str) -> None:
        s = self.get(block.id)
        s.failures += 1
        s.error_count += 1
        s.last_error, s.last_error_at = message, time.time()
        if s.failures >= block.cooldown.after_failures:
            s.cooldown_until = time.time() + block.cooldown.seconds

    def probe_ok(self, block: Block) -> None:
        s = self.get(block.id)
        s.failures, s.cooldown_until = 0, 0.0

    # spend ---------------------------------------------------------------

    def spend_today(self, block: Block) -> float:
        s = self.get(block.id)
        return s.spend_usd if s.spend_day == _today() else 0.0

    def over_cap(self, block: Block) -> bool:
        cap = block.spend_cap_usd_per_day
        return cap is not None and self.spend_today(block) >= cap

    def add_spend(self, block: Block, usd: float) -> None:
        if usd <= 0:
            return
        s = self.get(block.id)
        today = _today()
        if s.spend_day != today:
            s.spend_day, s.spend_usd = today, 0.0
        s.spend_usd += usd
        self._save_spend()

    def _save_spend(self) -> None:
        if not self.writable:
            return
        data = {bid: {"day": s.spend_day, "usd": round(s.spend_usd, 6)}
                for bid, s in self.blocks.items() if s.spend_day}
        try:
            tmp = self.state_dir / "spend.json.tmp"
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self.state_dir / "spend.json")
        except OSError as e:
            log.warning("could not save spend.json: %s", e)

    # request log -----------------------------------------------------------

    def log_request(self, entry: dict) -> None:
        line = json.dumps(entry, separators=(",", ":"))
        print(line, file=sys.stdout, flush=True)
        if not self.writable:
            return
        try:
            with open(self.state_dir / "requests.jsonl", "a") as f:
                f.write(line + "\n")
        except OSError as e:
            log.warning("could not append requests.jsonl: %s", e)

    # capture switches set from the inspector page ---------------------------

    def load_capture_overrides(self) -> dict:
        try:
            data = json.loads((self.state_dir / "capture.json").read_text())
        except (OSError, ValueError):
            return {"all": False, "clients": {}}
        clients = data.get("clients") if isinstance(data.get("clients"), dict) else {}
        return {"all": bool(data.get("all")), "clients": {str(k): bool(v) for k, v in clients.items()}}

    def save_capture_overrides(self, overrides: dict) -> None:
        if not self.writable:
            return
        try:
            tmp = self.state_dir / "capture.json.tmp"
            tmp.write_text(json.dumps(overrides, indent=2))
            tmp.replace(self.state_dir / "capture.json")
        except OSError as e:
            log.warning("could not save capture.json: %s", e)

    def tail_requests(self, n: int) -> list[dict]:
        """The last n entries of requests.jsonl (to refill the inspector after a restart)."""
        try:
            with open(self.state_dir / "requests.jsonl", "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - n * 4096))
                lines = f.read().splitlines()[-n:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and "id" in e:
                out.append(e)
        return out

    def effective_context(self, block: Block) -> int | None:
        if isinstance(block.max_context_tokens, int):
            return block.max_context_tokens
        if block.max_context_tokens == "auto":
            return self.get(block.id).ctx_detected or block.context_fallback
        return None

    def context_source(self, block: Block) -> str | None:
        if isinstance(block.max_context_tokens, int):
            return "config"
        if block.max_context_tokens == "auto":
            s = self.get(block.id)
            if s.ctx_detected:
                return s.ctx_source
            return "context_fallback" if block.context_fallback else None
        return None

    def snapshot(self, block: Block) -> dict:
        s = self.get(block.id)
        d = asdict(s)
        d["context_tokens"] = self.effective_context(block)
        d["context_source"] = self.context_source(block)
        for k in ("ctx_detected", "ctx_source", "ctx_detected_at"):
            del d[k]
        d["cooling_down"] = s.cooling()
        d["cooldown_remaining_s"] = max(0, round(s.cooldown_until - time.time(), 1))
        d["spend_usd_today"] = round(self.spend_today(block), 4)
        del d["spend_day"], d["spend_usd"]
        return d


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")
