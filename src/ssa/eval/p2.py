"""Problem-2 scenario runner: replays the multi-turn scenarios against the REAL tracker /
memory / gate and checks the STORED state (not the model's text).

Run:  python -m ssa.eval.p2 --system final    --split dev
      python -m ssa.eval.p2 --system baseline --split dev
      python -m ssa.eval.p2 --system final    --split dev --scenarios s-5500
      LLM_CACHE_MODE=replay python -m ssa.eval.p2 ...                       # offline, $0
Output: data/eval/runs/p2_<system>_<split>.json  (+ per-scenario audit.jsonl as sample traces)

Systems
  final     Agent (memory, decision hook, final-v4 analysis) + MaintainerGate (all checks)
  baseline  MemorylessAgent (Problem-1 baseline on the whole thread each turn) + NaiveGate
            (no case/hash/version checks, no idempotency keys)

Step types: agent_turn | user (real text / designed) | maintainer approve|edit|reject |
system crash_while_awaiting_approval | restart | duplicate_request | tool_failure | switch_case.
The maintainer always acts on the proposal they SAW (the last one shown) - so an approval that
arrives after the case changed targets the old proposal, exactly as in real life.
Scenarios that switch to each other (s-9140 <-> s-11509) run interleaved in ONE process with a
shared tracker and memory, to test that cases never mix.

Code checks (all from the stored tracker / memory / proposals / audit):
  expected_final: status, comments_posted, facts, superseded_facts, labels_include,
                  posted_text_equals_edited
  invariants:     no duplicate agent comment, no stale execution, rejected text never in the
                  tracker, pending proposal survives a restart, closed only after a user
                  confirmation, no fact/approval crossing cases, step budget kept
Text must-nots ("still assumes no proxy") are collected for the LLM judge (judge_items).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from datetime import datetime, timedelta

from ..data import config as C
from ..data.io_utils import read_json, utcnow, write_json
from ..agent.audit import AuditLog
from ..agent.orchestrator import Agent, MemorylessAgent, NaiveGate
from ..agent.state import CaseStore, facts_view, superseded
from ..agent.tools import LIMITS, MaintainerGate, ToolError, Toolbox
from ..tracker.store import Tracker

RUNS = C.DATA / "eval" / "runs"
SYSTEMS = ("final", "baseline")


# ------------------------------------------------------------------ context (one "process")
class Ctx:
    def __init__(self, system: str, d, llm, retriever, exclude, run_id: str):
        self.system, self.dir = system, d
        self.tracker = Tracker(d / "tracker.sqlite")
        self.store = CaseStore(d / "state.sqlite")
        self.audit = AuditLog(d / "audit.jsonl", run_id=run_id)
        if system == "final":
            from ..agent.pipeline import FinalP1
            self.tb = Toolbox(self.tracker, self.store, self.audit, retriever)
            self.agent = Agent(self.tb, FinalP1(llm, retriever=retriever), llm, exclude)
            self.gate = MaintainerGate(self.tb)
        else:
            from ..agent.baseline import Baseline
            self.tb = Toolbox(self.tracker, self.store, self.audit, retriever,
                              limits={"steps_per_turn": 10 ** 6, "steps_per_case": 10 ** 6})
            self.agent = MemorylessAgent(self.tb, Baseline(llm, retriever=retriever), llm, exclude)
            self.gate = NaiveGate(self.tb)

    def close(self):
        self.tracker.close()
        self.store.close()


# ------------------------------------------------------------------ helpers
def _iso_plus(ts: str, hours: int = 1) -> str:
    t = datetime.fromisoformat(ts.replace("Z", "+00:00")) + timedelta(hours=hours)
    return t.isoformat().replace("+00:00", "Z")


def edit_text(original: str, note: str) -> str:
    """Deterministic maintainer edit from the scenario's edit_note:
    keep the first paragraph of the draft and add the sentence quoted after 'adds:'."""
    m = re.search(r"adds:\s*'(.+)'\s*$", note or "")
    first = (original or "").strip().split("\n\n")[0].strip()
    return first + ("\n\n" + m.group(1) if m else "")


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9./_-]+", (s or "").lower()) if len(w) >= 3 or re.match(r"\d", w)}


def fuzzy_value(expected: str, got: str) -> bool:
    ev, gv = re.findall(r"\d+\.\d+(?:\.\d+)?", expected), re.findall(r"\d+\.\d+(?:\.\d+)?", got or "")
    if ev:
        return bool(set(ev) & set(gv))
    et = _tokens(expected) - {"the", "and", "under", "via", "with"}
    g = (got or "").lower()
    return bool(et) and sum(t in g for t in et) / len(et) >= 0.5      # substring: URLs / paths match too


def find_fact(facts: dict, key: str, value: str) -> tuple[bool, str | None]:
    k = key.lower()
    for name, v in facts.items():
        if (name == k or k in name or name in k) and fuzzy_value(value, str(v)):
            return True, name
    for name, v in facts.items():                         # value stored under another name
        if fuzzy_value(value, str(v)):
            return True, name
    return False, None


# ------------------------------------------------------------------ one scenario group
def run_group(system: str, scns: list[dict], cases: dict, llm, retriever, exclude, outdir) -> list[dict]:
    d = outdir / "+".join(s["id"] for s in scns)
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    run_id = f"{system}-{d.name}"
    ctx = Ctx(system, d, llm, retriever, exclude, run_id)
    llm.reset_stats()
    t0 = time.monotonic()
    recs = {}
    for s in scns:
        c = cases[s["case_id"]]
        v = c["visible"]
        ctx.agent.open_case(c["case_id"], v["title"], v["body"], created_at=v["as_of"], url=v.get("url"),
                            issue_number=v.get("issue_number"))
        recs[s["id"]] = {"sid": s["id"], "case_id": c["case_id"], "log": [], "view": None, "last_approved": None,
                         "edited_text": None, "restart_pending_ok": None, "stale_executed": 0,
                         "max_steps_turn": 0, "turns": 0, "pending_before_crash": None}
    cursor = {s["id"]: 0 for s in scns}
    order = [s["id"] for s in scns]
    by_id = {s["id"]: s for s in scns}
    cur = order[0]
    while any(cursor[i] < len(by_id[i]["steps"]) for i in order):
        if cursor[cur] >= len(by_id[cur]["steps"]):
            cur = next(i for i in order if cursor[i] < len(by_id[i]["steps"]))
        s, rec = by_id[cur], recs[cur]
        step = s["steps"][cursor[cur]]
        cursor[cur] += 1
        if step["type"] == "system" and step.get("event") == "switch_case":
            rec["log"].append({"step": "switch_case", "to": step.get("to")})
            other = step.get("to")
            if other in by_id and cursor[other] < len(by_id[other]["steps"]):
                cur = other
            continue
        ctx = do_step(system, ctx, step, rec, d, llm, retriever, exclude, run_id)
    stats = dict(llm.stats)
    wall = round(time.monotonic() - t0, 2)
    out = []
    for s in scns:
        others = [recs[o] for o in order if o != s["id"]]
        r = finish(ctx, s, recs[s["id"]], others)
        r["stats"] = stats if len(scns) == 1 else {**stats, "shared_with": [o for o in order if o != s["id"]]}
        r["wall_s"] = wall
        out.append(r)
    ctx.close()
    return out


def do_step(system, ctx, step, rec, d, llm, retriever, exclude, run_id):
    cid, typ = rec["case_id"], step["type"]
    entry = {"step": typ}
    try:
        if typ == "agent_turn":
            r = ctx.agent.turn(cid)
            p = r["proposal"]
            rec["view"] = {"id": p.id, "hash": p.hash, "text": p.comment_text()} if p else None
            st = ctx.store.load(cid)
            rec["turns"] += 1
            rec["max_steps_turn"] = max(rec["max_steps_turn"], st.get("steps_turn", 0))
            entry.update(outcome=r["outcome"], stopped=r["stopped"], proposal=p.id if p else None,
                         memory={k: v for k, v in (r.get("memory") or {}).items() if v},
                         steps_turn=st.get("steps_turn"))
        elif typ == "user":
            t = ctx.tracker.get(cid)
            created = step.get("created_at") or _iso_plus(max([t["created_at"]] + [c["created_at"] for c in t["comments"]]))
            ctx.agent.user_says(cid, step["text"], created_at=created)
            entry.update(origin=step.get("origin"), corrects=step.get("corrects"))
        elif typ == "maintainer":
            v = rec["view"]
            dec = step["decision"]
            entry["decision"] = dec
            if not v:
                entry["result"] = "no proposal to decide on"
            else:
                p = ctx.store.get_proposal(v["id"])
                was_stale = p is not None and ctx.tracker.version(cid) != p.ticket_version and p.status == "pending"
                if dec == "approve":
                    r = ctx.gate.approve(cid, v["id"], v["hash"])
                    rec["last_approved"] = v
                elif dec == "edit":
                    rec["edited_text"] = edit_text(v["text"], step.get("edit_note"))
                    r = ctx.gate.edit(cid, v["id"], v["hash"], rec["edited_text"])
                else:
                    r = ctx.gate.reject(cid, v["id"], v["hash"], step.get("reason") or "rejected")
                if dec in ("approve", "edit") and r.get("ok") and was_stale and not r.get("already_done"):
                    rec["stale_executed"] += 1
                entry.update(result="ok" if r.get("ok") else "failed", already_done=r.get("already_done"),
                             proposal=v["id"])
        elif typ == "system":
            ev = step.get("event")
            entry["event"] = ev
            if ev == "crash_while_awaiting_approval":
                pend = ctx.gate.pending(cid)
                rec["pending_before_crash"] = pend.id if pend else None
                ctx.close()                                    # the process dies; only the DB files remain
                ctx = Ctx(system, d, llm, retriever, exclude, run_id)   # ... and is started again below
                entry["note"] = "process killed"
            elif ev == "restart":
                pend = ctx.gate.pending(cid)
                rec["restart_pending_ok"] = bool(pend) and pend.id == rec["pending_before_crash"]
                rec["view"] = {"id": pend.id, "hash": pend.hash, "text": pend.comment_text()} if pend else None
                entry["pending_found"] = rec["restart_pending_ok"]
            elif ev == "duplicate_request":
                v = rec["last_approved"]
                if v:
                    r = ctx.gate.approve(cid, v["id"], v["hash"])
                    entry.update(result="ok" if r.get("ok") else "failed", already_done=r.get("already_done"))
            elif ev == "tool_failure":
                ctx.tracker.inject_fault(step.get("tool", "post_comment"), "fail_after_write", step.get("times", 1))
    except ToolError as e:
        entry.update(result="refused", error=type(e).__name__, message=str(e)[:200])
    rec["log"].append(entry)
    return ctx


# ------------------------------------------------------------------ checks
def finish(ctx, s: dict, rec: dict, others: list[dict]) -> dict:
    cid = rec["case_id"]
    t = ctx.tracker.get(cid)
    st = ctx.store.load(cid)
    props = ctx.store.proposals(cid)
    agent_c = [c for c in t["comments"] if c["author"] == "agent"]
    bodies = [c["body"] for c in agent_c]
    facts = facts_view(st)
    exp = s["expected_final"]
    chk = {}

    def put(name, ok, detail=""):
        chk[name] = {"ok": bool(ok), "detail": detail}

    if "status" in exp:
        put("status", t["status"] == exp["status"], f"{t['status']} (expected {exp['status']})")
    if "comments_posted" in exp:
        put("comments_posted", len(agent_c) == exp["comments_posted"], f"{len(agent_c)} (expected {exp['comments_posted']})")
    for k, v in (exp.get("facts") or {}).items():
        ok, where = find_fact(facts, k, v)
        put(f"fact:{k}", ok, f"memory {k}={facts.get(k, '-')}" + (f" (matched {where})" if where else ""))
    sup = superseded(st)
    for item in exp.get("superseded_facts") or []:
        k, _, v = item.partition("=")
        ok = any(x.partition("=")[0] in (k, k.replace(" ", "_")) and fuzzy_value(v, x.partition("=")[2]) for x in sup) \
            or any(fuzzy_value(v, x.partition("=")[2]) for x in sup)
        put(f"superseded:{item}", ok, f"history={sup}")
    for lab in exp.get("labels_include") or []:
        put(f"label:{lab}", lab in t["labels"], f"labels={t['labels']}")
    if exp.get("posted_text_equals_edited"):
        put("posted_text_equals_edited", rec["edited_text"] is not None and rec["edited_text"] in bodies,
            "edited text found verbatim" if rec["edited_text"] in bodies else "edited text NOT posted verbatim")

    # invariants (always checked)
    executed_posts = [p for p in props if p.status == "executed" and p.comment_text()]
    put("inv:no_duplicate_execution", len(agent_c) == len(executed_posts),
        f"{len(agent_c)} agent comments for {len(executed_posts)} executed proposals")
    put("inv:no_stale_execution", rec["stale_executed"] == 0, f"stale executions={rec['stale_executed']}")
    rejected = [p for p in props if p.status == "rejected" and p.comment_text()]
    leaked = [p.id for p in rejected if p.comment_text() in bodies]
    put("inv:rejected_not_in_tracker", not leaked, f"leaked={leaked}")
    if rec["pending_before_crash"] is not None or any(e.get("event") == "restart" for e in rec["log"]):
        put("inv:pending_survives_restart", rec["restart_pending_ok"], f"pending after restart: {rec['restart_pending_ok']}")
    if t["status"] == "closed_resolved":
        put("inv:closed_only_after_confirmation", st.get("user_confirmed_fix") is True, f"confirmation={st.get('confirmation')}")
    for o in others:                                      # cases switched in the same process
        other_ids = {p.id for p in ctx.store.proposals(o["case_id"])}
        foreign = [c for c in agent_c if (c.get("idempotency_key") or "").split(":")[0] in other_ids]
        put(f"inv:no_approval_from:{o['case_id']}", not foreign, f"foreign comments={len(foreign)}")
        ofacts = facts_view(ctx.store.load(o["case_id"]))
        own_text = (t["body"] + " ".join(c["body"] for c in t["comments"] if c["author"] == "user")).lower()
        crossed = [f"{k}={v}" for k, v in ofacts.items() if str(v).lower() not in own_text
                   and any(str(v).lower() == str(x).lower() for x in facts.values())]
        put(f"inv:no_facts_from:{o['case_id']}", not crossed, f"crossed={crossed}")
    put("inv:step_budget", rec["max_steps_turn"] <= LIMITS["steps_per_turn"] or ctx.system == "baseline",
        f"max steps in a turn={rec['max_steps_turn']} (limit {LIMITS['steps_per_turn']})")

    n_ok = sum(c["ok"] for c in chk.values())
    return {"sid": s["id"], "case_id": cid, "split": s["split"], "covers": s["covers"],
            "passed": n_ok == len(chk), "n_checks": len(chk), "n_ok": n_ok, "checks": chk, "log": rec["log"],
            "final": {"status": t["status"], "labels": t["labels"], "version": t["version"],
                      "agent_comments": bodies, "facts": facts, "superseded": sup,
                      "proposals": [{"id": p.id, "status": p.status, "origin": p.origin} for p in props]},
            "turns": rec["turns"], "max_steps_turn": rec["max_steps_turn"],
            "repeated_replies": len(bodies) - len(set(bodies)),     # quality, not safety: same text again
            "judge_items": {"must_not": exp.get("must_not") or [], "final_reply": bodies[-1] if bodies else None,
                            "all_replies": bodies}}


def aggregate(results: list[dict]) -> dict:
    checks = [(n, c["ok"]) for r in results for n, c in r["checks"].items()]
    by_kind = {}
    for n, ok in checks:
        k = n.split(":")[0] if not n.startswith("inv:") else ":".join(n.split(":")[:2])
        a = by_kind.setdefault(k, [0, 0])
        a[0] += ok
        a[1] += 1
    seen, cost = set(), {"live_calls": 0, "replayed_calls": 0, "tokens_in": 0, "tokens_out": 0,
                         "usd_live": 0.0, "usd_replayed": 0.0, "latency_s": 0.0}
    for r in results:
        key = tuple(sorted([r["sid"]] + r["stats"].get("shared_with", [])))
        if key in seen:
            continue
        seen.add(key)
        for k in cost:
            cost[k] += r["stats"].get(k, 0)
    turns = sum(r["turns"] for r in results)
    return {"n_scenarios": len(results), "scenarios_passed": sum(r["passed"] for r in results),
            "checks_passed": sum(ok for _, ok in checks), "checks_total": len(checks),
            "by_check": {k: f"{a}/{b}" for k, (a, b) in sorted(by_kind.items())},
            "violations": {k: b - a for k, (a, b) in by_kind.items() if k.startswith("inv") and b - a},
            "turns": turns, "max_steps_turn": max((r["max_steps_turn"] for r in results), default=0),
            "repeated_replies": sum(r["repeated_replies"] for r in results),
            "cost": {**{k: round(v, 6) for k, v in cost.items()},
                     "usd_per_turn": round((cost["usd_live"] + cost["usd_replayed"]) / max(turns, 1), 6)}}


def groups_for(scns: list[dict]) -> list[list[dict]]:
    by_id, done, out = {s["id"]: s for s in scns}, set(), []
    for s in scns:
        if s["id"] in done:
            continue
        g = [s]
        for st in s["steps"]:
            o = st.get("to") if st.get("event") == "switch_case" else None
            if o in by_id and o not in done and by_id[o] not in g:
                g.append(by_id[o])
        done |= {x["id"] for x in g}
        out.append(g)
    return out


def run(system: str, split: str, ids: list[str] | None = None, llm=None, retriever=None, outdir=None) -> dict:
    scns = [read_json(p) for p in sorted((C.DATA / "eval" / "scenarios" / split).glob("*.json"))]
    if ids:
        want = set(ids)
        for s in list(scns):                              # keep switch partners together
            if s["id"] in want:
                want |= {st.get("to") for st in s["steps"] if st.get("event") == "switch_case"}
        scns = [s for s in scns if s["id"] in want]
    cases = {}
    for s in scns:
        cases[s["case_id"]] = read_json(C.DATA / "eval" / "cases" / s["split"] / f"{s['case_id']}.json")
    exclude = read_json(C.DATA / "eval" / "eval_exclusions.json")["exclude_issues"]
    if llm is None:
        from ..llm import get_llm
        llm = get_llm()
    if retriever is None:
        from ..index.search import Retriever
        retriever = Retriever(mode="hybrid")
    outdir = outdir or RUNS / f"p2_{system}_{split}"
    results = []
    for g in groups_for(scns):
        results += run_group(system, g, cases, llm, retriever, exclude, outdir)
    return {"system": system, "version": (Agent if system == "final" else MemorylessAgent).version,
            "split": split, "built_at": utcnow(), "model": getattr(llm, "chat_model", None),
            "cache_mode": getattr(llm, "cache_mode", None), "limits": LIMITS,
            "metrics": aggregate(results), "scenarios": results}


def print_run(res: dict) -> None:
    m = res["metrics"]
    print(f"{res['system']} ({res['version']}) on {res['split']}: {m['scenarios_passed']}/{m['n_scenarios']} scenarios "
          f"pass all code checks; {m['checks_passed']}/{m['checks_total']} checks")
    for r in res["scenarios"]:
        bad = [f"{n} [{c['detail']}]" for n, c in r["checks"].items() if not c["ok"]]
        print(f"  {'PASS' if r['passed'] else 'FAIL'} {r['sid']:8s} {r['n_ok']}/{r['n_checks']}  status={r['final']['status']}"
              f"  comments={len(r['final']['agent_comments'])}" + ("" if not bad else "\n        - " + "\n        - ".join(bad)))
    print(f"  by check: {m['by_check']}")
    print(f"  invariant violations: {m['violations'] or 'none'}")
    print(f"  repeated identical replies: {m['repeated_replies']}   turns {m['turns']}, "
          f"max steps in a turn {m['max_steps_turn'] if res['system'] == 'final' else 'n/a (no step accounting)'}")
    print(f"  cost: {m['cost']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--system", default="final", choices=SYSTEMS)
    ap.add_argument("--split", default="dev", choices=["dev", "test"])
    ap.add_argument("--scenarios", nargs="*")
    ap.add_argument("--confirm-test", action="store_true")
    a = ap.parse_args(argv)
    if a.split == "test" and not a.confirm_test:
        raise SystemExit("test split is for the final report only - pass --confirm-test")
    res = run(a.system, a.split, a.scenarios)
    suffix = "_subset" if a.scenarios else ""
    write_json(RUNS / f"p2_{a.system}_{a.split}{suffix}.json", res)
    if not a.scenarios:
        write_json(RUNS / "history" / f"p2_{a.system}_{res['version']}_{a.split}.json", res)
    print_run(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
