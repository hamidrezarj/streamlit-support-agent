"""Typed tools + the maintainer gate.

Agent-side tools (Toolbox)                 Maintainer-side (MaintainerGate) - the ONLY path to act
  read_case(case_id)                          approve(case_id, proposal_id, proposal_hash)
  search_evidence(query, filters)             edit(case_id, proposal_id, proposal_hash, new_text)
  propose_actions(case_id, actions)           reject(case_id, proposal_id, reason)
                                              (gated tracker writes happen only inside the gate)

Approval rules, enforced here in code (nothing an LLM writes and no text in an issue can
create an approval - the agent has no method that sets a proposal to approved):
  * the proposal must belong to the same case_id               else NotApproved
  * the hash the maintainer approved must equal the stored one  else StaleProposal
  * the ticket must be unchanged since the proposal was made    else StaleProposal (marked stale)
  * rejected / edited / superseded proposals can never run      NotApproved / StaleProposal
  * an already executed proposal is never run again: a duplicate delivery returns the stored
    results (already_done) - and every write carries the key "<proposal_id>:<i>", so even a
    retry after a lost connection cannot post twice
  * status closed_resolved only after the user confirmed the fix   else InvalidAction
After executing, the gate reads the tracker back; only verified actions count as done,
and a failure is recorded as failed - never as success.
"""
from __future__ import annotations

import re

from ..tracker.store import STATUSES, InvalidInput, NotFound as TrackerNotFound, ToolUnavailable, Tracker, now
from .audit import AuditLog
from .state import CaseStore, Proposal


# ------------------------------------------------------------------ errors
class ToolError(Exception):
    code = "tool_error"


class NotFound(ToolError):
    code = "not_found"


class NotApproved(ToolError):
    code = "not_approved"


class StaleProposal(ToolError):
    code = "stale_proposal"


class StepLimit(ToolError):
    code = "step_limit"


class InvalidAction(ToolError):
    code = "invalid_action"


LIMITS = {"steps_per_turn": 6, "steps_per_case": 30}
OPS = ("post_comment", "set_status", "set_labels")
LABEL_RX = re.compile(r"^[a-z0-9][a-z0-9:_\-]{0,39}$")
MAX_COMMENT = 6000

TOOL_SPECS = [  # documentation, also printed by the CLI
    {"name": "read_case", "gated": False, "input": "case_id", "output": "ticket, comments, version, status, labels",
     "errors": ["NotFound", "StepLimit"]},
    {"name": "search_evidence", "gated": False, "input": "query, filters", "output": "ranked sources",
     "errors": ["ToolUnavailable", "StepLimit"]},
    {"name": "propose_actions", "gated": False, "input": "case_id, actions[]", "output": "Proposal(id, hash)",
     "errors": ["InvalidAction", "NotFound", "StepLimit"]},
    {"name": "post_comment", "gated": True, "input": "case_id, text (+ approved proposal)", "output": "comment_id",
     "errors": ["NotApproved", "StaleProposal", "ToolUnavailable"]},
    {"name": "set_status", "gated": True, "input": f"case_id, status in {STATUSES}", "output": "new status",
     "errors": ["NotApproved", "StaleProposal", "InvalidAction", "ToolUnavailable"]},
    {"name": "set_labels", "gated": True, "input": "case_id, add[], remove[]", "output": "labels",
     "errors": ["NotApproved", "StaleProposal", "InvalidAction", "ToolUnavailable"]},
]


# ------------------------------------------------------------------ agent-side tools
class Toolbox:
    def __init__(self, tracker: Tracker, store: CaseStore, audit: AuditLog, retriever=None, limits: dict | None = None):
        self.tracker, self.store, self.audit, self.retriever = tracker, store, audit, retriever
        self.limits = {**LIMITS, **(limits or {})}

    def _state(self, case_id: str) -> dict:
        st = self.store.load(case_id)
        if st is None:
            raise NotFound(f"case {case_id!r} not open")
        return st

    def charge(self, case_id: str, what: str, n: int = 1) -> dict:
        """Count a step (LLM call or tool call). Raises StepLimit BEFORE the step runs."""
        st = self._state(case_id)
        if st["steps_turn"] + n > self.limits["steps_per_turn"] or st["steps_total"] + n > self.limits["steps_per_case"]:
            self.audit.write("step_limit", case_id, what=what, steps_turn=st["steps_turn"], steps_total=st["steps_total"],
                             limits=self.limits)
            raise StepLimit(f"step budget exhausted before {what} "
                            f"(turn {st['steps_turn']}/{self.limits['steps_per_turn']}, "
                            f"case {st['steps_total']}/{self.limits['steps_per_case']})")
        st["steps_turn"] += n
        st["steps_total"] += n
        self.store.save(st)
        return st

    def read_case(self, case_id: str) -> dict:
        st = self.charge(case_id, "read_case")
        try:
            t = self.tracker.get(st["ticket_id"])
        except TrackerNotFound as e:
            raise NotFound(str(e))
        self.audit.write("tool_call", case_id, tool="read_case", version=t["version"], n_comments=len(t["comments"]))
        return t

    def search_evidence(self, case_id: str, query: str, filters, k: int = 10, **kw) -> list:
        self.charge(case_id, "search_evidence")
        if self.retriever is None:
            raise ToolUnavailable("no retriever configured")
        hits = self.retriever.search(query, filters, k=k, **kw)
        self.audit.write("tool_call", case_id, tool="search_evidence", query=query[:200], n=len(hits))
        return hits

    def validate_actions(self, st: dict, actions: list[dict]) -> list[dict]:
        if not actions:
            raise InvalidAction("no actions")
        seen, clean = set(), []
        for a in actions:
            op = a.get("op")
            if op not in OPS:
                raise InvalidAction(f"unknown op {op!r}")
            if op in seen:
                raise InvalidAction(f"duplicate op {op}")
            seen.add(op)
            if op == "post_comment":
                text = (a.get("text") or "").strip()
                if not text or len(text) > MAX_COMMENT:
                    raise InvalidAction("comment empty or too long")
                clean.append({"op": op, "text": text})
            elif op == "set_status":
                if a.get("status") not in STATUSES:
                    raise InvalidAction(f"status must be one of {STATUSES}")
                if a["status"] == "closed_resolved" and not st.get("user_confirmed_fix"):
                    raise InvalidAction("closed_resolved needs a user confirmation that the fix works")
                clean.append({"op": op, "status": a["status"]})
            else:
                add, rem = sorted(set(a.get("add") or [])), sorted(set(a.get("remove") or []))
                bad = [l for l in add + rem if not LABEL_RX.match(l)]
                if bad or not (add or rem):
                    raise InvalidAction(f"bad labels {bad or 'none'}")
                clean.append({"op": op, "add": add, "remove": rem})
        return clean

    def propose_actions(self, case_id: str, actions: list[dict], origin: str = "agent",
                        parent_id: str | None = None, charge: bool = True) -> Proposal:
        st = self.charge(case_id, "propose_actions") if charge else self._state(case_id)
        actions = self.validate_actions(st, actions)
        version = self.tracker.version(st["ticket_id"])
        # an older pending proposal of the same case can never run any more
        for old in self.store.proposals(case_id):
            if old.status == "pending":
                old.status, old.decided_at = "superseded", now()
                self.store.update_proposal(old)
                self.audit.write("state_change", case_id, proposal=old.id, status="superseded")
        p = Proposal.make(case_id, actions, version, origin=origin, parent_id=parent_id)
        self.store.add_proposal(p)
        st = self._state(case_id)
        st["pending_proposal_id"] = p.id
        self.store.save(st)
        self.audit.write("proposal", case_id, proposal=p.id, hash=p.hash, ticket_version=version,
                         origin=origin, actions=[{k: (v[:200] if isinstance(v, str) else v) for k, v in a.items()}
                                                 for a in actions])
        return p


# ------------------------------------------------------------------ maintainer gate
class MaintainerGate:
    """The only code path that can approve, edit, reject - and therefore write to the tracker."""

    def __init__(self, toolbox: Toolbox):
        self.tb = toolbox
        self.tracker, self.store, self.audit = toolbox.tracker, toolbox.store, toolbox.audit

    def pending(self, case_id: str) -> Proposal | None:
        st = self.store.load(case_id)
        pid = st and st.get("pending_proposal_id")
        p = self.store.get_proposal(pid) if pid else None
        return p if p and p.status in ("pending", "failed") else None

    def _get(self, case_id: str, proposal_id: str, proposal_hash: str) -> Proposal:
        p = self.store.get_proposal(proposal_id)
        if p is None:
            raise NotFound(f"proposal {proposal_id!r} not found")
        if p.case_id != case_id:
            self.audit.write("rejected_call", case_id, reason="proposal belongs to another case",
                             proposal=proposal_id, proposal_case=p.case_id)
            raise NotApproved(f"proposal {proposal_id} belongs to case {p.case_id}, not {case_id}")
        if proposal_hash != p.hash:
            self.audit.write("rejected_call", case_id, reason="hash mismatch", proposal=proposal_id)
            raise StaleProposal("the approved content differs from the stored proposal (hash mismatch)")
        return p

    def _check_runnable(self, case_id: str, p: Proposal) -> None:
        if p.status == "rejected":
            raise NotApproved(f"proposal {p.id} was rejected")
        if p.status in ("stale", "superseded", "edited"):
            raise StaleProposal(f"proposal {p.id} is {p.status}")
        if p.status in ("pending", "approved"):
            st = self.store.load(case_id)
            v = self.tracker.version(st["ticket_id"])
            if v != p.ticket_version:
                p.status, p.decided_at = "stale", now()
                self.store.update_proposal(p)
                self.audit.write("state_change", case_id, proposal=p.id, status="stale",
                                 reason=f"ticket changed (version {p.ticket_version} -> {v})")
                raise StaleProposal(f"the case changed since proposal {p.id} was made "
                                    f"(version {p.ticket_version} -> {v}); a new proposal is needed")

    def approve(self, case_id: str, proposal_id: str, proposal_hash: str, approver: str = "maintainer") -> dict:
        p = self._get(case_id, proposal_id, proposal_hash)
        if p.status == "executed":                         # duplicate delivery / repeated request
            self.audit.write("duplicate_request", case_id, proposal=p.id)
            return {"ok": True, "proposal_id": p.id, "already_done": True, "results": p.results}
        self._check_runnable(case_id, p)
        p.status, p.decided_at = "approved", now()
        p.decision = {"action": "approve", "by": approver, "hash": proposal_hash, "at": p.decided_at}
        self.store.update_proposal(p)
        self.audit.write("approval", case_id, proposal=p.id, by=approver, hash=proposal_hash)
        return self._execute(case_id, p)

    def edit(self, case_id: str, proposal_id: str, proposal_hash: str, new_text: str,
             approver: str = "maintainer") -> dict:
        orig = self._get(case_id, proposal_id, proposal_hash)
        self._check_runnable(case_id, orig)
        if orig.status != "pending":
            raise NotApproved(f"only a pending proposal can be edited (status {orig.status})")
        actions = [dict(a) for a in orig.actions]
        if any(a["op"] == "post_comment" for a in actions):
            for a in actions:
                if a["op"] == "post_comment":
                    a["text"] = new_text
        else:
            actions.insert(0, {"op": "post_comment", "text": new_text})
        orig.status, orig.decided_at = "edited", now()
        orig.decision = {"action": "edit", "by": approver, "hash": proposal_hash, "at": orig.decided_at}
        self.store.update_proposal(orig)
        new = self.tb.propose_actions(case_id, actions, origin="maintainer_edit", parent_id=orig.id, charge=False)
        new.status, new.decided_at = "approved", now()
        new.decision = {"action": "approve", "by": approver, "hash": new.hash, "edited_from": orig.id, "at": new.decided_at}
        self.store.update_proposal(new)
        self.audit.write("approval", case_id, proposal=new.id, by=approver, hash=new.hash, edited_from=orig.id)
        return self._execute(case_id, new)

    def reject(self, case_id: str, proposal_id: str, proposal_hash: str, reason: str,
               approver: str = "maintainer") -> dict:
        p = self._get(case_id, proposal_id, proposal_hash)
        if p.status not in ("pending", "failed"):
            raise NotApproved(f"proposal {p.id} cannot be rejected (status {p.status})")
        p.status, p.decided_at = "rejected", now()
        p.decision = {"action": "reject", "by": approver, "reason": reason, "at": p.decided_at}
        self.store.update_proposal(p)
        st = self.store.load(case_id)
        st["rejection_notes"].append({"proposal_id": p.id, "reason": reason, "at": p.decided_at})
        st["pending_proposal_id"] = None
        self.store.save(st)
        self.audit.write("rejection", case_id, proposal=p.id, by=approver, reason=reason)
        return {"ok": True, "proposal_id": p.id, "rejected": True}

    # ------------------------------------------------------------ execution
    def _do(self, ticket_id: str, a: dict, key: str) -> dict:
        if a["op"] == "post_comment":
            return self.tracker.add_comment(ticket_id, "agent", a["text"], idempotency_key=key)
        if a["op"] == "set_status":
            return self.tracker.set_status(ticket_id, a["status"], idempotency_key=key)
        return self.tracker.set_labels(ticket_id, add=a.get("add", ()), remove=a.get("remove", ()), idempotency_key=key)

    def _execute(self, case_id: str, p: Proposal) -> dict:
        st = self.store.load(case_id)
        tid = st["ticket_id"]
        results, ok = [], True
        for i, a in enumerate(p.actions):
            key = f"{p.id}:{i}"
            for attempt in (1, 2):                         # one retry, SAME key -> no duplicates
                try:
                    r = self._do(tid, a, key)
                    results.append({"op": a["op"], "key": key, "attempts": attempt, **r})
                    self.audit.write("tool_call", case_id, tool=a["op"], key=key, attempt=attempt,
                                     already_done=r.get("already_done"))
                    break
                except (ToolUnavailable, InvalidInput) as e:
                    self.audit.write("tool_error", case_id, tool=a["op"], key=key, attempt=attempt, error=str(e))
                    if attempt == 2 or isinstance(e, InvalidInput):
                        results.append({"op": a["op"], "key": key, "attempts": attempt, "error": str(e)})
                        ok = False
            if not ok:
                break                                       # never set a status after a failed comment
        # verify by reading the tracker back
        try:
            t = self.tracker.get(tid)
            keys = {c["idempotency_key"] for c in t["comments"] if c["idempotency_key"]}
            for r, a in zip(results, p.actions):
                if "error" in r:
                    r["verified"] = False
                elif a["op"] == "post_comment":
                    r["verified"] = r["key"] in keys
                elif a["op"] == "set_status":
                    r["verified"] = t["status"] == a["status"] or self.tracker.find_op(r["key"]) is not None
                else:
                    r["verified"] = set(a.get("add", ())) <= set(t["labels"]) or self.tracker.find_op(r["key"]) is not None
        except Exception as e:                             # cannot verify -> cannot claim success
            self.audit.write("tool_error", case_id, tool="verify", error=str(e))
            for r in results:
                r.setdefault("verified", False)
        ok = ok and len(results) == len(p.actions) and all(r.get("verified") for r in results)
        p.status, p.results = ("executed" if ok else "failed"), results
        self.store.update_proposal(p)
        st = self.store.load(case_id)
        if ok:
            st["pending_proposal_id"] = None
        st["log"].append({"at": now(), "proposal": p.id, "status": p.status})
        self.store.save(st)
        self.audit.write("state_change", case_id, proposal=p.id, status=p.status,
                         results=[{k: r.get(k) for k in ("op", "key", "verified", "already_done", "error")} for r in results])
        return {"ok": ok, "proposal_id": p.id, "already_done": False, "results": results}
