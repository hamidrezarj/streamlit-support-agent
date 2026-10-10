"""Case memory (agent's own store, separate from the tracker) - SQLite, one row per case.

    facts           {name: {value, quote, turn, at, history: [older values...]}}
                    a correction REPLACES the value; the old one moves to `history`
                    with superseded_at / superseded_by_turn (never silently lost)
    tried           checks the user already did (never suggest them again)
    unknowns, sources_used, last_decision, rejection_notes, user_confirmed_fix
    pending_proposal_id, seen_comment_ids, turns, steps_turn, steps_total

Proposals live in their own table, each bound to ONE case_id with a content hash and
the ticket version it was made for. Everything is written in one transaction after
every step, so a crash at any point resumes from the last completed step.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..data import config as C
from ..tracker.store import now

DB_PATH = C.DATA / "state" / "agent.sqlite"
PROPOSAL_STATUSES = ("pending", "approved", "executed", "failed", "rejected", "stale", "superseded", "edited")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (case_id TEXT PRIMARY KEY, state TEXT NOT NULL, rev INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, hash TEXT NOT NULL, ticket_version INTEGER NOT NULL,
  actions TEXT NOT NULL, status TEXT NOT NULL, origin TEXT NOT NULL, parent_id TEXT,
  created_at TEXT NOT NULL, decided_at TEXT, decision TEXT, results TEXT);
CREATE INDEX IF NOT EXISTS ix_prop_case ON proposals(case_id);
"""


def new_state(case_id: str, ticket_id: str) -> dict:
    return {"case_id": case_id, "ticket_id": ticket_id, "created_at": now(),
            "facts": {}, "tried": [], "unknowns": [], "sources_used": [], "last_decision": None,
            "rejection_notes": [], "user_confirmed_fix": False, "pending_proposal_id": None,
            "seen_comment_ids": [], "turns": 0, "steps_turn": 0, "steps_total": 0, "log": []}


def _same(a, b) -> bool:
    """Equal ignoring case/spaces; for versions, equal version numbers ('v1.13.0' == '1.13.0')."""
    a, b = str(a).strip().lower(), str(b).strip().lower()
    if a == b:
        return True
    va, vb = re.findall(r"\d+\.\d+(?:\.\d+)?", a), re.findall(r"\d+\.\d+(?:\.\d+)?", b)
    return bool(va) and va == vb and len(va) == 1 and len(a) < 40 and len(b) < 40


def set_fact(state: dict, name: str, value, quote: str | None, turn: int) -> str:
    """Add or correct a fact. Returns 'added' | 'unchanged' | 'superseded'."""
    name = name.strip().lower().replace(" ", "_")
    cur = state["facts"].get(name)
    if cur and _same(cur["value"], value):
        return "unchanged"
    entry = {"value": value, "quote": quote, "turn": turn, "at": now(), "history": []}
    if cur:
        old = {k: cur[k] for k in ("value", "quote", "turn", "at")}
        entry["history"] = cur.get("history", []) + [{**old, "superseded_at": now(), "superseded_by_turn": turn}]
        state["facts"][name] = entry
        return "superseded"
    state["facts"][name] = entry
    return "added"


def facts_view(state: dict) -> dict:
    return {k: v["value"] for k, v in state["facts"].items()}


def superseded(state: dict) -> list[str]:
    return [f"{k}={h['value']}" for k, v in state["facts"].items() for h in v.get("history", [])]


def proposal_hash(case_id: str, actions: list[dict]) -> str:
    canon = json.dumps({"case_id": case_id, "actions": actions}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


@dataclass
class Proposal:
    id: str
    case_id: str
    hash: str
    ticket_version: int
    actions: list
    status: str = "pending"
    origin: str = "agent"                 # agent | maintainer_edit
    parent_id: str | None = None
    created_at: str = field(default_factory=now)
    decided_at: str | None = None
    decision: dict | None = None
    results: list | None = None

    @staticmethod
    def make(case_id: str, actions: list[dict], ticket_version: int, origin="agent", parent_id=None) -> "Proposal":
        return Proposal(id="p-" + uuid.uuid4().hex[:12], case_id=case_id, hash=proposal_hash(case_id, actions),
                        ticket_version=ticket_version, actions=actions, origin=origin, parent_id=parent_id)

    def comment_text(self) -> str | None:
        return next((a["text"] for a in self.actions if a["op"] == "post_comment"), None)


class CaseStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)

    # ------------------------------------------------------------ cases
    def load(self, case_id: str) -> dict | None:
        r = self.con.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()
        return json.loads(r["state"]) if r else None

    def save(self, state: dict) -> None:
        self.con.execute("BEGIN IMMEDIATE")
        self.con.execute("INSERT INTO cases VALUES (?,?,1,?) ON CONFLICT(case_id) DO UPDATE SET "
                         "state=excluded.state, rev=cases.rev+1, updated_at=excluded.updated_at",
                         (state["case_id"], json.dumps(state, ensure_ascii=False), now()))
        self.con.execute("COMMIT")

    def create(self, case_id: str, ticket_id: str) -> dict:
        st = self.load(case_id)
        if st is None:
            st = new_state(case_id, ticket_id)
            self.save(st)
        return st

    def case_ids(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT case_id FROM cases ORDER BY case_id")]

    # ------------------------------------------------------------ proposals
    def add_proposal(self, p: Proposal) -> None:
        self.con.execute("BEGIN IMMEDIATE")
        self.con.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", self._row(p))
        self.con.execute("COMMIT")

    def update_proposal(self, p: Proposal) -> None:
        self.con.execute("BEGIN IMMEDIATE")
        self.con.execute("UPDATE proposals SET status=?, decided_at=?, decision=?, results=? WHERE id=?",
                         (p.status, p.decided_at, json.dumps(p.decision), json.dumps(p.results), p.id))
        self.con.execute("COMMIT")

    @staticmethod
    def _row(p: Proposal):
        return (p.id, p.case_id, p.hash, p.ticket_version, json.dumps(p.actions, ensure_ascii=False), p.status,
                p.origin, p.parent_id, p.created_at, p.decided_at, json.dumps(p.decision), json.dumps(p.results))

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        r = self.con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if not r:
            return None
        return Proposal(id=r["id"], case_id=r["case_id"], hash=r["hash"], ticket_version=r["ticket_version"],
                        actions=json.loads(r["actions"]), status=r["status"], origin=r["origin"],
                        parent_id=r["parent_id"], created_at=r["created_at"], decided_at=r["decided_at"],
                        decision=json.loads(r["decision"]) if r["decision"] else None,
                        results=json.loads(r["results"]) if r["results"] else None)

    def proposals(self, case_id: str) -> list[Proposal]:
        return [self.get_proposal(r[0]) for r in
                self.con.execute("SELECT id FROM proposals WHERE case_id=? ORDER BY created_at, rowid", (case_id,))]

    def close(self) -> None:
        self.con.close()
