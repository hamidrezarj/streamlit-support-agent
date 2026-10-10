"""Local tracker (SQLite): tickets, comments, labels, status. Resettable.

It stands in for GitHub - nothing is ever sent to the real repository.

* Every change bumps the ticket `version`. A proposal records the version it was made
  for; if the ticket changed since (e.g. the user wrote again), the proposal is stale.
* Agent-side writes take an `idempotency_key`. The key and the write's result are stored
  in the SAME transaction as the write, so a repeated call (retry, duplicate delivery,
  lost connection) finds the key and returns the stored result: `already_done=True`,
  nothing written twice.
* Fault injection for the scenarios: `fail_before` (error, nothing written) and
  `fail_after_write` (written + committed, but the caller gets an error - a connection
  lost after the action).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..data import config as C

DB_PATH = C.DATA / "tracker" / "tracker.sqlite"
STATUSES = ("open", "awaiting_user", "answered", "escalated", "closed_resolved")
AUTHORS = ("user", "agent", "maintainer")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, issue_number INTEGER, title TEXT NOT NULL, body TEXT NOT NULL, url TEXT,
  status TEXT NOT NULL, labels TEXT NOT NULL, version INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL REFERENCES tickets(id),
  author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL,
  idempotency_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS ops (            -- idempotency ledger: one row per keyed write
  key TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, op TEXT NOT NULL, result TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, at TEXT NOT NULL);
"""


class TrackerError(Exception):
    pass


class NotFound(TrackerError):
    pass


class InvalidInput(TrackerError):
    pass


class ToolUnavailable(TrackerError):
    """The tracker could not be reached / the call failed. The write may or may not have
    happened - callers must retry with the SAME idempotency key, never assume success."""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Tracker:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, isolation_level=None)   # explicit transactions
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.faults: dict[str, list[str]] = {}

    # ------------------------------------------------------------ faults
    def inject_fault(self, op: str, mode: str, times: int = 1) -> None:
        assert mode in ("fail_before", "fail_after_write")
        self.faults.setdefault(op, []).extend([mode] * times)

    def _fault(self, op: str) -> str | None:
        q = self.faults.get(op)
        return q.pop(0) if q else None

    # ------------------------------------------------------------ reads
    def _ticket_row(self, ticket_id: str) -> sqlite3.Row:
        row = self.con.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if row is None:
            raise NotFound(f"ticket {ticket_id!r} not found")
        return row

    def get(self, ticket_id: str) -> dict:
        r = self._ticket_row(ticket_id)
        comments = [dict(c) for c in self.con.execute(
            "SELECT id, author, body, created_at, idempotency_key FROM comments WHERE ticket_id=? ORDER BY id",
            (ticket_id,))]
        return {"id": r["id"], "issue_number": r["issue_number"], "title": r["title"], "body": r["body"],
                "url": r["url"], "status": r["status"], "labels": json.loads(r["labels"]),
                "version": r["version"], "created_at": r["created_at"], "updated_at": r["updated_at"],
                "comments": comments}

    def version(self, ticket_id: str) -> int:
        return self._ticket_row(ticket_id)["version"]

    def find_op(self, key: str) -> dict | None:
        r = self.con.execute("SELECT result FROM ops WHERE key=?", (key,)).fetchone()
        return json.loads(r["result"]) if r else None

    def events(self, ticket_id: str) -> list[dict]:
        return [{"kind": e["kind"], "payload": json.loads(e["payload"]), "at": e["at"]}
                for e in self.con.execute("SELECT * FROM events WHERE ticket_id=? ORDER BY id", (ticket_id,))]

    # ------------------------------------------------------------ writes
    def create_ticket(self, ticket_id: str, title: str, body: str, url: str | None = None,
                      issue_number: int | None = None, created_at: str | None = None) -> dict:
        try:
            self._ticket_row(ticket_id)
            return self.get(ticket_id)                      # creating twice is a no-op
        except NotFound:
            pass
        t = created_at or now()
        self.con.execute("BEGIN IMMEDIATE")
        self.con.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (ticket_id, issue_number, title, body, url, "open", "[]", 1, t, t))
        self._event(ticket_id, "created", {"title": title})
        self.con.execute("COMMIT")
        return self.get(ticket_id)

    def _event(self, ticket_id: str, kind: str, payload: dict) -> None:
        self.con.execute("INSERT INTO events (ticket_id, kind, payload, at) VALUES (?,?,?,?)",
                         (ticket_id, kind, json.dumps(payload, ensure_ascii=False), now()))

    def _bump(self, ticket_id: str) -> int:
        self.con.execute("UPDATE tickets SET version = version + 1, updated_at=? WHERE id=?", (now(), ticket_id))
        return self.version(ticket_id)

    def _keyed_write(self, op: str, ticket_id: str, key: str | None, do, fault_op: str | None = None) -> dict:
        """Run `do()` inside one transaction together with the idempotency record."""
        self._ticket_row(ticket_id)                          # NotFound before anything else
        if key:
            prev = self.find_op(key)
            if prev is not None:
                return {**prev, "already_done": True}
        fault = self._fault(fault_op or op)
        if fault == "fail_before":
            raise ToolUnavailable(f"{op}: tracker unavailable (nothing written)")
        self.con.execute("BEGIN IMMEDIATE")
        try:
            result = do()
            result["version"] = self._bump(ticket_id)
            if key:
                self.con.execute("INSERT INTO ops VALUES (?,?,?,?,?)",
                                 (key, ticket_id, op, json.dumps(result, ensure_ascii=False), now()))
            self._event(ticket_id, op, {**result, "key": key})
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        if fault == "fail_after_write":
            raise ToolUnavailable(f"{op}: connection lost after the write")
        return {**result, "already_done": False}

    def add_comment(self, ticket_id: str, author: str, body: str, idempotency_key: str | None = None,
                    created_at: str | None = None) -> dict:
        if author not in AUTHORS:
            raise InvalidInput(f"author must be one of {AUTHORS}")
        if not body or not body.strip():
            raise InvalidInput("empty comment")

        def do():
            cur = self.con.execute(
                "INSERT INTO comments (ticket_id, author, body, created_at, idempotency_key) VALUES (?,?,?,?,?)",
                (ticket_id, author, body, created_at or now(), idempotency_key))
            return {"op": "add_comment", "comment_id": cur.lastrowid, "author": author}
        # faults for agent posts are injected as "post_comment" (user comments are never faulted)
        return self._keyed_write("add_comment", ticket_id, idempotency_key, do,
                                 fault_op="post_comment" if author == "agent" else "add_comment")

    def set_status(self, ticket_id: str, status: str, idempotency_key: str | None = None) -> dict:
        if status not in STATUSES:
            raise InvalidInput(f"status must be one of {STATUSES}")

        def do():
            self.con.execute("UPDATE tickets SET status=? WHERE id=?", (status, ticket_id))
            return {"op": "set_status", "status": status}
        return self._keyed_write("set_status", ticket_id, idempotency_key, do)

    def set_labels(self, ticket_id: str, add=(), remove=(), idempotency_key: str | None = None) -> dict:
        def do():
            cur = json.loads(self._ticket_row(ticket_id)["labels"])
            new = sorted((set(cur) | set(add)) - set(remove))
            self.con.execute("UPDATE tickets SET labels=? WHERE id=?", (json.dumps(new), ticket_id))
            return {"op": "set_labels", "labels": new}
        return self._keyed_write("set_labels", ticket_id, idempotency_key, do)

    # ------------------------------------------------------------ admin
    def reset(self) -> None:
        self.con.execute("BEGIN IMMEDIATE")
        for t in ("comments", "ops", "events", "tickets"):
            self.con.execute(f"DELETE FROM {t}")
        self.con.execute("COMMIT")
        self.faults.clear()

    def close(self) -> None:
        self.con.close()
