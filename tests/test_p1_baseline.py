"""Offline tests for the Problem-1 baseline, output contract and scorer (no API, no index)."""
import json

from ssa.agent.baseline import Baseline
from ssa.agent.schema import validate
from ssa.eval import p1
from ssa.index.search import Hit
from ssa.llm.fake import FakeLLM


def _hit(sid, typ="issue", url="https://github.com/streamlit/streamlit/issues/1"):
    return Hit(chunk_id=sid + "-c", source_id=sid, type=typ, title="t " + sid, heading="body part 1",
               url=url, text="some text about " + sid, available_at="2022-01-01T00:00:00Z",
               score=0.1, ranks={})


class FakeRetr:
    def __init__(self, hits):
        self.hits, self.calls, self.query_embeddings = hits, [], 0

    def search(self, query, filters, k=10, mode=None, **kw):
        self.calls.append((query, filters, k, mode))
        return self.hits[:k]


CASE = {
    "case_id": "c-1", "split": "dev", "bucket": "ambiguous",
    "visible": {"title": "Please wait forever", "as_of": "2022-10-08T10:00:00Z",
                "body": "### Debug info\n\n- Streamlit version: 1.13.0\n- Python version: 3.8\n"},
    "filters": {"before": "2022-10-08T10:00:00Z", "exclude_issues": [1, 2]},
    "gold": {"outcome": "info_request", "relevant_sources": ["issue:10", "page:/knowledge-base/x"],
             "must_not": ["SECRET-GOLD"]},
}
HITS = [_hit("issue:10"), _hit("doc:x#a", "doc", "https://docs.streamlit.io/knowledge-base/x#a"),
        _hit("issue:11")]
GOOD = {"outcome": "info_request", "summary": "s", "known_facts": ["v1.13"],
        "hypotheses": [{"text": "websocket blocked", "basis": "similar_report", "sources": ["S1"]}],
        "unknowns": ["proxy?"], "next_step": {"type": "ask", "text": "How do you access the app?"},
        "questions": ["Is there a reverse proxy in front of the app?"],
        "user_reply": "This looks like [S1] or [S9].",
        "maintainer_summary": {"problem": "p", "environment": "e", "checks_done": "", "key_sources": ["S2"],
                               "open_questions": "", "next_step": "ask"},
        "confidence": "low"}


def test_baseline_prompt_and_filters():
    llm, retr = FakeLLM([GOOD]), FakeRetr(HITS)
    res = Baseline(llm, retriever=retr).run({k: CASE[k] for k in p1.PUBLIC_KEYS})
    q, f, k, mode = retr.calls[0]
    assert k == 10 and mode == "hybrid" and f.before == CASE["filters"]["before"] and f.exclude_issues == {1, 2}
    prompt = llm.calls[0]["messages"][1]["content"]
    assert "[S1] issue" in prompt and "Today is 2022-10-08" in prompt
    assert res["sources"]["S2"]["source_id"] == "doc:x#a"
    assert res["check"]["cited"] == ["S1", "S2"] and res["check"]["invalid_citations"] == ["S9"]


def test_gold_never_reaches_the_system(monkeypatch):
    llm, seen = FakeLLM([GOOD]), []
    sysm = Baseline(llm, retriever=FakeRetr(HITS))
    orig = sysm.run
    sysm.run = lambda case: (seen.append(case), orig(case))[1]
    monkeypatch.setattr(p1, "load_cases", lambda split, ids=None: [CASE])
    out = p1.run("baseline", "dev", llm=llm, system=sysm)
    assert set(seen[0]) == {"case_id", "visible", "filters"}
    assert "SECRET-GOLD" not in json.dumps(llm.calls)
    s = out["cases"][0]["score"]
    assert s["outcome_exact"] and s["bucket_ok"]
    assert s["retrieval_recall"] == 1.0                      # issue:10 + page match via doc url
    assert s["cited_recall"] == 1.0 and s["invalid_citations"] == 1
    assert s["redundant_q"] == []                            # asked about a proxy, not the version


def test_redundant_question_detected():
    out = {**GOOD, "questions": ["Which Streamlit version are you using?", "Which browser?"]}
    assert p1.redundant_questions(CASE["visible"], out) == ["streamlit_version"]   # browser not given


def test_contract_validation():
    bad = {**GOOD, "outcome": "solved", "next_step": "ask", "hypotheses": [{"text": "x", "basis": "vibes"}]}
    chk = validate(bad, {"S1": {}})
    assert len(chk["problems"]) == 3


def test_llm_failure_degrades_not_crashes():
    from ssa.llm.client import LLMError
    llm = FakeLLM([LLMError("bad json")])
    res = Baseline(llm, retriever=FakeRetr(HITS)).run({k: CASE[k] for k in p1.PUBLIC_KEYS})
    assert res["output"] == {} and "bad json" in res["error"]
    assert p1.score_case(CASE, res)["bucket_ok"] is False
