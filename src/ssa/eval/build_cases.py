"""Assemble evaluation cases + scenarios from the annotation drafts and validate them.

Run:  python -m ssa.eval.build_cases

Inputs  data/eval/annotations/{selection,gold_draft,scenarios_draft}.json
Outputs data/eval/cases/{dev,test}/c-<issue>.json
        data/eval/scenarios/{dev,test}/s-<issue>.json
        data/eval/eval_exclusions.json     every eval issue -> excluded from ALL retrieval
        data/eval/validation_report.json   checks below (non-zero exit on hard failures)

Case file layout
  visible   what the assistant may see at the start (title, body, as_of, url)
  filters   retrieval filters to use for this case (before=as_of, exclude eval issues)
  gold      hidden: outcome, missing_info, acceptable_next_steps, must_not,
            relevant_sources, reference (what really happened later)
Real follow-up turns are copied verbatim from the issue author's comments and
tagged origin=real; designed turns are tagged origin=designed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict

import numpy as np

from ..data import config as C
from ..data.io_utils import read_json, read_jsonl, utcnow, write_json

E = C.DATA / "eval"
A = E / "annotations"


def load():
    sel = read_json(A / "selection.json")["cases"]
    gold = read_json(A / "gold_draft.json")["cases"]
    scen = read_json(A / "scenarios_draft.json")["scenarios"]
    issues = {r["number"]: r for r in read_jsonl(C.PROCESSED / "issues.jsonl")}
    return sel, gold, scen, issues


def build(sel, gold, scen, issues) -> tuple[list, list]:
    eval_issues = sorted(c["issue"] for c in sel)
    cases = []
    for c in sel:
        r = issues[c["issue"]]
        g = gold[str(c["issue"])]
        case = {
            "case_id": f"c-{r['number']}", "split": c["split"], "bucket": c["bucket"],
            "topic": c["topic"], "family_id": r["family_id"], "multi_turn": bool(c.get("mt")),
            "visible": {"issue_number": r["number"], "url": r["url"], "title": r["title"],
                        "body": r["body"], "as_of": r["created_at"]},
            "filters": {"before": r["created_at"], "exclude_issues": eval_issues},
            "gold": {**g, "annotated_by": "claude-draft", "reviewed_by": None},
        }
        cases.append(case)
    scenarios = []
    for s in scen:
        r = issues[s["issue"]]
        by_id = {c["id"]: c for c in r["comments"]}
        steps = []
        for st in s["steps"]:
            st = dict(st)
            if st["type"] == "user" and st.get("origin") == "real":
                cm = by_id[st["comment_id"]]
                st.update(text=cm["text"], created_at=cm["created_at"], url=cm["url"],
                          by_issue_author=cm["is_issue_author"])
            steps.append(st)
        scenarios.append({**s, "case_id": f"c-{s['issue']}", "steps": steps,
                          "annotated_by": "claude-draft", "reviewed_by": None})
    return cases, scenarios


def validate(cases, scenarios, issues) -> dict:
    hard, warn = [], []
    by_split = defaultdict(list)
    for c in cases:
        by_split[c["split"]].append(c)
    rep: dict = {"n_cases": len(cases), "per_split": {}, "n_scenarios": len(scenarios)}
    for sp, cs in by_split.items():
        rep["per_split"][sp] = {"cases": len(cs), "buckets": dict(Counter(c["bucket"] for c in cs)),
                                "multi_turn": sum(c["multi_turn"] for c in cs),
                                "scenarios": sum(s["split"] == sp for s in scenarios)}
    if len(cases) < 30: hard.append("fewer than 30 cases")
    if any(len(v) < 15 for v in by_split.values()): hard.append("a split has fewer than 15 cases")
    if len(scenarios) < 10: hard.append("fewer than 10 multi-turn scenarios")
    if sum(s["split"] == "test" for s in scenarios) < len(scenarios) / 2 - 0.5:
        hard.append("fewer than half of the scenarios are in test")

    # 1. no topic / duplicate family spans both splits
    for key in ("topic", "family_id"):
        span = defaultdict(set)
        for c in cases:
            span[c[key]].add(c["split"])
        bad = [k for k, v in span.items() if len(v) > 1]
        if bad: hard.append(f"{key} in both splits: {bad}")

    # 2. relevant sources exist and existed before as_of; none is an eval issue
    con = sqlite3.connect(C.INDEX_DB)
    eval_set = {c["visible"]["issue_number"] for c in cases}
    src_issues = []
    for c in cases:
        for sid in c["gold"]["relevant_sources"]:
            if sid.startswith("page:"):
                row = con.execute("SELECT MIN(available_at), COUNT(*) FROM chunks WHERE type='doc' AND url LIKE ?",
                                  (f"%{sid[5:]}%",)).fetchone()
            else:
                row = con.execute("SELECT MIN(COALESCE(available_at,'')), COUNT(*) FROM chunks WHERE source_id=?",
                                  (sid,)).fetchone()
            if not row[1]:
                hard.append(f"{c['case_id']}: relevant source not in index: {sid}")
                continue
            if row[0] and row[0] >= c["visible"]["as_of"]:
                hard.append(f"{c['case_id']}: relevant source is from the future: {sid} ({row[0]})")
            if sid.startswith("issue:") and int(sid.split(":")[1]) in eval_set:
                hard.append(f"{c['case_id']}: relevant source is itself an eval case: {sid}")
            src_issues.append(sid)
    rep["cases_with_labelled_sources"] = sum(bool(c["gold"]["relevant_sources"]) for c in cases)

    # 3. real turns come from the issue author and appear after as_of, in order
    for s in scenarios:
        last = issues[s["issue"]]["created_at"]
        for st in s["steps"]:
            if st["type"] == "user" and st.get("origin") == "real":
                if not st["by_issue_author"]: warn.append(f"{s['id']}: real turn not by issue author")
                if st["created_at"] < last: hard.append(f"{s['id']}: real turns out of order")
                last = st["created_at"]

    # 4. cross-split near-duplicates via cached embeddings of the issue body (no API call)
    try:
        from ..index.embed import _h, cache
        import os
        model = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
        chunks = {}
        for r in read_jsonl(C.CHUNKS):
            if r["type"] == "issue" and r["issue_number"] in eval_set and r["heading"] == "body part 1":
                chunks[r["issue_number"]] = r["embed_text"]
        ec = cache()
        vec = {}
        for n, t in chunks.items():
            row = ec.execute("SELECT vec FROM emb WHERE h=?", (_h(model, t),)).fetchone()
            if row:
                v = np.frombuffer(row[0], dtype=np.float32)
                vec[n] = v / np.linalg.norm(v)
        dev = [c["visible"]["issue_number"] for c in by_split["dev"] if c["visible"]["issue_number"] in vec]
        test = [c["visible"]["issue_number"] for c in by_split["test"] if c["visible"]["issue_number"] in vec]
        pairs = sorted(((float(vec[a] @ vec[b]), a, b) for a in dev for b in test), reverse=True)
        rep["cross_split_max_similarity"] = [{"sim": round(s, 3), "dev": a, "test": b} for s, a, b in pairs[:5]]
        rep["cross_split_vectors_available"] = f"{len(dev) + len(test)}/{len(cases)}"
        for s, a, b in pairs:
            if s > 0.85:
                warn.append(f"near-duplicate across splits: #{a} (dev) vs #{b} (test) sim={s:.2f}")
    except Exception as e:  # embeddings optional
        warn.append(f"near-duplicate check skipped: {e}")

    rep["hard_failures"], rep["warnings"] = hard, warn
    return rep


def main() -> int:
    sel, gold, scen, issues = load()
    cases, scenarios = build(sel, gold, scen, issues)
    for c in cases:
        write_json(E / "cases" / c["split"] / f"{c['case_id']}.json", c)
    for s in scenarios:
        write_json(E / "scenarios" / s["split"] / f"{s['id']}.json", s)
    write_json(E / "eval_exclusions.json", {"built_at": utcnow(),
                                            "exclude_issues": sorted(c["visible"]["issue_number"] for c in cases),
                                            "note": "Pass to Filters(exclude_issues=...) for EVERY eval run."})
    rep = validate(cases, scenarios, issues)
    write_json(E / "validation_report.json", {"built_at": utcnow(), **rep})
    print(json.dumps({k: v for k, v in rep.items() if k not in ("warnings",)}, indent=1))
    for w in rep["warnings"]:
        print("WARN", w)
    return 1 if rep["hard_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
