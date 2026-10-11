"""Everything the demo UI does, without Streamlit (so it is testable and reusable).

Env:  SSA_UI_FAKE=1   scripted LLM + tiny index (offline demo / tests, $0)
      SSA_UI_DATA=dir where the UI's own tracker / memory / audit live (default data/ui/)
The UI never touches the evaluation runs; it has its own tracker and case memory.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..data import config as C
from ..data.io_utils import read_json
from ..eval.p1 import PUBLIC_KEYS

FAKE = os.environ.get("SSA_UI_FAKE") == "1"
P1_SYSTEMS = ("final", "baseline", "baseline-v2")
P1_VERSIONS = [("baseline-v1", "p1_baseline_baseline-v1_dev.json"), ("baseline-v2", "p1_baseline-v2_baseline-v2_dev.json"),
               ("final-v2", "p1_final_final-v2_dev.json"), ("final-v3", "p1_final_final-v3_dev.json"),
               ("final-v4", "p1_final_final-v4_dev.json")]


def ui_dir() -> Path:
    d = Path(os.environ.get("SSA_UI_DATA") or C.DATA / "ui")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ shared resources
def make_llm():
    if FAKE:
        from .fakes import fake_llm
        return fake_llm()
    from ..llm import get_llm
    return get_llm()


def make_retriever():
    if FAKE:
        from .fakes import FakeRetr
        return FakeRetr()
    from ..index.search import Retriever
    return Retriever(mode="hybrid")


def dev_cases() -> list[dict]:
    """Dev cases only: the test split stays untouched until the final run."""
    return [read_json(p) for p in sorted((C.DATA / "eval" / "cases" / "dev").glob("*.json"))]


def find_case(cid: str) -> dict:
    return read_json(C.DATA / "eval" / "cases" / "dev" / f"{cid}.json")


def public(case: dict) -> dict:
    return {k: case[k] for k in PUBLIC_KEYS}


def new_report(title: str, body: str, as_of: str | None) -> dict:
    from ..cli import new_report as nr
    return nr(title, body, as_of)


# ------------------------------------------------------------------ Problem 1
def p1_system(name: str, llm, retriever):
    if name == "final":
        from ..agent.pipeline import FinalP1
        return FinalP1(llm, retriever=retriever)
    from ..agent.baseline import Baseline, BaselineV2
    return (BaselineV2 if name == "baseline-v2" else Baseline)(llm, retriever=retriever)


def analyse(name: str, pub: dict, llm, retriever) -> dict:
    llm.reset_stats()
    res = p1_system(name, llm, retriever).run(pub)
    return {**res, "system": name, "stats": dict(llm.stats)}


def evidence_rows(res: dict) -> list[dict]:
    tr = res.get("trace") or {}
    rel, resolves = tr.get("assessments") or {}, set(tr.get("resolves") or [])
    cited = set((res.get("check") or {}).get("cited", []))
    rows = []
    for lab, s in (res.get("sources") or {}).items():
        rows.append({"S": lab, "relation": rel.get(lab, "-" if not rel else "unassessed"),
                     "resolves": lab in resolves, "maintainer reply": bool(s.get("maintainer")),
                     "cited": lab in cited, "type": s["type"], "title": s["title"][:70],
                     "date": (s.get("available_at") or "docs")[:10], "url": s["url"]})
    return rows


def gold_verdict(case: dict, outcome: str | None) -> str:
    g = case["gold"]
    acc = g.get("acceptable_outcomes") or [g["outcome"]]
    return "match" if outcome == g["outcome"] else "acceptable" if outcome in acc else "mismatch"


# ------------------------------------------------------------------ Problem 2
class World:
    """The UI's own 'process': tracker + case memory + agent + maintainer gate."""

    def __init__(self, llm, retriever, d: Path | None = None):
        from ..agent.audit import AuditLog
        from ..agent.orchestrator import Agent
        from ..agent.pipeline import FinalP1
        from ..agent.state import CaseStore
        from ..agent.tools import MaintainerGate, Toolbox
        from ..tracker.store import Tracker
        self.llm, self.retriever, self.dir = llm, retriever, d or ui_dir()
        self.tracker = Tracker(self.dir / "tracker.sqlite")
        self.store = CaseStore(self.dir / "state.sqlite")
        self.audit = AuditLog(self.dir / "audit.jsonl", run_id="ui")
        self.tb = Toolbox(self.tracker, self.store, self.audit, retriever)
        excl = read_json(C.DATA / "eval" / "eval_exclusions.json")["exclude_issues"]
        self.agent = Agent(self.tb, FinalP1(llm, retriever=retriever), llm, excl)
        self.gate = MaintainerGate(self.tb)

    def close(self):
        self.tracker.close()
        self.store.close()

    def restart(self) -> "World":
        """Simulate a crash + restart: drop every in-memory object, rebuild from the files."""
        self.close()
        return World(self.llm, self.retriever, self.dir)

    def reset(self):
        self.tracker.reset()
        self.store.con.executescript("DELETE FROM cases; DELETE FROM proposals;")

    def cases(self) -> list[str]:
        return self.store.case_ids()

    def open_eval_case(self, cid: str) -> str:
        v = find_case(cid)["visible"]
        self.agent.open_case(cid, v["title"], v["body"], created_at=v["as_of"], url=v.get("url"),
                             issue_number=v.get("issue_number"))
        return cid

    def open_new(self, title: str, body: str, as_of: str | None) -> str:
        pub = new_report(title, body, as_of)
        v = pub["visible"]
        self.agent.open_case(pub["case_id"], v["title"], v["body"], created_at=v["as_of"])
        return pub["case_id"]

    def say(self, cid: str, text: str):
        t = self.tracker.get(cid)
        from ..eval.p2 import _iso_plus
        from ..agent.orchestrator import case_as_of
        last = case_as_of(t)                                   # agent posts carry the wall clock
        return self.agent.user_says(cid, text, created_at=_iso_plus(last))    # keep the case's own timeline

    def turn(self, cid: str) -> dict:
        self.llm.reset_stats()
        r = self.agent.turn(cid)
        return {**r, "stats": dict(self.llm.stats)}

    def pending(self, cid: str):
        return self.gate.pending(cid)

    def fail_next_post(self):
        self.tracker.inject_fault("post_comment", "fail_after_write", 1)

    def snapshot(self, cid: str) -> dict:
        from ..agent.state import facts_view, superseded
        t = self.tracker.get(cid)
        s = self.store.load(cid)
        props = [{"id": p.id, "status": p.status, "origin": p.origin, "hash": p.hash[:12],
                  "ticket version": p.ticket_version, "created": p.created_at} for p in self.store.proposals(cid)]
        audit = [{"at": r["at"][11:19], "kind": r["kind"],
                  "detail": {k: v for k, v in r.items() if k not in ("at", "run", "case", "kind")}}
                 for r in self.audit.read(cid)][-12:]
        return {"ticket": t, "facts": facts_view(s), "superseded": superseded(s), "tried": s["tried"],
                "unknowns": s["unknowns"], "confirmed": s["user_confirmed_fix"], "last_decision": s["last_decision"],
                "rejections": [n["reason"] for n in s["rejection_notes"]], "turns": s["turns"],
                "steps_total": s["steps_total"], "proposals": props, "audit": audit}


# ------------------------------------------------------------------ results (read-only)
def p1_results() -> tuple[list[dict], dict]:
    H = C.DATA / "eval" / "runs" / "history"
    rows, runs = [], {}
    for name, f in P1_VERSIONS:
        p = H / f
        if not p.exists():
            continue
        r = read_json(p)
        runs[name] = r
        m = r["metrics"]
        rows.append({"version": name, "outcome exact": m["outcome_exact"], "acceptable": m.get("outcome_acceptable"),
                     "answerable": m["per_bucket"].get("answerable", {}).get("outcome_exact"),
                     "ambiguous": m["per_bucket"].get("ambiguous", {}).get("outcome_exact"),
                     "escalate": m["per_bucket"].get("escalate", {}).get("outcome_exact"),
                     "retrieval recall": m["retrieval_recall"], "high-conf wrong": m["high_confidence_wrong"],
                     "$ / case": m["cost"]["usd_per_case"]})
    return rows, runs


def p1_case_rows(runs: dict) -> list[dict]:
    if "final-v4" not in runs:
        return []
    out = []
    for c in runs["final-v4"]["cases"]:
        row = {"case": c["case_id"], "gold": c["score"]["gold_outcome"]}
        for name in ("baseline-v1", "baseline-v2", "final-v4"):
            pc = next((x for x in runs.get(name, {}).get("cases", []) if x["case_id"] == c["case_id"]), None)
            if pc:
                s = pc["score"]
                row[name] = s["outcome"] + (" ✓" if s["outcome_exact"] else " ~" if s.get("outcome_acceptable") else " ✗")
        out.append(row)
    return out


def p2_results() -> list[dict]:
    R = C.DATA / "eval" / "runs"
    out = []
    for p in sorted(R.glob("p2_*_*.json")):
        r = read_json(p)
        m = r["metrics"]
        out.append({"run": p.stem, "system": r["system"], "version": r["version"], "split": r["split"],
                    "scenarios passed": f"{m['scenarios_passed']}/{m['n_scenarios']}",
                    "checks": f"{m['checks_passed']}/{m['checks_total']}",
                    "safety violations": sum(m["violations"].values()) if m["violations"] else 0,
                    "repeated replies": m.get("repeated_replies"),
                    "$ / turn": m["cost"].get("usd_per_turn")})
    return out


def p2_scenario_rows(run_stem: str) -> list[dict]:
    r = read_json(C.DATA / "eval" / "runs" / f"{run_stem}.json")
    return [{"scenario": s["sid"], "pass": s["passed"], "checks": f"{s['n_ok']}/{s['n_checks']}",
             "final status": s["final"]["status"], "comments": len(s["final"]["agent_comments"]),
             "failed checks": "; ".join(n for n, c in s["checks"].items() if not c["ok"])} for s in r["scenarios"]]
