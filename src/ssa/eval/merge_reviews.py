"""Parse the human gold reviews (markdown) into structured edits.

Run:  python -m ssa.eval.merge_reviews        -> data/eval/annotations/reviews.json
      (then python -m ssa.eval.build_cases applies them to the case files)

Input  data/eval/annotations/reviews/<reviewer>/{dev,test}/*-c-<n>.md   (one file per case)
       data/eval/annotations/review_decisions.json                    (team decisions: acceptable_outcomes)
Rules
* outcome      = the reviewer's recommended outcome (human overrides the LLM draft)
* bucket       = changed only where the reviewer wrote "Change from X to Y"
* missing_info / acceptable_next_steps / must_not / summary / hypotheses / reference:
                 replaced where the reviewer gave new content ("No change ..." keeps the draft)
* the reviewer's blind outcome (decided BEFORE reading the draft) is kept for the
  human-vs-draft agreement statistic (Cohen's kappa)
"""
from __future__ import annotations

import re
import sys
from collections import Counter

from ..data import config as C
from ..data.io_utils import read_json, utcnow, write_json

A = C.DATA / "eval" / "annotations"
OUTCOMES = ("usage_guidance", "known_issue", "info_request", "escalate")


def _field(text: str, name: str) -> str | None:
    m = re.search(rf"^- {re.escape(name)}:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else None


def _unchanged(v: str | None) -> bool:
    return v is None or v.lower().startswith("no change")


def _items(v: str) -> list[str]:
    """'A.; B.' -> ['A.', 'B.'] (items end with a period, separated by '; ')."""
    parts = re.split(r"(?<=[.!?])\s*;\s*", v.strip())
    return [p.strip() for p in parts if p.strip()]


def _missing(v: str) -> list[dict]:
    out = []
    for part in re.split(r";\s*(?=[^;]*\s—\s)", v):
        item, _, why = part.partition(" — ")
        if item.strip():
            out.append({"item": item.strip(), "why": why.strip().rstrip(".") + ("." if why.strip() else "")})
    return out


def parse(path) -> dict:
    t = path.read_text()
    g = lambda rx: (m.group(1).strip() if (m := re.search(rx, t, re.S)) else None)
    r = {"case_id": g(r"Case ID:\s*(\S+)"), "split": g(r"Split:\s*(\S+)"), "reviewer": g(r"Reviewer:\s*([^\n]+)"),
         "review_date": g(r"Review date:\s*(\S+)"), "file": str(path.relative_to(C.ROOT)),
         "blind_outcome": g(r"My initial decision.*?Outcome:\s*(\w+)"),
         "draft_outcome": g(r"Original outcome:\s*(\w+)"), "verdict": g(r"Verdict:\s*([^\n]+)"),
         "outcome": g(r"Recommended outcome:\s*(\w+)"),
         "reason": g(r"Reason for accepting or changing the draft:\s*([^\n]+)"),
         "complete": g(r"Human review complete:\s*(\w+)") == "YES", "edits": {}}
    for o in ("blind_outcome", "draft_outcome", "outcome"):
        if r[o] not in OUTCOMES:
            raise ValueError(f"{path.name}: bad {o} {r[o]!r}")
    mi = _field(t, "missing_info")
    if not _unchanged(mi):
        r["edits"]["missing_info"] = _missing(mi)
    for f in ("acceptable_next_steps", "must_not", "hypotheses"):
        v = _field(t, f)
        if not _unchanged(v):
            r["edits"][f] = _items(v)
    for f in ("summary", "reference"):
        v = _field(t, f)
        if not _unchanged(v):
            r["edits"][f] = v
    b = _field(t, "bucket") or ""
    m = re.search(r"Change from (\w+) to (\w+)", b)
    if m:
        r["edits"]["bucket"] = {"from": m.group(1), "to": m.group(2)}
    return r


def kappa(pairs: list[tuple[str, str]]) -> float:
    n = len(pairs)
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] / n * cb[k] / n for k in set(ca) | set(cb))
    return round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0


def main() -> int:
    files = sorted((A / "reviews").glob("*/*/*.md"))
    reviews = [parse(p) for p in files]
    dec = read_json(A / "review_decisions.json")
    for r in reviews:
        alt = dec["acceptable_outcomes"].get(r["case_id"], {})
        r["acceptable_outcomes"] = [r["outcome"]] + [o for o in alt.get("alternates", []) if o != r["outcome"]]
        if alt:
            r["acceptable_why"] = alt["why"]
    pairs = [(r["blind_outcome"], r["draft_outcome"]) for r in reviews]
    stats = {"n": len(reviews), "complete": sum(r["complete"] for r in reviews),
             "blind_equals_draft": sum(a == b for a, b in pairs), "kappa_blind_vs_draft": kappa(pairs),
             "outcome_changed": [r["case_id"] for r in reviews if r["outcome"] != r["draft_outcome"]],
             "needs_discussion": [r["case_id"] for r in reviews if "discussion" in (r["verdict"] or "").lower()],
             "bucket_changed": [r["case_id"] for r in reviews if "bucket" in r["edits"]],
             "fields_edited": dict(Counter(k for r in reviews for k in r["edits"]))}
    write_json(A / "reviews.json", {"built_at": utcnow(), "stats": stats,
                                    "reviews": {r["case_id"]: r for r in reviews}})
    print(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
