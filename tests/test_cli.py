"""Offline tests for the CLI (fake LLM, fake retriever, no index)."""
import json

from ssa import cli
from ssa.agent.baseline import Baseline
from ssa.agent.pipeline import FinalP1
from ssa.index.search import Hit
from ssa.llm.fake import FakeLLM


def _hit(sid, typ):
    return Hit(chunk_id=sid, source_id=sid, type=typ, title="t " + sid, heading="h", url="https://x/" + sid,
               text="text", available_at=None if typ == "doc" else "2022-01-01T00:00:00Z", score=0.1, ranks={})


class FakeRetr:
    query_embeddings = 0
    def __init__(self):
        self.filters = []
    def search(self, q, f, k=10, mode=None, **kw):
        self.filters.append(f)
        return [_hit("doc:a", "doc"), _hit("issue:1", "issue")]
    def search_grouped(self, q, f, per_group=None, mode=None):
        self.filters.append(f)
        return {"docs": [_hit("doc:a", "doc")], "release_notes": [], "issues": [_hit("issue:1", "issue")]}


OUT = {"outcome": "usage_guidance", "summary": "s", "known_facts": [], "unknowns": [],
       "hypotheses": [{"text": "config", "basis": "documented", "sources": ["S1"]}],
       "next_step": {"type": "answer", "text": "set server.enableCORS"}, "questions": [],
       "user_reply": "Do X [S1].", "maintainer_summary": {"problem": "p", "key_sources": ["S1"]}, "confidence": "high"}


def _args(**kw):
    a = cli.build_parser().parse_args(["analyze"] + kw.pop("argv", []))
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_new_report_from_file_uses_leakage_filters(tmp_path, capsys):
    body = tmp_path / "r.txt"
    body.write_text("My app hangs on Please wait behind nginx. Streamlit version: 1.30.0")
    retr = FakeRetr()
    llm = FakeLLM([{"symptoms": ["hang"], "search_queries": ["nginx websocket"]},
                   {"relations": {"S1": "same_cause", "S2": "background"},
                    "key_sources": [{"source": "S1", "resolves": True}], "hypotheses": [],
                    "proposed_outcome": "usage_guidance"}, OUT])
    a = _args(argv=["--title", "Please wait", "--body-file", str(body), "--as-of", "2024-05-01",
                    "--save", str(tmp_path / "out.json")])
    rc = cli.cmd_analyze(a, cli.Style(False), llm=llm, system=FinalP1(llm, retriever=retr))
    text = capsys.readouterr().out
    assert rc == 0
    assert "USAGE GUIDANCE" in text and "RESOLVES" in text and "REPLY TO THE USER" in text
    f = retr.filters[0]
    assert f.before == "2024-05-01T00:00:00Z" and len(f.exclude_issues) == 32       # eval issues always excluded
    saved = json.loads((tmp_path / "out.json").read_text())
    assert saved["case_id"].startswith("new-") and saved["trace"]["decision"]["outcome"] == "usage_guidance"


def test_baseline_output_renders_without_trace(capsys):
    llm = FakeLLM([OUT])
    pub = cli.new_report("t", "b", "2023-01-01")
    res = Baseline(llm, retriever=FakeRetr()).run(pub)
    text = cli.render(res, pub["visible"], pub["case_id"], "baseline", llm.stats, cli.Style(False))
    assert "single LLM call, no code checks" in text and "Do X [S1]." in text


def test_test_cases_need_confirmation():
    import pytest
    a = _args(argv=["--case", "c-9140"])            # a test-split case
    with pytest.raises(SystemExit, match="confirm-test"):
        cli.cmd_analyze(a, cli.Style(False), llm=FakeLLM([]), system=object())


def test_gold_is_rendered_only_on_request():
    case = cli.find_case("c-8149")
    g = cli.render_gold(case, "escalate", cli.Style(False))
    assert "MATCH" in g and "MISMATCH" not in g
