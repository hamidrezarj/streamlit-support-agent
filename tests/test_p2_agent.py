"""Offline tests for the Problem-2 orchestrator and scenario runner (scripted LLM, fake index)."""
import re

import pytest
from ssa.agent.audit import AuditLog
from ssa.agent.orchestrator import CLOSING_TEXT, Agent
from ssa.agent.pipeline import FinalP1
from ssa.agent.state import CaseStore, facts_view, superseded
from ssa.agent.tools import MaintainerGate, Toolbox
from ssa.eval import p2
from ssa.index.search import Hit
from ssa.llm.fake import FakeLLM
from ssa.tracker.store import Tracker


def _hit(sid, typ):
    return Hit(chunk_id=sid, source_id=sid, type=typ, title="t " + sid, heading="h", url="https://x/" + sid,
               text="evidence text", available_at=None if typ == "doc" else "2021-01-01T00:00:00Z", score=0.1, ranks={})


class FakeRetr:
    query_embeddings = 0

    def search(self, q, f, k=10, mode=None, **kw):
        return [_hit("doc:a", "doc"), _hit("issue:1", "issue")]

    def search_grouped(self, q, f, per_group=None, mode=None):
        return {"docs": [_hit("doc:a", "doc")], "release_notes": [], "issues": [_hit("issue:1", "issue")]}


def scripted(outcome="info_request", fail_tried=False):
    """A fake LLM that answers every prompt type of the pipeline + memory update."""
    def answer(messages, **kw):
        sysm, user = messages[0]["content"], messages[-1]["content"]
        if sysm.startswith("You update the memory"):
            new = user.split("NEW messages:", 1)[1]
            facts, tried = [], []
            m = re.search(r"\d+\.\d+\.\d+", new)
            if m:
                facts.append({"name": "streamlit_version", "value": m.group(0), "quote": m.group(0),
                              "corrects": "streamlit_version"})
            if "nginx" in new:
                facts.append({"name": "access_path", "value": "nginx reverse proxy under /st/", "quote": "nginx",
                              "corrects": "access_path"})
            elif "directly" in new:
                facts.append({"name": "access_path", "value": "direct ip", "quote": "directly"})
            if "invented" in new:
                facts.append({"name": "os", "value": "Windows", "quote": "I use Windows 11"})   # not in text
            if "tried" in new:
                tried.append({"text": "set server.address", "result": "did_not_help" if fail_tried else "unknown",
                              "quote": "tried", "was_our_suggestion": "proposed" in new})
            ok = "working now" in new.lower()
            return {"facts": facts, "tried": tried, "user_confirmed_fix": ok,
                    "confirmation_quote": "working now" if ok else None}
        if sysm.startswith("You analyse"):
            return {"symptoms": ["stuck"], "search_queries": ["q"], "env_quotes": {}}
        if "Assess EVERY" in sysm:
            return {"relations": {"S1": "background", "S2": "similar_symptom"}, "hypotheses":
                    [{"text": "proxy", "supported_by": ["S2"], "check": "Is there a proxy?"}],
                    "blocking_unknowns": ["how the app is accessed"] if outcome == "info_request" else [],
                    "proposed_outcome": outcome}
        if sysm.startswith("You write the final"):
            return {"outcome": outcome, "summary": "s", "known_facts": [], "hypotheses": [], "unknowns": [],
                    "next_step": {"type": "ask", "text": "x"}, "questions": ["Is there a reverse proxy?"],
                    "user_reply": f"Draft reply ({len(user)} chars) [S2].", "maintainer_summary": {"problem": "p"},
                    "confidence": "low"}
        return {"outcome": "known_issue", "user_reply": "baseline reply", "maintainer_summary": {},
                "next_step": {"type": "answer", "text": "x"}}
    return FakeLLM(answer)


@pytest.fixture
def world(tmp_path):
    def make(llm, limits=None):
        tr, st = Tracker(tmp_path / "t.sqlite"), CaseStore(tmp_path / "s.sqlite")
        tb = Toolbox(tr, st, AuditLog(tmp_path / "a.jsonl"), FakeRetr(), limits=limits)
        agent = Agent(tb, FinalP1(llm, retriever=FakeRetr()), llm, exclude_issues=[1])
        agent.open_case("c-1", "App stuck on Please wait", "### Debug info\n- Streamlit version: 1.13.0",
                        created_at="2022-10-08T10:00:00Z")
        return agent, MaintainerGate(tb), tr, st
    return make


def test_first_turn_proposes_and_never_acts(world):
    agent, gate, tr, st = world(scripted())
    r = agent.turn("c-1")
    assert r["outcome"] == "info_request" and r["proposal"].status == "pending"
    assert tr.get("c-1")["comments"] == []                          # nothing visible before approval
    assert facts_view(st.load("c-1"))["streamlit_version"] == "1.13.0"     # seeded from the report
    acts = {a["op"]: a for a in r["proposal"].actions}
    assert acts["set_status"]["status"] == "awaiting_user" and "reverse proxy" in acts["post_comment"]["text"]
    assert st.load("c-1")["steps_turn"] <= 8


def test_correction_supersedes_and_rejects_unquoted_facts(world):
    agent, gate, tr, st = world(scripted())
    p = agent.turn("c-1")["proposal"]
    gate.approve("c-1", p.id, p.hash)
    agent.user_says("c-1", "I open it directly. Some invented detail.", created_at="2022-10-09T10:00:00Z")
    r = agent.turn("c-1")
    assert "os" in r["memory"]["rejected_quotes"]                    # quote not in the message -> not stored
    agent.user_says("c-1", "Correction: it is behind our nginx.", created_at="2022-10-09T11:00:00Z")
    agent.turn("c-1")
    s = st.load("c-1")
    assert "nginx" in facts_view(s)["access_path"] and superseded(s) == ["access_path=direct ip"]
    assert "No longer true" in agent._report(tr.get("c-1"), s)


def test_user_confirmation_closes_only_with_approval(world):
    agent, gate, tr, st = world(scripted("usage_guidance"))
    p = agent.turn("c-1")["proposal"]
    gate.approve("c-1", p.id, p.hash)
    agent.user_says("c-1", "It's working now. Thank you.", created_at="2022-10-09T10:00:00Z")
    r = agent.turn("c-1")
    assert r["outcome"] == "close_resolved" and tr.get("c-1")["status"] != "closed_resolved"
    gate.approve("c-1", r["proposal"].id, r["proposal"].hash)
    t = tr.get("c-1")
    assert t["status"] == "closed_resolved" and t["comments"][-1]["body"] == CLOSING_TEXT


def test_failed_fix_without_new_evidence_escalates(world):
    agent, gate, tr, st = world(scripted("info_request", fail_tried=True))
    p = agent.turn("c-1")["proposal"]
    gate.approve("c-1", p.id, p.hash)
    agent.user_says("c-1", "I tried the proposed setting, it did not help.", created_at="2022-10-09T10:00:00Z")
    r = agent.turn("c-1")
    assert r["outcome"] == "escalate"
    gate.approve("c-1", r["proposal"].id, r["proposal"].hash)
    t = tr.get("c-1")
    assert t["status"] == "escalated" and "needs-maintainer" in t["labels"]
    assert "Already tried by the user" in agent._report(t, st.load("c-1"))


def test_step_limit_gives_an_escalation_proposal(world):
    agent, gate, tr, st = world(scripted(), limits={"steps_per_turn": 3})
    r = agent.turn("c-1")
    assert r["outcome"] == "escalate" and "step budget" in r["stopped"]
    assert r["proposal"].status == "pending"                          # still needs the maintainer


def test_no_new_message_means_wait(world):
    agent, gate, tr, st = world(scripted())
    p1 = agent.turn("c-1")["proposal"]
    r = agent.turn("c-1")
    assert r["outcome"] == "wait" and r["proposal"].id == p1.id


# ------------------------------------------------------------------ runner on the real scenario files
def test_runner_dev_scenarios_offline(tmp_path):
    res = p2.run("final", "dev", llm=scripted(), retriever=FakeRetr(), outdir=tmp_path / "final")
    m = res["metrics"]
    assert m["n_scenarios"] == 5 and not m["violations"]               # every invariant holds
    s8364 = next(r for r in res["scenarios"] if r["sid"] == "s-8364")
    assert s8364["checks"]["inv:pending_survives_restart"]["ok"]
    s9781 = next(r for r in res["scenarios"] if r["sid"] == "s-9781")
    assert s9781["checks"]["inv:no_duplicate_execution"]["ok"] and s9781["final"]["status"] == "closed_resolved"
    s7867 = next(r for r in res["scenarios"] if r["sid"] == "s-7867")
    assert s7867["checks"]["posted_text_equals_edited"]["ok"]
    assert (tmp_path / "final" / "s-8364" / "audit.jsonl").exists()


def test_runner_baseline_shows_the_unsafe_behaviour(tmp_path):
    res = p2.run("baseline", "dev", llm=scripted(), retriever=FakeRetr(), outdir=tmp_path / "base")
    s9781 = next(r for r in res["scenarios"] if r["sid"] == "s-9781")
    assert not s9781["checks"]["inv:no_duplicate_execution"]["ok"]    # duplicate delivery posts twice


def test_runner_case_switch_and_stale_on_test_split(tmp_path):
    res = p2.run("final", "test", ids=["s-9140", "s-11500"], llm=scripted(), retriever=FakeRetr(),
                 outdir=tmp_path / "t")
    sids = {r["sid"] for r in res["scenarios"]}
    assert sids == {"s-9140", "s-11509", "s-11500"}                     # switch partner pulled in
    assert not res["metrics"]["violations"]
    s11500 = next(r for r in res["scenarios"] if r["sid"] == "s-11500")
    assert any(e.get("error") == "StaleProposal" for e in s11500["log"])


def test_cli_case_flow(tmp_path, monkeypatch, capsys):
    from ssa import cli
    llm = scripted()
    tr, st = Tracker(tmp_path / "t.sqlite"), CaseStore(tmp_path / "s.sqlite")
    tb = Toolbox(tr, st, AuditLog(tmp_path / "a.jsonl"), FakeRetr())
    agent = Agent(tb, FinalP1(llm, retriever=FakeRetr()), llm, exclude_issues=[1])
    monkeypatch.setattr(cli, "_live", lambda with_agent=False: (tb, MaintainerGate(tb), agent))
    run = lambda *argv: cli.main(["--no-color", "case", *argv])
    assert run("open", "--case", "c-5500") == 0
    assert run("turn", "--case", "c-5500") == 0
    p = MaintainerGate(tb).pending("c-5500")
    out = capsys.readouterr().out
    assert p.id in out and "case approve" in out
    with pytest.raises(SystemExit, match="hash does not match"):
        run("approve", "--case", "c-5500", "--proposal", p.id, "--hash", "deadbeef00")
    assert run("approve", "--case", "c-5500", "--proposal", p.id, "--hash", p.hash[:12]) == 0
    assert run("say", "--case", "c-5500", "--text", "Version is 1.13.0 and I open it directly") == 0
    assert run("status", "--case", "c-5500") == 0
    out = capsys.readouterr().out
    assert "executed" in out and "status=awaiting_user" in out and "[user," in out


def test_users_own_failed_attempt_does_not_trigger_escalation(world):
    agent, gate, tr, st = world(scripted("info_request", fail_tried=True))
    p = agent.turn("c-1")["proposal"]
    gate.approve("c-1", p.id, p.hash)
    agent.user_says("c-1", "Before you answered, I tried reinstalling; it did not help.", created_at="2022-10-09T10:00:00Z")
    r = agent.turn("c-1")                                             # tried, but not our suggestion
    assert r["outcome"] == "info_request"


def test_report_versions_are_cleaned_and_same_version_is_no_correction(world):
    agent, gate, tr, st = world(scripted())
    tr.con.execute("UPDATE tickets SET body=? WHERE id='c-1'",
                   ("### Debug info\n- Streamlit version: (get it with `$ streamlit version`)1.13.0",))
    p = agent.turn("c-1")["proposal"]
    assert facts_view(st.load("c-1"))["streamlit_version"] == "1.13.0"
    gate.approve("c-1", p.id, p.hash)
    agent.user_says("c-1", "Version is 1.13.0", created_at="2022-10-09T10:00:00Z")
    agent.turn("c-1")
    assert superseded(st.load("c-1")) == []                           # same version: not a correction
