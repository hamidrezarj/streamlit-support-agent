"""Append-only audit log (JSONL): decisions, tool calls, errors, state changes, approvals.

Secrets never reach the log: values of known secret env vars are masked before writing.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from ..data import config as C
from ..tracker.store import now

AUDIT_DIR = C.DATA / "audit"
SECRET_ENV = ("LLM_API_KEY", "GITHUB_TOKEN", "CHAT_API_KEY", "EMBED_API_KEY")


class AuditLog:
    def __init__(self, path: Path | str | None = None, run_id: str | None = None):
        self.run_id = run_id or "run-" + uuid.uuid4().hex[:8]
        self.path = Path(path or AUDIT_DIR / f"{self.run_id}.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._secrets = [v for k in SECRET_ENV if len(v := os.environ.get(k, "")) >= 8]

    def write(self, kind: str, case_id: str | None = None, **data) -> None:
        line = json.dumps({"at": now(), "run": self.run_id, "case": case_id, "kind": kind, **data},
                          ensure_ascii=False, default=str)
        for s in self._secrets:
            line = line.replace(s, "***")
        with self.path.open("a") as f:
            f.write(line + "\n")

    def read(self, case_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = [json.loads(l) for l in self.path.open() if l.strip()]
        return [r for r in rows if case_id is None or r.get("case") == case_id]
