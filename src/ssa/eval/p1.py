"""Run a Problem-1 system over eval cases and score it (deterministic metrics).

Run:  python -m ssa.eval.p1 --system baseline --split dev
      python -m ssa.eval.p1 --system baseline --split dev --cases c-5500 c-8149   # subset
      LLM_CACHE_MODE=replay python -m ssa.eval.p1 ...                             # offline, $0
Output: data/eval/runs/p1_<system>_<split>.json   (per-case outputs + metrics + cost)

Each system only gets {case_id, visible, filters} - gold is stripped here.
Metrics (judge-free; must_not / reply quality come from the LLM judge later):
  outcome_exact      outcome == gold outcome
  bucket_ok          outcome is the right *kind* for the bucket (answerable ->
                     usage_guidance|known_issue, ambiguous -> info_request, escalate -> escalate)
  retrieval_recall   labelled relevant sources among what the system retrieved
  cited_recall       labelled relevant sources the system actually cited
  invalid_citations  [S#] that point to no shown source (should be 0)
  redundant_q        asks for an env field the report already gives (brief: don't ask a fixed list)
  cost               live vs replayed calls, tokens, USD, latency, steps
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from types import SimpleNamespace

from ..data import config as C
from ..data.clean_issues import extract_env
from ..data.io_utils import read_json, utcnow, write_json
from ..agent.schema import BUCKET_OUTCOMES, OUTCOMES
from .retrieval_gold import match

RUNS = C.DATA / "eval" / "runs"
PUBLIC_KEYS = ("case_id", "visible", "filters")

from ..agent.pipeline import asked_fields      # same question patterns as the pipeline filter


def load_cases(split: str, ids: list[str] | None = None) -> list[dict]:
    cases = [read_json(p) for p in sorted((C.DATA / "eval" / "cases" / split).glob("*.json"))]
    return [c for c in cases if not ids or c["case_id"] in ids]


SYSTEMS = ("baseline", "baseline-v2", "final")


def make_system(name: str, llm, **kw):
    if name == "baseline":
        from ..agent.baseline import Baseline
        return Baseline(llm, **kw)
    if name == "baseline-v2":
        from ..agent.baseline import BaselineV2
        return BaselineV2(llm, **kw)
    if name == "final":
        from ..agent.pipeline import FinalP1
        return FinalP1(llm, **kw)
    raise SystemExit(f"unknown system {name!r} (choose from {SYSTEMS})")


def _git_rev() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=C.ROOT, capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


# ------------------------------------------------------------------ scoring
def _asked_text(out: dict) -> list[str]:
    qs = [q for q in (out.get("questions") or []) if isinstance(q, str)]
    ns = out.get("next_step") or {}
    if isinstance(ns, dict) and ns.get("type") == "ask":
        qs.append(ns.get("text") or "")
    return qs


def redundant_questions(visible: dict, out: dict) -> list[str]:
    """Env fields the system asks for although the report already gives them."""
    env = extract_env(visible["body"])
    asked = set().union(*[set(asked_fields(q)) for q in _asked_text(out)] or [set()])
    return sorted(f for f in asked if env.get(f))


def checklist_question(out: dict) -> bool:
    """One question/next step asking for >=3 env fields at once (the generic checklist)."""
    return any(len(asked_fields(q)) >= 3 for q in _asked_text(out))


def basis_mislabels(out: dict, srcs: dict) -> tuple[int, int]:
    """(mislabelled, total) hypotheses: basis claimed vs types of the sources it cites."""
    bad = tot = 0
    for h in out.get("hypotheses") or []:
        if not isinstance(h, dict):
            continue
        tot += 1
        types = {srcs[s.strip("[]")]["type"] for s in h.get("sources") or []
                 if isinstance(s, str) and s.strip("[]") in srcs}
        docish = bool(types & {"doc", "config_option", "release_note"})
        b = h.get("basis")
        if (b == "documented" and not docish) or (b == "similar_report" and not types & {"issue", "comment"}) \
                or (b == "assistant_hypothesis" and types):
            bad += 1
    return bad, tot


def unsupported_upgrade(out: dict, srcs: dict, cited: list[str]) -> bool:
    """Reply recommends upgrading but cites neither a release note nor a maintainer statement
    (patch releases such as 1.45.1 are not in the release notes, only in maintainer replies)."""
    txt = (out.get("user_reply") or "") + " " + str((out.get("next_step") or {}).get("text", "") if isinstance(out.get("next_step"), dict) else "")
    if not re.search(r"\bupgrad", txt, re.I):
        return False
    return not any(srcs.get(c, {}).get("type") == "release_note" or srcs.get(c, {}).get("maintainer")
                   for c in cited)


def score_case(case: dict, res: dict) -> dict:
    g, out = case["gold"], res.get("output") or {}
    labels = g["relevant_sources"]
    srcs = res.get("sources") or {}
    as_hit = lambda s: SimpleNamespace(type=s["type"], url=s["url"], source_id=s["source_id"])
    retrieved = [as_hit(s) for s in srcs.values()]
    cited = [as_hit(srcs[c]) for c in (res.get("check") or {}).get("cited", []) if c in srcs]
    found = lambda hits: sum(any(match(h, lab) for h in hits) for lab in labels)
    outcome = out.get("outcome")
    cited_labels = (res.get("check") or {}).get("cited", [])
    cited_ids = [srcs[c]["source_id"] for c in cited_labels if c in srcs]
    bad_basis, n_hyp = basis_mislabels(out, srcs)
    return {
        "case_id": case["case_id"], "bucket": case["bucket"], "gold_outcome": g["outcome"],
        "outcome": outcome,
        "outcome_exact": outcome == g["outcome"],
        "outcome_acceptable": outcome in (g.get("acceptable_outcomes") or [g["outcome"]]),
        "bucket_ok": outcome in BUCKET_OUTCOMES[case["bucket"]],
        "retrieval_recall": round(found(retrieved) / len(labels), 3) if labels else None,
        "cited_recall": round(found(cited) / len(labels), 3) if labels else None,
        "n_cited": len(cited),
        "invalid_citations": len((res.get("check") or {}).get("invalid_citations", [])),
        "contract_problems": (res.get("check") or {}).get("problems", []),
        "n_questions": len(out.get("questions") or []),
        "redundant_q": redundant_questions(case["visible"], out),
        "checklist_q": checklist_question(out),
        "basis_mislabelled": bad_basis, "n_hypotheses": n_hyp,
        "duplicate_citations": len(cited_ids) - len(set(cited_ids)),
        "unsupported_upgrade": unsupported_upgrade(out, srcs, cited_labels),
        "n_sources_shown": len(srcs),
        "confidence": out.get("confidence"),
        "llm_proposed": ((res.get("trace") or {}).get("decision") or {}).get("proposed"),
        "error": res.get("error"),
    }


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def aggregate(scores: list[dict], stats: list[dict]) -> dict:
    by_b = defaultdict(list)
    for s in scores:
        by_b[s["bucket"]].append(s)
    n = len(scores)
    st = lambda k: round(sum(x.get(k, 0) for x in stats), 6)
    return {
        "n_cases": n,
        "outcome_exact": _mean([s["outcome_exact"] for s in scores]),
        "outcome_acceptable": _mean([s.get("outcome_acceptable", s["outcome_exact"]) for s in scores]),
        "bucket_ok": _mean([s["bucket_ok"] for s in scores]),
        "per_bucket": {b: {"n": len(v), "bucket_ok": _mean([s["bucket_ok"] for s in v]),
                           "outcome_exact": _mean([s["outcome_exact"] for s in v]),
                           "outcome_acceptable": _mean([s.get("outcome_acceptable", s["outcome_exact"]) for s in v])}
                       for b, v in sorted(by_b.items())},
        "confusion": {b: dict(Counter(s["outcome"] or "none" for s in v)) for b, v in sorted(by_b.items())},
        "retrieval_recall": _mean([s["retrieval_recall"] for s in scores]),
        "cited_recall": _mean([s["cited_recall"] for s in scores]),
        "avg_cited": _mean([s["n_cited"] for s in scores]),
        "invalid_citation_cases": sum(s["invalid_citations"] > 0 for s in scores),
        "contract_problem_cases": sum(bool(s["contract_problems"]) for s in scores),
        "redundant_question_cases": sum(bool(s["redundant_q"]) for s in scores),
        "checklist_question_cases": sum(s["checklist_q"] for s in scores),
        "basis_mislabel_rate": round(sum(s["basis_mislabelled"] for s in scores) /
                                     max(sum(s["n_hypotheses"] for s in scores), 1), 3),
        "duplicate_citation_cases": sum(s["duplicate_citations"] > 0 for s in scores),
        "unsupported_upgrade_cases": sum(s["unsupported_upgrade"] for s in scores),
        "high_confidence_wrong": sum(s["confidence"] == "high" and not s["outcome_exact"] for s in scores),
        "avg_sources_shown": _mean([s["n_sources_shown"] for s in scores]),
        "avg_questions": _mean([s["n_questions"] for s in scores]),
        "errors": sum(bool(s["error"]) for s in scores),
        "llm_proposed_exact": (_mean([s["llm_proposed"] == s["gold_outcome"] for s in scores])
                               if any(s.get("llm_proposed") for s in scores) else None),
        "cost": {"llm_calls_live": st("live_calls"), "llm_calls_replayed": st("replayed_calls"),
                 "tokens_in": st("tokens_in"), "tokens_out": st("tokens_out"),
                 "usd_live": st("usd_live"), "usd_replayed_equiv": st("usd_replayed"),
                 "usd_per_case": round((st("usd_live") + st("usd_replayed")) / max(n, 1), 6),
                 "query_embeddings": st("query_embeddings"),
                 "avg_llm_latency_s": round(st("latency_s") / max(n, 1), 3),
                 "avg_wall_s": round(st("wall_s") / max(n, 1), 3),
                 "avg_steps": round(st("steps") / max(n, 1), 2)},
    }


# ------------------------------------------------------------------ run
def run(system_name: str, split: str, ids: list[str] | None = None, llm=None, system=None) -> dict:
    from ..llm.client import BudgetExceeded
    if llm is None:
        from ..llm import get_llm
        llm = get_llm()
    system = system or make_system(system_name, llm)
    cases = load_cases(split, ids)
    per_case, scores, stats, stopped = [], [], [], None
    for c in cases:
        public = {k: c[k] for k in PUBLIC_KEYS}          # gold never reaches the system
        llm.reset_stats()
        q0 = getattr(system.retr, "query_embeddings", 0)
        t0 = time.monotonic()
        try:
            res = system.run(public)
        except BudgetExceeded as e:                        # stop cleanly, keep what we have
            stopped = str(e)
            break
        except Exception as e:                             # e.g. ReplayMiss: record, continue
            res = {"output": {}, "sources": {}, "check": {}, "error": f"{type(e).__name__}: {e}",
                   "steps": 0}
        s = {**llm.stats, "query_embeddings": getattr(system.retr, "query_embeddings", 0) - q0,
             "wall_s": round(time.monotonic() - t0, 3), "steps": res.get("steps", 0)}
        sc = score_case(c, res)
        per_case.append({"case_id": c["case_id"], "bucket": c["bucket"], "score": sc, "stats": s, **res})
        scores.append(sc)
        stats.append(s)
    return {"system": system_name, "prompt_version": getattr(system, "prompt_version", None),
            "split": split, "built_at": utcnow(), "git_commit": _git_rev(),
            "model": getattr(llm, "chat_model", None), "cache_mode": getattr(llm, "cache_mode", None),
            "stopped_early": stopped, "metrics": aggregate(scores, stats), "cases": per_case}


def rescore(path) -> dict:
    """Re-score a saved run against the CURRENT gold (e.g. after the team review). No API calls."""
    res = read_json(path)
    gold = {c["case_id"]: c for c in load_cases(res["split"])}
    scores, stats = [], []
    for pc in res["cases"]:
        if pc["case_id"] not in gold:
            continue
        pc["score"] = score_case(gold[pc["case_id"]], pc)
        scores.append(pc["score"])
        stats.append(pc["stats"])
    res["metrics"] = aggregate(scores, stats)
    res["rescored_at"] = utcnow()
    return res


def print_metrics(res: dict) -> None:
    m = res["metrics"]
    print(f"{res['system']} on {res['split']} ({m['n_cases']} cases, model={res['model']}, "
          f"cache={res['cache_mode']})" + (f"  STOPPED: {res['stopped_early']}" if res["stopped_early"] else ""))
    print(f"  outcome exact {m['outcome_exact']}  |  acceptable {m.get('outcome_acceptable')}  |  right kind (bucket_ok) {m['bucket_ok']}")
    for b, v in m["per_bucket"].items():
        print(f"    {b:10s} n={v['n']:2d}  bucket_ok={v['bucket_ok']}  exact={v['outcome_exact']}  acceptable={v.get('outcome_acceptable')}  "
              f"-> {m['confusion'][b]}")
    print(f"  evidence: retrieval_recall {m['retrieval_recall']}  cited_recall {m['cited_recall']}  "
          f"avg_cited {m['avg_cited']}  invalid-citation cases {m['invalid_citation_cases']}")
    print(f"  sources shown avg {m['avg_sources_shown']}  duplicate-citation cases {m['duplicate_citation_cases']}"
          f"  basis mislabel rate {m['basis_mislabel_rate']}")
    print(f"  questions: avg {m['avg_questions']}  redundant cases {m['redundant_question_cases']}"
          f"  checklist cases {m['checklist_question_cases']}  | unsupported-upgrade cases "
          f"{m['unsupported_upgrade_cases']}  high-confidence-wrong {m['high_confidence_wrong']}")
    print(f"  contract problems {m['contract_problem_cases']}  errors {m['errors']}"
          + (f"  | LLM-proposed outcome exact {m['llm_proposed_exact']} (vs code decision {m['outcome_exact']})"
             if m.get("llm_proposed_exact") is not None else ""))
    print(f"  cost: {m['cost']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--system", default="baseline", choices=SYSTEMS)
    ap.add_argument("--rescore", action="store_true", help="re-score the saved run file, no API calls")
    ap.add_argument("--split", default="dev", choices=["dev", "test"])
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--confirm-test", action="store_true", help="required for --split test (run once!)")
    a = ap.parse_args(argv)
    if a.split == "test" and not a.confirm_test and not a.rescore:
        raise SystemExit("test split is for the final report only - pass --confirm-test")
    suffix = "" if not a.cases else "_subset"
    path = RUNS / f"p1_{a.system}_{a.split}{suffix}.json"
    res = rescore(path) if a.rescore else run(a.system, a.split, a.cases)
    write_json(path, res)
    if not a.rescore:     # keep every iteration for the report (baseline -> final-v1 -> v2 -> ...)
        write_json(RUNS / "history" / f"p1_{a.system}_{res['prompt_version']}_{a.split}{suffix}.json", res)
    print_metrics(res)
    return 2 if res["stopped_early"] else 0


if __name__ == "__main__":
    sys.exit(main())
