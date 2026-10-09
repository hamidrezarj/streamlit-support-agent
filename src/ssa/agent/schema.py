"""Shared Problem-1 output contract - the baseline and the final system both
return exactly this, so they are scored by the same code.

Citations are written as [S1], [S2] ... and refer to the numbered evidence
list the system was shown. Code (not the model) maps them back to source ids
and records citations that point to nothing.
"""
from __future__ import annotations

import re

OUTCOMES = ("usage_guidance", "known_issue", "info_request", "escalate")
# eval bucket -> outcomes that count as the right *kind* of response
BUCKET_OUTCOMES = {"answerable": {"usage_guidance", "known_issue"},
                   "ambiguous": {"info_request"},
                   "escalate": {"escalate"}}
NEXT_STEP_TYPES = ("answer", "ask", "check", "escalate")
BASIS = ("reported", "documented", "similar_report", "assistant_hypothesis")

OUTPUT_SPEC = """Return ONE JSON object with exactly these keys:
{
  "outcome": "usage_guidance | known_issue | info_request | escalate",
  "summary": "one sentence: the problem as reported",
  "known_facts": ["facts stated in the report itself (versions, OS, what was tried)"],
  "hypotheses": [{"text": "possible cause", "basis": "documented | similar_report | assistant_hypothesis",
                  "sources": ["S1"]}],
  "unknowns": ["what is still unknown and matters for the decision"],
  "next_step": {"type": "answer | ask | check | escalate", "text": "the single most useful next step"},
  "questions": ["questions for the user (may be empty)"],
  "user_reply": "reply to the user; cite evidence inline as [S1]",
  "maintainer_summary": {"problem": "", "environment": "", "checks_done": "", "key_sources": ["S1"],
                         "open_questions": "", "next_step": ""},
  "confidence": "low | medium | high"
}"""

OUTCOME_GUIDE = """Outcomes:
- usage_guidance: the problem is explained by documented behaviour or configuration; tell the user what to do.
- known_issue: it matches a known bug or limitation in the evidence (e.g. fixed in a later version); explain it and give the workaround/upgrade path.
- info_request: the information is not enough to tell the likely causes apart; ask the most useful question(s) or propose a concrete check.
- escalate: likely a new or unconfirmed Streamlit bug; refer it to maintainers with a summary and the evidence."""

CITE = re.compile(r"\[S(\d+)\]")
REQUIRED = ["outcome", "user_reply", "maintainer_summary"]


def cited_labels(out: dict) -> list[str]:
    """All S-labels the output cites (reply, hypotheses, maintainer summary), in order."""
    labels: list[str] = []

    def add(lab: str):
        lab = lab.strip().strip("[]")
        if re.fullmatch(r"S\d+", lab) and lab not in labels:
            labels.append(lab)

    for m in CITE.findall(out.get("user_reply") or ""):
        add(f"S{m}")
    for h in out.get("hypotheses") or []:
        if isinstance(h, dict):
            for s in h.get("sources") or []:
                add(str(s))
            for m in CITE.findall(h.get("text") or ""):
                add(f"S{m}")
    ms = out.get("maintainer_summary") or {}
    if isinstance(ms, dict):
        for s in ms.get("key_sources") or []:
            add(str(s))
        for v in ms.values():
            if isinstance(v, str):
                for m in CITE.findall(v):
                    add(f"S{m}")
    return labels


def validate(out: dict, sources: dict[str, dict]) -> dict:
    """Check the contract in code. Returns {problems, cited, invalid_citations}."""
    problems = []
    if out.get("outcome") not in OUTCOMES:
        problems.append(f"bad outcome {out.get('outcome')!r}")
    ns = out.get("next_step")
    if not isinstance(ns, dict) or ns.get("type") not in NEXT_STEP_TYPES:
        problems.append("bad next_step")
    if not isinstance(out.get("maintainer_summary"), dict):
        problems.append("maintainer_summary is not an object")
    for h in out.get("hypotheses") or []:
        if not isinstance(h, dict) or h.get("basis") not in BASIS:
            problems.append("hypothesis without valid basis")
            break
    cited = cited_labels(out)
    invalid = [c for c in cited if c not in sources]
    return {"problems": problems, "cited": [c for c in cited if c in sources],
            "invalid_citations": invalid}
