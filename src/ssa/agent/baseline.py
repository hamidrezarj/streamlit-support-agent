"""Problem-1 BASELINE: one query -> flat hybrid top-10 -> ONE LLM call.

Deliberately simple (the point of comparison), but not a strawman: same model,
same index + leakage filters, same output contract, a reasonable prompt.
What it does NOT do (and the final pipeline does): fact extraction,
multi-query / grouped retrieval, evidence assessment, version checks,
fact/doc/hypothesis discipline enforced in code, state, tools.

The system only ever receives case["visible"] and case["filters"] - never gold.
"""
from __future__ import annotations

import time

from ..index.search import Filters, Retriever
from ..llm.client import LLMError
from .evidence import format_hits, label_hits
from .rules import RULES
from .schema import OUTCOME_GUIDE, OUTPUT_SPEC, REQUIRED, validate

NAME = "baseline"
PROMPT_VERSION = "baseline-v1"
K = 10
MAX_TOKENS = 1500

SYSTEM = f"""You are a technical support assistant for Streamlit (running and deploying Streamlit apps).
You get a user's issue report and numbered evidence from Streamlit docs, earlier GitHub issues and release notes.
Write a useful response for the user and a short technical summary for the maintainer.
Cite evidence as [S1], [S2]. Do not invent versions, test results or facts that are not in the report or evidence.

{OUTCOME_GUIDE}

{OUTPUT_SPEC}"""

# baseline-v2: identical single call + the SAME rules text the final pipeline uses.
# Isolates "better wording" from "better pipeline".
SYSTEM_V2 = SYSTEM.replace("\n\nOutcomes:", "\n\n" + RULES + "\n\nOutcomes:", 1)


def build_query(visible: dict) -> str:
    return visible["title"] + "\n" + visible["body"][:1500]


def filters_for(case: dict) -> Filters:
    f = case["filters"]
    return Filters(before=f["before"], exclude_issues=set(f["exclude_issues"]))


class Baseline:
    name = NAME
    prompt_version = PROMPT_VERSION
    system_prompt = SYSTEM

    def __init__(self, llm, retriever: Retriever | None = None, mode: str = "hybrid", k: int = K):
        self.llm = llm
        self.retr = retriever or Retriever(mode=mode)
        self.mode, self.k = mode, k

    def run(self, case: dict) -> dict:
        v = case["visible"]
        t0 = time.monotonic()
        hits = self.retr.search(build_query(v), filters_for(case), k=self.k, mode=self.mode)
        t_retr = time.monotonic() - t0
        sources = label_hits(hits)
        user = (f"Today is {v['as_of'][:10]}.\n\n"
                f"## Issue report\nTitle: {v['title']}\n\n{v['body']}\n\n"
                f"## Evidence\n{format_hits(hits)}")
        messages = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": user}]
        error = None
        try:
            out = self.llm.chat_json(messages, required=REQUIRED, max_tokens=MAX_TOKENS,
                                     tag=f"{self.name}:{case['case_id']}")
        except LLMError as e:          # degrade, never crash the whole eval run
            out, error = {}, str(e)
        return {"output": out, "sources": sources, "check": validate(out, sources),
                "retrieved": [s["source_id"] for s in sources.values()],
                "error": error, "retrieval_s": round(t_retr, 3), "steps": 2}


class BaselineV2(Baseline):
    """Same retrieval and single call as Baseline; prompt adds the shared RULES."""
    name = "baseline-v2"
    prompt_version = "baseline-v2"
    system_prompt = SYSTEM_V2
