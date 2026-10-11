"""Scripted LLM + tiny retriever so the UI can run offline (SSA_UI_FAKE=1): demos without an
API key and automatic UI tests. Answers are canned - never use them for evaluation."""
from __future__ import annotations

import re

from ..index.search import Hit
from ..llm.fake import FakeLLM


def _hit(sid, typ, title, text, url):
    return Hit(chunk_id=sid, source_id=sid, type=typ, title=title, heading="(offline demo)", url=url,
               text=text, available_at=None if typ == "doc" else "2021-06-01T00:00:00Z", score=0.1, ranks={})


HITS = [
    _hit("doc:/knowledge-base/deploy/remote-start#symptom", "doc", "App is not loading when running remotely",
         "Symptom: the app says Please wait forever. Check the WebSocket connection and the proxy configuration.",
         "https://docs.streamlit.io/knowledge-base/deploy/remote-start"),
    _hit("issue:4001", "issue", "Stuck on 'Please wait...' behind nginx",
         "[maintainer, 2021-06-02]\nYour nginx config is missing the WebSocket upgrade headers.",
         "https://github.com/streamlit/streamlit/issues/4001"),
]


class FakeRetr:
    query_embeddings = 0

    def search(self, q, f, k=10, mode=None, **kw):
        return HITS[:k]

    def search_grouped(self, q, f, per_group=None, mode=None):
        return {"docs": HITS[:1], "release_notes": [], "issues": HITS[1:]}


def fake_llm() -> FakeLLM:
    def answer(messages, **kw):
        sysm, user = messages[0]["content"], messages[-1]["content"]
        if sysm.startswith("You update the memory"):
            new = user.split("NEW messages:", 1)[1]
            facts = []
            if (m := re.search(r"\d+\.\d+\.\d+", new)):
                facts.append({"name": "streamlit_version", "value": m.group(0), "quote": m.group(0)})
            if "nginx" in new.lower():
                facts.append({"name": "access_path", "value": "nginx reverse proxy", "quote": "nginx",
                              "corrects": "access_path"})
            elif "direct" in new.lower():
                facts.append({"name": "access_path", "value": "direct IP and port", "quote": "direct"})
            ok = "works now" in new.lower() or "working now" in new.lower()
            return {"facts": facts, "tried": [], "user_confirmed_fix": ok,
                    "confirmation_quote": ("works now" if "works now" in new.lower() else "working now") if ok else None}
        if sysm.startswith("You analyse"):
            return {"symptoms": ["app stuck on Please wait"], "search_queries": ["please wait websocket proxy"],
                    "env_quotes": {}}
        if "Assess EVERY" in sysm:
            return {"relations": {"S1": "similar_symptom", "S2": "similar_symptom"},
                    "hypotheses": [{"text": "a proxy without WebSocket upgrade", "supported_by": ["S1", "S2"],
                                    "check": "Is the app behind a reverse proxy?"}],
                    "blocking_unknowns": ["how the app is accessed"], "proposed_outcome": "info_request",
                    "outcome_reason": "Several causes remain; how the app is reached decides between them."}
        if sysm.startswith("You write the final"):
            return {"outcome": "info_request", "summary": "App stuck on Please wait on a server.",
                    "known_facts": [], "unknowns": ["access path"],
                    "hypotheses": [{"text": "proxy without WebSocket upgrade", "basis": "documented", "sources": ["S1"]}],
                    "next_step": {"type": "ask", "text": "Ask how the app is accessed."},
                    "questions": ["Do you open the app directly (IP:port) or through a reverse proxy?"],
                    "user_reply": "(offline demo) This usually means the browser cannot keep its WebSocket "
                                  "connection [S1]. How do you open the app: directly or through a proxy?",
                    "maintainer_summary": {"problem": "Please wait forever", "next_step": "ask access path"},
                    "confidence": "low"}
        return {"outcome": "known_issue", "summary": "s", "known_facts": [], "hypotheses": [], "unknowns": [],
                "next_step": {"type": "answer", "text": "x"}, "questions": [],
                "user_reply": "(offline demo baseline) This looks like a known issue [S2].",
                "maintainer_summary": {"problem": "p"}, "confidence": "high"}
    return FakeLLM(answer)
