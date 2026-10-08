"""Build a reviewable pool of evaluation-case candidates.

Run:  python -m ssa.eval.select_cases     -> data/eval/case_candidates.jsonl

Evaluation is HISTORICAL: a case starts at the issue's `created_at` (as_of).
The assistant only sees the title/body as first written; everything later
(comments, labels, fixes) is hidden gold used for judging.

Bucket hints come from what maintainers actually did later (label history and
comments). They are hints for the human annotator, not final labels:
  answerable  expected-behavior / won't-fix / redirect-forum / unlikely labels,
              or an earlier issue in the same duplicate family existed,
              or closed after a maintainer reply without any PR/commit (explained, not fixed)
  ambiguous   maintainer asked the user for more info (awaiting-user-response),
              or cannot-reproduce
  escalate    later confirmed as a bug (status:confirmed) - a correct answer at
              as_of is "likely bug, escalate with evidence/repro"
Multi-turn potential: the issue author replied after a maintainer comment.
"""
from __future__ import annotations

import sys
from collections import defaultdict

from ..data import config as C
from ..data.io_utils import read_jsonl, write_jsonl

ANSWER_LABELS = {"status:expected-behavior", "status:won't-fix", "status:will-not-fix",
                 "status:redirect-forum", "status:unlikely"}


def features(r: dict, fam_members: list[dict]) -> dict:
    ever = {h["label"] for h in r["label_history"] if h["label"]} | set(r["labels"])
    mt = [c for c in r["comments"] if c["is_maintainer"]]
    au = [c for c in r["comments"] if c["is_issue_author"]]
    first_mt = min((c["created_at"] for c in mt), default=None)
    follow = [c for c in au if first_mt and c["created_at"] > first_mt]
    hints = []
    if ever & ANSWER_LABELS or any(o["created_at"] < r["created_at"] for o in fam_members
                                   if o["number"] != r["number"]):
        hints.append("answerable")
    elif (r["state"] == "closed" and not r["linked_prs"] and "status:confirmed" not in ever
          and not r["closed_by_commit"] and mt):
        hints.append("answerable")        # closed after a maintainer explanation, no code change
    if {"status:awaiting-user-response", "status:cannot-reproduce"} & ever:
        hints.append("ambiguous")
    if "status:confirmed" in ever:
        hints.append("escalate")
    return {
        "number": r["number"], "url": r["url"], "title": r["title"], "kind": r["kind"],
        "created_at": r["created_at"], "state": r["state"], "state_reason": r["state_reason"],
        "family_id": r["family_id"], "family_size": len(fam_members),
        "bucket_hints": hints, "status_labels_ever": sorted(l for l in ever if l.startswith("status:")),
        "env": {k: v for k, v in r["env"].items() if not k.endswith("_parsed")},
        "env_missing": [k for k in ("streamlit_version", "python_version", "os", "browser")
                        if r["env"][k] is None],
        "context": r["context_keywords"], "error_signatures": r["error_signatures"],
        "body_chars": len(r["body"]), "n_comments": len(r["comments"]),
        "n_maintainer_comments": len(mt), "author_followups_after_maintainer": len(follow),
        "multi_turn_candidate": len(follow) >= 1,
        "fixed_in": r["fixed_in"],
    }


def main() -> int:
    issues = read_jsonl(C.PROCESSED / "issues.jsonl")
    fams = defaultdict(list)
    for r in issues:
        fams[r["family_id"]].append(r)
    pool = []
    for r in issues:
        if r["kind"] not in ("bug", "regression", "question", "unknown"):
            continue
        if r["created_at"] < "2022-01-01" or len(r["body"]) < 200:
            continue                      # need the modern template + a real description
        if not any(c["is_maintainer"] for c in r["comments"]):
            continue                      # no maintainer reaction -> no reference outcome
        f = features(r, fams[r["family_id"]])
        if f["bucket_hints"] or f["multi_turn_candidate"]:
            pool.append(f)
    pool.sort(key=lambda f: (-len(f["bucket_hints"]), -f["multi_turn_candidate"], f["number"]))
    n = write_jsonl(C.DATA / "eval" / "case_candidates.jsonl", pool)
    by = defaultdict(int)
    for f in pool:
        for h in f["bucket_hints"] or ["none"]:
            by[h] += 1
    print(f"candidates: {n}  by hint: {dict(by)}  multi-turn: {sum(f['multi_turn_candidate'] for f in pool)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
