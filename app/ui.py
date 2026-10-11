"""Minimal demo UI for the Streamlit Support Agent (Problem 1 + Problem 2).

Run:   PYTHONPATH=src streamlit run app/ui.py
       SSA_UI_FAKE=1 PYTHONPATH=src streamlit run app/ui.py     # offline: scripted LLM, $0

Tabs: Analyse a report (P1) · Follow a case (P2) · Results. Logic lives in ssa.ui.backend;
this file only draws. Eval cases replay from the LLM cache ($0); new reports are live calls.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import streamlit as st

from ssa.agent.tools import ToolError
from ssa.ui import backend as B

st.set_page_config(page_title="Streamlit Support Agent", layout="wide")
OUT_COLOR = {"usage_guidance": "green", "known_issue": "blue", "info_request": "orange", "escalate": "red",
             "close_resolved": "green", "wait": "gray"}


@st.cache_resource(show_spinner="Loading index and LLM client ...")
def resources():
    return B.make_llm(), B.make_retriever()


LLM, RETR = resources()


def world() -> B.World:
    if "world" not in st.session_state:
        st.session_state.world = B.World(LLM, RETR)
    return st.session_state.world


def md_safe(text: str) -> str:
    """Issue text is user content: show its markdown headings as bold lines, not page titles."""
    return re.sub(r"^\s*#{1,6}\s*(.+)$", r"**\1**", text or "", flags=re.M)


def badge(outcome: str | None) -> str:
    return f":{OUT_COLOR.get(outcome, 'gray')}[**{(outcome or 'none').upper()}**]"


def cost_line(stats: dict) -> str:
    return (f"LLM calls: {stats.get('live_calls', 0)} live / {stats.get('replayed_calls', 0)} replayed · "
            f"tokens {stats.get('tokens_in', 0)} in / {stats.get('tokens_out', 0)} out · "
            f"USD {stats.get('usd_live', 0):.5f} live (+{stats.get('usd_replayed', 0):.5f} replayed)")


# ====================================================================== header
st.title("Streamlit Support Agent")
st.caption("QBC13 Agentic AI final project · domain: running & deploying Streamlit apps"
           + (" · **OFFLINE DEMO MODE (scripted LLM)**" if B.FAKE else ""))
tab1, tab2, tab3 = st.tabs(["Analyse a report (Problem 1)", "Follow a case (Problem 2)", "Results"])


# ====================================================================== tab 1: Problem 1
def show_p1(res: dict, case: dict | None, show_gold: bool):
    out = res.get("output") or {}
    tr = res.get("trace") or {}
    d = tr.get("decision") or {}
    st.markdown(f"#### {res['system']} → {badge(out.get('outcome'))}  confidence: {out.get('confidence')}")
    if res.get("error"):
        st.error(res["error"])
    if d:
        over = "" if d.get("agrees_with_llm", True) else " — **overridden by code**"
        st.markdown(f"LLM proposed **{d.get('proposed')}**{over}")
        for r in d.get("rules_applied") or []:
            st.markdown(f"- rule: `{r}`")
    else:
        st.caption("single LLM call, no code checks")
    if tr.get("facts"):
        env = {k: v for k, v in (tr["facts"].get("env") or {}).items() if v}
        st.markdown(f"**Facts from the report:** {env or 'no environment stated'}"
                    + (f" · symptoms: {', '.join(tr['facts'].get('symptoms') or [])}" if tr["facts"].get("symptoms") else ""))
    st.markdown("**Reply to the user**")
    st.info(out.get("user_reply") or "(none)")
    if out.get("questions"):
        st.markdown("**Questions:** " + " · ".join(out["questions"]))
    with st.expander("Evidence (relation per source, cited in the reply)", expanded=False):
        st.dataframe(B.evidence_rows(res), hide_index=True, width="stretch",
                     column_config={"url": st.column_config.LinkColumn("url")})
    with st.expander("Maintainer summary and hypotheses"):
        st.json({"maintainer_summary": out.get("maintainer_summary"), "hypotheses": out.get("hypotheses")})
    enf = tr.get("enforced") or {}
    if any(enf.values()):
        with st.expander("Corrected by code"):
            st.json(enf)
    st.caption(cost_line(res.get("stats") or {}))
    if show_gold and case:
        v = B.gold_verdict(case, out.get("outcome"))
        g = case["gold"]
        st.markdown(f"**Gold** (hidden from the system): `{g['outcome']}`"
                    + (f" (also acceptable: {', '.join(a for a in g.get('acceptable_outcomes', []) if a != g['outcome'])})"
                       if len(g.get("acceptable_outcomes") or []) > 1 else "")
                    + f" → {':green[MATCH]' if v == 'match' else ':orange[ACCEPTABLE]' if v == 'acceptable' else ':red[MISMATCH]'}"
                    + f" · reviewed by {g.get('reviewed_by')}")


with tab1:
    src = st.radio("Input", ["Evaluation case (dev)", "New report"], horizontal=True, key="p1_src")
    case = None
    if src.startswith("Evaluation"):
        cases = B.dev_cases()
        labels = {c["case_id"]: f"{c['case_id']} [{c['bucket']}] {c['visible']['title'][:70]}" for c in cases}
        cid = st.selectbox("Case", list(labels), format_func=labels.get, key="p1_case")
        case = B.find_case(cid)
        with st.expander("Report as the system sees it"):
            st.markdown(f"**{case['visible']['title']}** · as of {case['visible']['as_of'][:10]} · {case['visible']['url']}")
            st.text(case["visible"]["body"][:3000])
        pub = B.public(case)
    else:
        title = st.text_input("Title", "Stuck on loading screen on the server", key="p1_title")
        body = st.text_area("Report", "My app works on my laptop but on the server it stays on the loading screen. "
                            "Reinstalling did not help.", key="p1_body", height=120)
        as_of = st.text_input("As of (YYYY-MM-DD, empty = now)", "", key="p1_asof")
        pub = B.new_report(title, body, as_of or None)
    c1, c2, c3 = st.columns(3)
    system = c1.selectbox("System", B.P1_SYSTEMS, key="p1_system")
    compare = c2.checkbox("Compare with baseline", value=True, key="p1_compare")
    show_gold = c3.checkbox("Show gold after the result", value=False, key="p1_gold", disabled=case is None)
    if st.button("Analyse", type="primary", key="p1_run"):
        with st.spinner("Analysing ..."):
            st.session_state.p1 = {"main": B.analyse(system, pub, LLM, RETR), "case": case,
                                   "base": B.analyse("baseline", pub, LLM, RETR) if compare and system != "baseline" else None}
    r = st.session_state.get("p1")
    if r:
        if r["base"]:
            left, right = st.columns(2)
            with left:
                show_p1(r["main"], r["case"], show_gold)
            with right:
                show_p1(r["base"], r["case"], show_gold)
        else:
            show_p1(r["main"], r["case"], show_gold)


# ====================================================================== tab 2: Problem 2
def flash(kind: str, msg: str):
    st.session_state.flash = (kind, msg)


def act(fn, ok_msg):
    try:
        r = fn()
        if isinstance(r, dict) and r.get("ok") is False:
            flash("error", f"FAILED: {r.get('results')}")
        else:
            flash("success", ok_msg(r) if callable(ok_msg) else ok_msg)
    except ToolError as e:
        flash("error", f"Refused by the gate: {type(e).__name__}: {e}")


with tab2:
    W = world()
    top1, top2, top3 = st.columns([2, 2, 1])
    with top1:
        cases = B.dev_cases()
        labels = {c["case_id"]: f"{c['case_id']} {c['visible']['title'][:55]}" for c in cases}
        oc = st.selectbox("Open an evaluation case", list(labels), format_func=labels.get, key="p2_open_case")
        if st.button("Open case", key="p2_open"):
            st.session_state.p2_cid = W.open_eval_case(oc)
            flash("success", f"opened {oc}")
    with top2:
        opened = W.cases()
        if opened:
            cur = st.session_state.get("p2_cid")
            st.session_state.p2_cid = st.selectbox("Current case", opened, key="p2_cur",
                                                   index=opened.index(cur) if cur in opened else 0)
    with top3:
        if st.button("Reset tracker + memory", key="p2_reset"):
            W.reset()
            st.session_state.pop("p2_cid", None)
            flash("success", "tracker and case memory emptied")
    with st.expander("Open a new report"):
        nt = st.text_input("Title", key="p2_new_title")
        nb = st.text_area("Report", key="p2_new_body")
        if st.button("Open new report", key="p2_new_open") and nt and nb:
            st.session_state.p2_cid = W.open_new(nt, nb, None)
            flash("success", f"opened {st.session_state.p2_cid}")

    if "flash" in st.session_state:
        kind, msg = st.session_state.pop("flash")
        (st.success if kind == "success" else st.error)(msg)

    cid = st.session_state.get("p2_cid")
    if not cid or cid not in W.cases():
        st.info("Open a case to start.")
    else:
        left, right = st.columns([1, 1])
        with left:
            st.markdown("##### User")
            msg = st.text_area("User says ...", key="p2_msg", height=80)
            if st.button("Send as user", key="p2_say") and msg.strip():
                W.say(cid, msg.strip())
                flash("success", "user message added (ticket version bumped - a pending proposal is now stale)")
                st.rerun()
            st.markdown("##### Agent")
            if st.button("Run agent turn", type="primary", key="p2_turn"):
                with st.spinner("Agent turn ..."):
                    r = W.turn(cid)
                flash("success", f"agent turn: outcome {r['outcome']}" + (f" ({r['stopped']})" if r["stopped"] else "")
                      + f" · {cost_line(r['stats'])}")
                st.rerun()
            st.markdown("##### Maintainer")
            p = W.pending(cid)
            if not p:
                st.caption("No pending proposal.")
            else:
                st.session_state.p2_seen = {"id": p.id, "hash": p.hash}      # what the maintainer saw
                st.markdown(f"Pending proposal `{p.id}` · hash `{p.hash[:12]}` · status **{p.status}** "
                            f"· made for ticket version {p.ticket_version}")
                for a in p.actions:
                    if a["op"] == "post_comment":
                        st.info(a["text"])
                    else:
                        st.markdown(f"- `{a['op']}` {', '.join(f'{k}={v}' for k, v in a.items() if k != 'op')}")
                b1, b2 = st.columns(2)
                if b1.button("Approve", type="primary", key="p2_approve"):
                    seen = st.session_state.p2_seen
                    act(lambda: W.gate.approve(cid, seen["id"], seen["hash"]),
                        lambda r: "executed and verified" + (" (already done - nothing posted again)" if r.get("already_done") else ""))
                    st.session_state.p2_last_approved = seen
                    st.rerun()
                reason = b2.text_input("Reject reason", key="p2_reason")
                if b2.button("Reject", key="p2_reject"):
                    act(lambda: W.gate.reject(cid, p.id, p.hash, reason or "rejected"),
                        "rejected - nothing written; the reason is kept for the next draft")
                    st.rerun()
                with st.expander("Edit and approve"):
                    edited = st.text_area("Edited reply", p.comment_text() or "", key=f"p2_edit_{p.id}")
                    if st.button("Post edited text", key="p2_edit_go"):
                        act(lambda: W.gate.edit(cid, p.id, p.hash, edited), "edited text posted verbatim")
                        st.rerun()
            with st.expander("Simulate failures (safety demo)"):
                s1, s2, s3 = st.columns(3)
                if s1.button("Restart process", key="p2_restart"):
                    st.session_state.world = W.restart()
                    flash("success", "process restarted - state reloaded from disk")
                    st.rerun()
                if s2.button("Deliver last approval again", key="p2_dup"):
                    last = st.session_state.get("p2_last_approved")
                    if last:
                        act(lambda: W.gate.approve(cid, last["id"], last["hash"]),
                            lambda r: "duplicate delivery: " + ("already done - nothing posted again" if r.get("already_done") else "executed"))
                    else:
                        flash("error", "nothing approved yet")
                    st.rerun()
                if s3.button("Fail next post (after write)", key="p2_fail"):
                    W.fail_next_post()
                    flash("success", "the next agent comment will be written but its response lost")
                    st.rerun()
        with right:
            snap = W.snapshot(cid)
            t = snap["ticket"]
            st.markdown(f"**Status** `{t['status']}` · **ticket version** {t['version']} · "
                        f"**agent turns** {snap['turns']} · **steps** {snap['steps_total']} · "
                        f"**labels** {', '.join(t['labels']) or '-'}")
            st.markdown(f"##### Tracker thread — {t['title'][:70]}")
            with st.container(height=320):
                with st.chat_message("user"):
                    st.markdown(f"**Report** ({t['created_at'][:10]})\n\n{md_safe(t['body'][:1500])}")
                for c in t["comments"]:
                    with st.chat_message("user" if c["author"] == "user" else "assistant"):
                        st.markdown(f"*{c['author']}, {c['created_at'][:16]}*\n\n{md_safe(c['body'])}")
            st.markdown("##### Case memory")
            st.json({"facts": snap["facts"], "superseded (corrected)": snap["superseded"],
                     "tried by the user": [f"{x['text']} -> {x['result']}" for x in snap["tried"]],
                     "blocking unknowns": snap["unknowns"], "user confirmed fix": snap["confirmed"],
                     "maintainer rejection notes": snap["rejections"], "last decision": snap["last_decision"]},
                    expanded=True)
            with st.expander("Proposals"):
                st.dataframe(snap["proposals"], hide_index=True, width="stretch")
            with st.expander("Audit log (last 12)"):
                st.dataframe(snap["audit"], hide_index=True, width="stretch")


# ====================================================================== tab 3: results
with tab3:
    st.markdown("#### Problem 1 — dev split (16 cases), human-reviewed gold")
    rows, runs = B.p1_results()
    st.dataframe(rows, hide_index=True, width="stretch")
    with st.expander("Per case (✓ exact, ~ acceptable alternative, ✗ wrong)"):
        st.dataframe(B.p1_case_rows(runs), hide_index=True, width="stretch")
    st.markdown("#### Problem 2 — multi-turn scenarios (code checks on the stored tracker / memory)")
    p2 = B.p2_results()
    st.dataframe(p2, hide_index=True, width="stretch")
    for r in p2:
        with st.expander(f"{r['run']} — per scenario"):
            st.dataframe(B.p2_scenario_rows(r["run"]), hide_index=True, width="stretch")
    st.caption("Numbers come from the saved run files in data/eval/runs; the test split has not been run yet.")
