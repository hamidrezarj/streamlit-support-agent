"""Command-line interface for the Streamlit support agent.

  python -m ssa.cli cases [--split dev]                       list eval cases
  python -m ssa.cli analyze --case c-5500                     run the final pipeline on an eval case
  python -m ssa.cli analyze --case c-5500 --system baseline   ... or a baseline, for comparison
  python -m ssa.cli analyze --title "App stuck on Please wait" --body-file report.txt [--as-of 2024-05-01]
  echo "..." | python -m ssa.cli analyze --title "..." --body-file -
  python -m ssa.cli show --case c-5500 [--run final]          pretty-print a saved eval run ($0, no API)

Common flags: --all-sources (also list irrelevant evidence), --gold (eval cases: show the hidden gold
AFTER the result - never sent to the system), --json (raw result), --save PATH, --no-color.

`analyze` goes through the normal LLM client: identical requests replay from the cache for $0; new ones
are budget-guarded and written to the ledger. Retrieval always applies the leakage filters
(before = as_of, all eval issues excluded).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from .data import config as C
from .data.io_utils import read_json, write_json

WIDTH = 100


# ------------------------------------------------------------------ styling
class Style:
    def __init__(self, color: bool):
        self.on = color

    def _w(self, code, s):
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def h(self, s): return self._w("1;36", s)       # section heading
    def b(self, s): return self._w("1", s)          # bold
    def dim(self, s): return self._w("2", s)
    def ok(self, s): return self._w("32", s)
    def bad(self, s): return self._w("31", s)
    def warn(self, s): return self._w("33", s)


OUTCOME_LABEL = {"usage_guidance": "USAGE GUIDANCE", "known_issue": "KNOWN ISSUE",
                 "info_request": "INFO REQUEST", "escalate": "ESCALATE"}


def wrap(text: str, indent: str = "  ") -> str:
    out = []
    for para in (text or "").split("\n"):
        out.append(textwrap.fill(para, WIDTH, initial_indent=indent, subsequent_indent=indent) if para.strip() else "")
    return "\n".join(out)


# ------------------------------------------------------------------ rendering
def render(res: dict, visible: dict, case_id: str, system: str, stats: dict | None,
           st: Style, all_sources: bool = False) -> str:
    out = res.get("output") or {}
    tr = res.get("trace") or {}
    d = tr.get("decision") or {}
    L = []
    sec = lambda t: L.append("\n" + st.h(f"== {t} " + "=" * max(0, WIDTH - len(t) - 4)))

    L.append(st.b(f"{case_id}  [{system}]  ") + visible["title"])
    L.append(st.dim(f"as of {visible['as_of'][:10]}" + (f"  |  {visible.get('url')}" if visible.get("url") else "")))

    facts = tr.get("facts")
    if facts:
        sec("FACTS FROM THE REPORT")
        env = {k: v for k, v in (facts.get("env") or {}).items() if v}
        L.append(f"  env: {env or 'nothing stated'}")
        for k in ("symptoms", "error_messages", "tried"):
            if facts.get(k):
                L.append(f"  {k}: " + "; ".join(facts[k]))
        if facts.get("setup"):
            L.append(f"  setup: {facts['setup']}")
        if tr.get("queries"):
            L.append(st.dim(f"  search queries: {len(tr['queries'])} -> {tr.get('n_candidates', '?')} candidate sources"))

    sec("EVIDENCE")
    srcs = res.get("sources") or {}
    rel = tr.get("assessments") or {}
    resolves = set(tr.get("resolves") or [])
    cited = set((res.get("check") or {}).get("cited", []))
    shown = 0
    for lab, s in srcs.items():
        r = rel.get(lab, "-" if not rel else "unassessed")
        if r == "irrelevant" and not all_sources:
            continue
        shown += 1
        tags = [s["type"]]
        if s.get("maintainer"):
            tags.append("maintainer")
        if lab in resolves:
            tags.append(st.ok("RESOLVES"))
        relc = st.ok(r) if r == "same_cause" else st.warn(r) if r == "similar_symptom" else st.dim(r)
        mark = st.b("*") if lab in cited else " "
        L.append(f" {mark}{lab:>4} {relc:<15} {', '.join(tags):<24} {s['title'][:52]}")
        L.append(st.dim(f"        {(s.get('available_at') or 'docs')[:10]:<10}  {s['url']}"))
    hidden = len(srcs) - shown
    L.append(st.dim(f"  * = cited in the reply" + (f"   ({hidden} irrelevant hidden; --all-sources)" if hidden else "")))

    if d:
        sec("DECISION")
        L.append(f"  outcome: {st.b(OUTCOME_LABEL.get(d.get('outcome'), d.get('outcome')))}   "
                 f"confidence: {d.get('confidence')}   LLM proposed: {d.get('proposed')}"
                 + ("" if d.get("agrees_with_llm", True) else st.warn("  (overridden by code)")))
        for r in d.get("rules_applied") or []:
            L.append(f"  rule: {r}")
        if d.get("blocking_unknowns"):
            L.append(f"  blocking unknowns: {d['blocking_unknowns']}")
        if d.get("env_unknowns_ignored"):
            L.append(st.dim(f"  ignored as non-blocking: {d['env_unknowns_ignored']}"))
        if tr.get("outcome_reason"):
            L.append(wrap("why: " + tr["outcome_reason"]))
    elif out.get("outcome"):
        sec("DECISION")
        L.append(f"  outcome: {st.b(OUTCOME_LABEL.get(out['outcome'], out['outcome']))}   "
                 f"confidence: {out.get('confidence')}   (single LLM call, no code checks)")

    if out.get("hypotheses"):
        sec("HYPOTHESES")
        for h in out["hypotheses"]:
            L.append(wrap(f"- {h.get('text')}  [{h.get('basis')}] {h.get('sources') or ''}"))
    ns = out.get("next_step") or {}
    if out:
        sec("NEXT STEP")
        L.append(wrap(f"[{ns.get('type')}] {ns.get('text')}"))
        for q in out.get("questions") or []:
            L.append(wrap(f"? {q}"))
        sec("REPLY TO THE USER")
        L.append(wrap(out.get("user_reply") or ""))
        ms = out.get("maintainer_summary") or {}
        if ms:
            sec("MAINTAINER SUMMARY")
            for k in ("problem", "environment", "checks_done", "open_questions", "next_step"):
                if ms.get(k):
                    L.append(wrap(f"{k}: {ms[k]}"))
            if ms.get("key_sources"):
                L.append(f"  key sources: {', '.join(ms['key_sources'])}")
    if res.get("error"):
        sec("ERROR")
        L.append(st.bad(f"  {res['error']}"))

    enf = tr.get("enforced") or {}
    if any(enf.values()):
        sec("CORRECTED BY CODE")
        for f in enf.get("fixes") or []:
            L.append(f"  - {f}")
        if enf.get("removed_citations"):
            L.append(f"  - removed citations to unknown/irrelevant sources: {enf['removed_citations']}")
        for q in enf.get("dropped_questions") or []:
            L.append(f"  - dropped question: {q}")

    if stats:
        sec("COST")
        L.append(f"  LLM calls: {stats.get('live_calls', 0)} live, {stats.get('replayed_calls', 0)} replayed   "
                 f"tokens: {stats.get('tokens_in', 0)} in / {stats.get('tokens_out', 0)} out   "
                 f"USD: {stats.get('usd_live', 0):.5f} live (+{stats.get('usd_replayed', 0):.5f} replayed)   "
                 f"LLM time: {stats.get('latency_s', 0):.1f}s")
    return "\n".join(L)


def render_gold(case: dict, outcome: str | None, st: Style) -> str:
    g = case["gold"]
    ok = outcome == g["outcome"]
    L = ["\n" + st.h("== GOLD (hidden from the system) " + "=" * (WIDTH - 34)),
         f"  bucket: {case['bucket']}   gold outcome: {g['outcome']}   -> "
         + (st.ok("MATCH") if ok else st.bad(f"MISMATCH (got {outcome})")),
         wrap("reference: " + (g.get("reference") or "")),
         f"  relevant sources: {', '.join(g.get('relevant_sources') or []) or '-'}",
         f"  must not: {'; '.join(g.get('must_not') or []) or '-'}",
         st.dim(f"  annotated_by={g.get('annotated_by')} reviewed_by={g.get('reviewed_by')}")]
    return "\n".join(L)


# ------------------------------------------------------------------ helpers
def find_case(case_id: str) -> dict:
    for split in ("dev", "test"):
        p = C.DATA / "eval" / "cases" / split / f"{case_id}.json"
        if p.exists():
            return read_json(p)
    raise SystemExit(f"case {case_id!r} not found in data/eval/cases/{{dev,test}}")


def eval_exclusions() -> list[int]:
    p = C.DATA / "eval" / "eval_exclusions.json"
    return read_json(p)["exclude_issues"] if p.exists() else []


def new_report(title: str, body: str, as_of: str | None) -> dict:
    as_of = as_of or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if len(as_of) == 10:
        as_of += "T00:00:00Z"
    cid = "new-" + hashlib.sha1((title + body).encode()).hexdigest()[:8]
    return {"case_id": cid, "visible": {"title": title, "body": body, "as_of": as_of},
            "filters": {"before": as_of, "exclude_issues": eval_exclusions()}}


# ------------------------------------------------------------------ commands
def cmd_cases(a, st: Style) -> int:
    for split in (["dev", "test"] if a.split == "all" else [a.split]):
        if split == "test":
            print(st.warn("(test split: for the final report only - do not tune on it)"))
        for p in sorted((C.DATA / "eval" / "cases" / split).glob("*.json")):
            c = read_json(p)
            mt = " multi-turn" if c.get("multi_turn") else ""
            print(f"{c['case_id']:9s} {split:4s} {c['bucket']:10s}{mt:11s} {c['visible']['title'][:70]}")
    return 0


def cmd_analyze(a, st: Style, llm=None, system=None) -> int:
    from .eval.p1 import PUBLIC_KEYS, make_system
    if a.case:
        case = find_case(a.case)
        if case["split"] == "test" and not a.confirm_test:
            raise SystemExit("test case: pass --confirm-test (the test split is for the final report only)")
        public = {k: case[k] for k in PUBLIC_KEYS}
    else:
        if not a.title or not a.body_file:
            raise SystemExit("give --case, or --title and --body-file (use - for stdin)")
        body = sys.stdin.read() if a.body_file == "-" else open(a.body_file, encoding="utf-8").read()
        case, public = None, new_report(a.title, body, a.as_of)
    if llm is None:
        from .llm import get_llm
        llm = get_llm()
    system = system or make_system(a.system, llm)
    llm.reset_stats()
    res = system.run(public)
    stats = dict(llm.stats)
    if a.json:
        print(json.dumps({"case_id": public["case_id"], "system": a.system, "stats": stats, **res},
                         indent=1, ensure_ascii=False))
    else:
        print(render(res, public["visible"], public["case_id"], a.system, stats, st, a.all_sources))
        if a.gold and case:
            print(render_gold(case, (res.get("output") or {}).get("outcome"), st))
    if a.save:
        write_json(Path(a.save) if a.save.endswith(".json") else Path(a.save) / f"{public['case_id']}.json",
                   {"case_id": public["case_id"], "system": a.system, "visible": public["visible"],
                    "stats": stats, **res})
    return 0 if not res.get("error") else 1


def cmd_show(a, st: Style) -> int:
    case = find_case(a.case)
    path = C.DATA / "eval" / "runs" / f"p1_{a.run}_{case['split']}.json"
    if not path.exists():
        raise SystemExit(f"no saved run {path.name} - run: python -m ssa.eval.p1 --system {a.run} --split {case['split']}")
    run = read_json(path)
    pc = next((c for c in run["cases"] if c["case_id"] == a.case), None)
    if pc is None:
        raise SystemExit(f"{a.case} not in {path.name}")
    print(st.dim(f"saved run {path.name}  ({run.get('prompt_version')}, {run.get('built_at')})"))
    print(render(pc, case["visible"], a.case, a.run, pc.get("stats"), st, a.all_sources))
    if a.gold:
        print(render_gold(case, (pc.get("output") or {}).get("outcome"), st))
    return 0


def cmd_tracker(a, st: Style) -> int:
    from .agent.tools import TOOL_SPECS
    from .tracker.store import Tracker
    if a.action == "tools":
        for t in TOOL_SPECS:
            gate = st.warn("GATED") if t["gated"] else "     "
            print(f"{gate} {t['name']:16s} {t['input']}  ->  {t['output']}   errors: {', '.join(t['errors'])}")
        return 0
    tr = Tracker()
    if a.action == "reset":
        from .agent.state import CaseStore
        tr.reset()
        cs = CaseStore()
        cs.con.executescript("DELETE FROM cases; DELETE FROM proposals;")
        print(f"tracker and case memory reset ({tr.path}, {cs.path})")
        return 0
    rows = tr.con.execute("SELECT id, status, labels, version, title FROM tickets ORDER BY id").fetchall()
    if not rows:
        print("tracker is empty")
    for r in rows:
        n = tr.con.execute("SELECT COUNT(*) FROM comments WHERE ticket_id=?", (r["id"],)).fetchone()[0]
        print(f"{r['id']:12s} {r['status']:16s} v{r['version']:<3d} {n:2d} comments  {r['labels']:28s} {r['title'][:50]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m ssa.cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-color", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("cases", help="list evaluation cases")
    p.add_argument("--split", default="dev", choices=["dev", "test", "all"])

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--all-sources", action="store_true", help="also list sources judged irrelevant")
    common.add_argument("--gold", action="store_true", help="eval cases: show hidden gold after the result")

    p = sub.add_parser("analyze", parents=[common], help="analyse an eval case or a new report")
    p.add_argument("--case")
    p.add_argument("--title")
    p.add_argument("--body-file", help="file with the report body, or - for stdin")
    p.add_argument("--as-of", help="historical date for a new report (YYYY-MM-DD); default: now")
    p.add_argument("--system", default="final", choices=["final", "baseline", "baseline-v2"])
    p.add_argument("--json", action="store_true")
    p.add_argument("--save", help="write the full result (with trace) to this .json file or directory")
    p.add_argument("--confirm-test", action="store_true")

    p = sub.add_parser("tracker", help="local tracker: list tickets, reset, list tools")
    p.add_argument("action", choices=["list", "reset", "tools"])

    p = sub.add_parser("show", parents=[common], help="pretty-print a saved eval run ($0)")
    p.add_argument("--case", required=True)
    p.add_argument("--run", default="final", choices=["final", "baseline", "baseline-v2"])
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    st = Style(color=not a.no_color and sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
    return {"cases": cmd_cases, "analyze": cmd_analyze, "show": cmd_show, "tracker": cmd_tracker}[a.cmd](a, st)


if __name__ == "__main__":
    sys.exit(main())
