"""Offline tests for the final Problem-1 pipeline: decision rules and code enforcement (FakeLLM, fake index)."""
import json

from ssa.agent.baseline import SYSTEM, SYSTEM_V2, BaselineV2
from ssa.agent.pipeline import FinalP1
from ssa.eval import p1
from ssa.index.search import Hit
from ssa.llm.client import LLMError
from ssa.llm.fake import FakeLLM


def H(sid, typ, text="text", url=None):
    return Hit(chunk_id=sid + "-c", source_id=sid, type=typ, title="t " + sid, heading="h",
               url=url or f"https://x/{sid}", text=text, available_at="2022-01-01T00:00:00Z", score=0.1, ranks={})


DOC = H("doc:/deploy/remote#a", "doc", url="https://docs.streamlit.io/knowledge-base/deploy/remote-start#a")
ISSUE = H("issue:10", "issue")
ISSUE_M = H("issue:11", "comment", text="[maintainer, 2021-05-01]\nThis is caused by X, fixed in 1.20")
RN_NEW = H("rn:1.20.0:abcd1234", "release_note")
RN_OLD = H("rn:1.10.0:abcd5678", "release_note")


class FakeRetr:
    query_embeddings = 0

    def __init__(self, groups):
        self.groups, self.queries = groups, []

    def search_grouped(self, q, f, per_group=None, mode=None):
        self.queries.append(q)
        return {g: list(v) for g, v in self.groups.items()}


CASE = {"case_id": "c-1", "split": "dev", "bucket": "ambiguous",
        "visible": {"title": "Please wait forever on server", "as_of": "2022-10-08T10:00:00Z",
                    "body": "### Debug info\n\n- Streamlit version: 1.13.0\n\nWorks locally, stuck on server. Reinstalled."},
        "filters": {"before": "2022-10-08T10:00:00Z", "exclude_issues": [1]},
        "gold": {"outcome": "info_request", "relevant_sources": ["issue:10"], "must_not": ["GOLD-SECRET"]}}
PUBLIC = {k: CASE[k] for k in p1.PUBLIC_KEYS}
FACTS = {"symptoms": ["stuck on Please wait"], "error_messages": [], "setup": "server", "tried": ["reinstall"],
         "env_quotes": {"python_version": "3.11", "os": None}, "search_queries": ["websocket proxy", "please wait"]}


def draft(**kw):
    d = {"outcome": "known_issue", "summary": "s", "known_facts": [], "unknowns": [],
         "hypotheses": [{"text": "proxy blocks websocket", "basis": "documented", "sources": ["S3"]}],
         "next_step": {"type": "answer", "text": "x"},
         "questions": ["Which Streamlit version are you using?", "Is there a reverse proxy?",
                       "Do you see errors in the browser console?"],
         "user_reply": "See [S1] and [S3] and [S2].",
         "maintainer_summary": {"problem": "p [S2]", "key_sources": ["S1", "S2"]}, "confidence": "high"}
    d.update(kw)
    return d


def run(groups, assess, drft=None, facts=FACTS):
    llm = FakeLLM([facts, assess, drft or draft()])
    sysm = FinalP1(llm, retriever=FakeRetr(groups))
    return sysm.run(PUBLIC), llm, sysm


GROUPS = {"docs": [DOC], "release_notes": [], "issues": [ISSUE, ISSUE]}   # duplicate issue chunk


def test_similar_issue_is_not_a_known_issue():
    assess = {"assessments": [{"source": "S1", "relation": "irrelevant"},
                              {"source": "S2", "relation": "similar_symptom"}],
              "hypotheses": [{"text": "proxy blocks websocket", "supported_by": ["S2"],
                              "check": "Is there a reverse proxy in front of the app?"}],
              "proposed_outcome": "known_issue"}
    res, llm, sysm = run(GROUPS, assess)
    out, tr = res["output"], res["trace"]
    assert len(llm.calls) == 3
    assert list(res["sources"]) == ["S1", "S2"]                       # docs first, duplicate merged
    assert tr["decision"]["outcome"] == "info_request" and out["outcome"] == "info_request"
    assert any("not allowed" in r for r in tr["decision"]["rules_applied"])
    assert out["next_step"]["type"] == "ask"
    assert out["confidence"] == "medium"                              # from evidence, not "high"
    assert "[S1]" not in out["user_reply"] and "[S3]" not in out["user_reply"]   # irrelevant / unknown
    assert out["maintainer_summary"]["key_sources"] == ["S2"]
    assert out["questions"] == ["Is there a reverse proxy?", "Do you see errors in the browser console?"]
    assert "Which Streamlit version are you using?" in tr["enforced"]["dropped_questions"]
    assert out["hypotheses"][0]["basis"] == "assistant_hypothesis"    # cited S3 does not exist
    assert "GOLD-SECRET" not in json.dumps(llm.calls)
    # env quote not in the report is rejected; queries include the LLM's
    assert tr["facts"]["env"]["python_version"] is None and tr["env_quotes_rejected"] == ["python_version"]
    assert "websocket proxy" in tr["queries"]
    assert "do NOT recommend upgrading" in llm.calls[2]["messages"][1]["content"]


def test_known_issue_needs_newer_fix_release_note():
    groups = {"docs": [], "release_notes": [RN_NEW, RN_OLD], "issues": []}
    a = {"relations": {"S1": "same_cause", "S2": "same_cause"},
         "key_sources": [{"source": "S1", "resolves": True}, {"source": "S2", "resolves": True}],
         "hypotheses": [], "proposed_outcome": "known_issue"}
    res, llm, _ = run(groups, a, draft(hypotheses=[{"text": "bug", "sources": ["S1"], "basis": "similar_report"}]))
    d = res["trace"]["decision"]
    assert d["outcome"] == "known_issue" and d["fix_support"] == ["S1"]
    assert d["fix_already_in_user_version"] == ["S2"] and d["upgrade_source"] == "S1"
    assert res["output"]["hypotheses"][0]["basis"] == "documented"      # release note -> documented
    assert "fixed in version 1.20.0" in llm.calls[2]["messages"][1]["content"]


def test_old_fix_only_is_downgraded():
    groups = {"docs": [], "release_notes": [RN_OLD], "issues": []}
    a = {"relations": {"S1": "same_cause"}, "key_sources": [{"source": "S1", "resolves": True}],
         "hypotheses": [], "proposed_outcome": "known_issue"}
    res, _, _ = run(groups, a)
    assert res["trace"]["decision"]["outcome"] == "escalate"           # no check to ask -> escalate


def test_maintainer_statement_supports_known_issue_and_doc_supports_guidance():
    d = FinalP1.decide("known_issue", {"S1": "same_cause"}, [], {"S1": {"type": "comment", "source_id": "issue:11"}},
                       {"S1": ISSUE_M}, "1.13.0")
    assert d["outcome"] == "known_issue" and d["maintainer_support"] == ["S1"]
    d = FinalP1.decide("known_issue", {"S1": "same_cause"}, [], {"S1": {"type": "issue", "source_id": "issue:10"}},
                       {"S1": ISSUE}, "1.13.0")
    assert d["outcome"] == "escalate"
    d = FinalP1.decide("usage_guidance", {"S1": "same_cause"}, [], {"S1": {"type": "doc", "source_id": "doc:x"}},
                       {"S1": DOC}, None)
    assert d["outcome"] == "usage_guidance" and d["confidence"] == "medium"


def test_llm_failures_degrade():
    llm = FakeLLM([LLMError("facts down"), LLMError("assess down"), LLMError("draft down")])
    res = FinalP1(llm, retriever=FakeRetr(GROUPS)).run(PUBLIC)
    assert res["output"] == {} and res["error"] == "draft down"
    assert len(res["trace"]["errors"]) == 3 and res["trace"]["decision"]["outcome"] == "escalate"


def test_baseline_v2_only_adds_rules():
    assert "NOT proof" in SYSTEM_V2 and "NOT proof" not in SYSTEM
    assert SYSTEM_V2.replace(SYSTEM_V2[SYSTEM_V2.index("Rules:"):SYSTEM_V2.index("Outcomes:")], "") == SYSTEM
    assert BaselineV2.name == "baseline-v2"


def test_scorer_new_metrics():
    srcs = {"S1": {"type": "issue", "source_id": "issue:1", "url": "u"},
            "S2": {"type": "issue", "source_id": "issue:1", "url": "u"}}
    out = {"hypotheses": [{"basis": "documented", "sources": ["S1"]}],
           "questions": ["Please provide your Streamlit version, Python version, OS and browser details."],
           "user_reply": "Upgrade to latest [S1] [S2]"}
    assert p1.basis_mislabels(out, srcs) == (1, 1)
    assert p1.checklist_question(out)
    assert p1.unsupported_upgrade(out, srcs, ["S1", "S2"])


def _decide(proposed="known_issue", rel=None, typ="doc", sid="doc:x", hit=DOC, uv="1.13.0", **sig):
    return FinalP1.decide(proposed, {"S1": rel} if rel else {}, [{"text": "h", "check": "c"}],
                          {"S1": {"type": typ, "source_id": sid}}, {"S1": hit}, uv, signals=sig)


def test_decision_table():
    # fix in a release note but user version unknown -> ask for the version
    assert _decide(rel="same_cause", typ="release_note", sid="rn:1.20.0:x", hit=RN_NEW, uv=None)["outcome"] == "info_request"
    # same-cause docs -> guidance, unless something blocking is missing
    assert _decide(rel="same_cause")["outcome"] == "usage_guidance"
    assert _decide(rel="same_cause", blocking_unknowns=["how is the app accessed?"])["outcome"] == "info_request"
    # concrete error and nothing explains it -> escalate (even if the LLM wanted to ask)
    d = _decide(proposed="info_request", rel="similar_symptom", typ="issue", sid="issue:10", hit=ISSUE,
                report_has_concrete_error_or_repro=True)
    assert d["outcome"] == "escalate" and d["agrees_with_llm"] is False
    # no qualifying evidence: an allowed proposal is kept
    assert _decide(proposed="info_request", rel="background")["outcome"] == "info_request"


class RowsRetr(FakeRetr):
    """Fake retriever exposing index rows, for the maintainer attachment."""
    def __init__(self, groups, rows):
        super().__init__(groups)
        self.rows = rows

    def _allowed(self, i, f):
        av = self.rows[i]["available_at"]
        return not (f.before and av and av >= f.before)


def test_maintainer_reply_is_attached_but_respects_the_date_filter():
    rows = [{"type": "comment", "source_id": "issue:10", "available_at": "2023-01-01T00:00:00Z",
             "text": "[maintainer, 2023-01-01]\nFUTURE answer"},                    # after as_of -> hidden
            {"type": "comment", "source_id": "issue:10", "available_at": "2022-05-01T00:00:00Z",
             "text": "[user, 2022-04-01]\nme too\n\n[maintainer, 2022-05-01]\nUse server.enableCORS=false"}]
    a = {"relations": {"S1": "same_cause"}, "key_sources": [{"source": "S1", "resolves": True}],
         "hypotheses": [], "proposed_outcome": "known_issue"}
    llm = FakeLLM([FACTS, a, draft()])
    res = FinalP1(llm, retriever=RowsRetr({"docs": [], "release_notes": [], "issues": [ISSUE]}, rows)).run(PUBLIC)
    prompt = llm.calls[1]["messages"][1]["content"]
    assert "Use server.enableCORS=false" in prompt and "FUTURE answer" not in prompt
    assert res["sources"]["S1"]["maintainer"] is True
    assert res["trace"]["decision"]["outcome"] == "known_issue"            # maintainer statement qualifies
    assert res["trace"]["unassessed"] == []


def test_blocking_unknowns_beat_maintainer_support_and_resolves_is_required():
    src = {"S1": {"type": "comment", "source_id": "issue:11"}}
    d = FinalP1.decide("known_issue", {"S1": "same_cause"}, [], src, {"S1": ISSUE_M}, None,
                       signals={"blocking_unknowns": ["Streamlit version"]}, resolves={"S1"})
    assert d["outcome"] == "info_request"
    d = FinalP1.decide("known_issue", {"S1": "same_cause"}, [], src, {"S1": ISSUE_M}, None, signals={}, resolves=set())
    assert d["outcome"] == "escalate"                       # maintainer commented but did not resolve it
    d = FinalP1.decide("known_issue", {"S1": "same_cause"}, [], src, {"S1": ISSUE_M}, None, signals={}, resolves={"S1"})
    assert d["outcome"] == "known_issue"
    # a newer fixing release note with a known version still wins over blocking unknowns
    d = FinalP1.decide("info_request", {"S1": "same_cause"}, [], {"S1": {"type": "release_note", "source_id": "rn:1.20.0:x"}},
                       {"S1": RN_NEW}, "1.13.0", signals={"blocking_unknowns": ["x"]}, resolves={"S1"})
    assert d["outcome"] == "known_issue"


def test_compact_relations_format_and_label_list_in_prompt():
    a = {"relations": {"S1": "background", "S2": "similar_symptom"}, "key_sources": [{"source": "S2", "why": "w"}],
         "hypotheses": [], "proposed_outcome": "info_request"}
    res, llm, _ = run(GROUPS, a)
    assert res["trace"]["assessments"] == {"S1": "background", "S2": "similar_symptom"}
    assert res["trace"]["unassessed"] == [] and res["trace"]["assessment_reasons"] == {"S2": "w"}
    assert "Labels to assess (all of them): S1, S2" in llm.calls[1]["messages"][1]["content"]


def test_env_fields_only_block_when_a_fix_depends_on_version_and_resolves_without_relation():
    src = {"S1": {"type": "config_option", "source_id": "config:client.toolbarMode"}}
    d = FinalP1.decide("info_request", {}, [], src, {"S1": DOC}, None,
                       signals={"blocking_unknowns": ["Streamlit version", "python_version"]}, resolves={"S1"})
    assert d["outcome"] == "usage_guidance" and d["env_unknowns_ignored"]
    src = {"S1": {"type": "comment", "source_id": "issue:11"}}
    d = FinalP1.decide("known_issue", {"S1": "same_cause"}, [], src, {"S1": ISSUE_M}, None,
                       signals={"blocking_unknowns": ["Streamlit version"]}, resolves={"S1"})
    assert d["outcome"] == "info_request"            # version matters: a maintainer-stated fix exists
