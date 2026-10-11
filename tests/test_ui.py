"""Click-through tests for the demo UI (Streamlit AppTest, scripted offline LLM, $0)."""
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")
APP = str(Path(__file__).resolve().parents[1] / "app" / "ui.py")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SSA_UI_FAKE", "1")
    monkeypatch.setenv("SSA_UI_DATA", str(tmp_path / "ui"))
    from ssa.ui import backend as B
    monkeypatch.setattr(B, "FAKE", True)
    at = st_testing.AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def texts(at) -> str:
    return " ".join(str(x.value) for x in list(at.markdown) + list(at.info) + list(at.success) + list(at.error)
                    + list(at.caption))


def test_problem1_tab_analyses_and_compares(app):
    app.button(key="p1_run").click().run()
    assert not app.exception
    t = texts(app)
    assert "final" in t and "baseline" in t and "INFO_REQUEST" in t
    assert "(offline demo)" in t                                   # final reply rendered
    app.checkbox(key="p1_gold").check().run()
    assert "Gold" in texts(app)


def test_problem2_tab_full_safety_flow(app):
    app.button(key="p2_open").click().run()
    app.button(key="p2_turn").click().run()
    assert not app.exception and "agent turn: outcome info_request" in texts(app)
    W = app.session_state["world"]
    cid = app.session_state["p2_cid"]
    assert W.tracker.get(cid)["comments"] == []                    # nothing visible before approval
    app.button(key="p2_approve").click().run()
    assert "executed and verified" in texts(app)
    assert len([c for c in W.tracker.get(cid)["comments"] if c["author"] == "agent"]) == 1
    app.button(key="p2_dup").click().run()                         # duplicate delivery
    assert "already done" in texts(app)
    assert len([c for c in W.tracker.get(cid)["comments"] if c["author"] == "agent"]) == 1
    app.text_area(key="p2_msg").input("I open it directly at the server IP.").run()
    app.button(key="p2_say").click().run()
    app.button(key="p2_turn").click().run()
    app.text_area(key="p2_msg").input("Correction: it's behind our nginx.").run()
    app.button(key="p2_say").click().run()
    app.button(key="p2_approve").click().run()                     # approve the proposal seen BEFORE the message
    assert "StaleProposal" in texts(app)
    app.button(key="p2_restart").click().run()                     # crash + restart
    W2 = app.session_state["world"]
    assert W2 is not W
    app.button(key="p2_turn").click().run()
    snap = W2.snapshot(cid)
    assert "nginx" in snap["facts"]["access_path"] and snap["superseded"]   # correction kept in memory
    app.button(key="p2_reject").click().run()
    assert "rejected" in texts(app)


def test_results_tab_reads_saved_runs(app):
    t = " ".join(str(d.value) for d in app.dataframe)
    assert "final-v4" in t
