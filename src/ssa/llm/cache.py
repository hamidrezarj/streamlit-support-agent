"""Record/replay cache for chat calls (SQLite, content-addressed).

Key = sha256 of the canonical request (model, messages, tools, tool_choice,
response_format, temperature, max_tokens, seed). The stored value is the raw
response plus the *original* tokens / USD / latency, so a replayed run can
still report what the live run cost - clearly marked as replayed.

Modes (env LLM_CACHE_MODE):
  cache   (default) read-through: hit -> replay, miss -> live call + store
  replay  offline: hit -> replay, miss -> ReplayMiss (no network, no key needed)
  live    always call the API, overwrite the stored entry
  off     no cache at all
The file is committed (see .gitignore) so graders can replay runs for $0.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from ..data import config as C
from ..data.io_utils import utcnow

CACHE_PATH = C.DATA / "llm_cache" / "chat.sqlite"
MODES = ("cache", "replay", "live", "off")


class ReplayMiss(RuntimeError):
    """Replay mode and the exact request was never recorded."""


def request_key(req: dict) -> str:
    canon = json.dumps(req, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


class ChatCache:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or CACHE_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, check_same_thread=False)   # UI may call from several threads
        self.con.execute("""CREATE TABLE IF NOT EXISTS chat (
            key TEXT PRIMARY KEY, model TEXT, request TEXT, response TEXT,
            tokens_in INT, tokens_out INT, usd REAL, latency_s REAL,
            tag TEXT, created_at TEXT)""")
        self.con.commit()

    def get(self, key: str) -> dict | None:
        row = self.con.execute("SELECT response, tokens_in, tokens_out, usd, latency_s, created_at "
                               "FROM chat WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        return {"response": json.loads(row[0]), "tokens_in": row[1], "tokens_out": row[2],
                "usd": row[3], "latency_s": row[4], "recorded_at": row[5]}

    def put(self, key: str, req: dict, response: dict, tokens_in: int, tokens_out: int,
            usd: float, latency_s: float, tag: str = "") -> None:
        self.con.execute("INSERT OR REPLACE INTO chat VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (key, req.get("model"), json.dumps(req, ensure_ascii=False),
                          json.dumps(response, ensure_ascii=False), tokens_in, tokens_out,
                          usd, latency_s, tag, utcnow()))
        self.con.commit()

    def __len__(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM chat").fetchone()[0]
