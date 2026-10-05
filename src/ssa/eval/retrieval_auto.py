"""Retrieval sanity eval on AUTO-labelled queries (dev signal, not the final eval).

Run:  python -m ssa.eval.retrieval_auto [--modes bm25 dense hybrid] [--k 5 10 20]

Three label sources that come for free from the snapshot:
  fix   query = issue text, relevant = release-note bullets that link the issue
        (searched within release notes only - how the agent will use them)
  dup   query = issue text, relevant = other issues of the same duplicate family
  doc   query = issue text, relevant = docs pages a maintainer linked in the thread
        (scored per page, not per section)
The query issue itself (and all its comments) is always excluded.
Caveats for the report: labels are incomplete (other sources may also be
relevant) and `fix`/`doc` labels come from information later than the issue,
so this measures "can the index find it", not historical answer quality.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from urllib.parse import urlparse

from ..data import config as C
from ..data.io_utils import read_jsonl, utcnow, write_json
from ..index.search import Filters, Retriever


def query_text(r: dict, max_chars: int = 1500) -> str:
    s = r["sections"]
    parts = [r["title"]] + [s.get(k, "") for k in ("summary", "current behavior", "steps to reproduce",
                                                   "_preamble", "problem", "description")]
    q = "\n".join(p for p in parts if p).strip()
    return (q if len(q) > len(r["title"]) + 20 else r["title"] + "\n" + r["body"])[:max_chars]


def build_tasks(retr: Retriever) -> list[dict]:
    issues = read_jsonl(C.PROCESSED / "issues.jsonl")
    rn_by_issue = defaultdict(set)
    doc_src_by_path = defaultdict(set)
    for i, row in enumerate(retr.rows):
        if row["type"] == "release_note":
            for n in retr.meta[i].get("linked_issues", []):
                rn_by_issue[n].add(row["source_id"])
        if row["type"] in ("doc", "config_option"):
            doc_src_by_path[urlparse(row["url"]).path.rstrip("/")].add(row["source_id"])
    fams = defaultdict(list)
    for r in issues:
        fams[r["family_id"]].append(r["number"])
    tasks = []
    for r in issues:
        q = query_text(r)
        if rn_by_issue.get(r["number"]):
            tasks.append({"kind": "fix", "issue": r["number"], "query": q,
                          "relevant": sorted(rn_by_issue[r["number"]]), "types": {"release_note"}})
        others = [n for n in fams[r["family_id"]] if n != r["number"]]
        if others:
            tasks.append({"kind": "dup", "issue": r["number"], "query": q,
                          "relevant": [f"issue:{n}" for n in others], "types": {"issue", "comment"}})
        paths = {p.rstrip("/") for c in r["comments"] if c["is_maintainer"]
                 for p in re.findall(r"docs\.streamlit\.io(/[^\s)#\]>\"'?]*)", c["text"])}
        rel = sorted(p for p in paths if p in doc_src_by_path)   # page-level labels
        if rel:
            tasks.append({"kind": "doc", "issue": r["number"], "query": q,
                          "relevant": rel, "types": {"doc", "config_option"}})
    return tasks


def run(modes: list[str], ks: list[int]) -> dict:
    retr = Retriever(mode="bm25")
    tasks = build_tasks(retr)
    kmax = max(ks)
    results: dict = {"built_at": utcnow(), "n_tasks": {}, "metrics": {}, "per_task": []}
    for kind in ("fix", "dup", "doc"):
        results["n_tasks"][kind] = sum(t["kind"] == kind for t in tasks)
    for mode in modes:
        if mode != "bm25" and retr.M is None:
            print(f"skip {mode}: no embeddings yet")
            continue
        agg = defaultdict(lambda: defaultdict(list))
        for t in tasks:
            f = Filters(exclude_issues={t["issue"]}, types=t["types"], drop_linked_release_notes=False)
            hits = retr.search(t["query"], f, k=kmax * (3 if t["kind"] == "doc" else 1),
                               mode=mode, max_per_source=1)
            if t["kind"] == "doc":   # compare at page level, deduplicated
                got = list(dict.fromkeys(urlparse(h.url).path.rstrip("/") for h in hits))
            else:
                got = [h.source_id for h in hits]
            rel = set(t["relevant"])
            first = next((i + 1 for i, s in enumerate(got) if s in rel), None)
            for k in ks:
                agg[t["kind"]][f"recall@{k}"].append(len(rel & set(got[:k])) / len(rel))
                agg[t["kind"]][f"hit@{k}"].append(1.0 if rel & set(got[:k]) else 0.0)
            agg[t["kind"]]["mrr"].append(1 / first if first else 0.0)
            results["per_task"].append({"mode": mode, "kind": t["kind"], "issue": t["issue"],
                                        "first_relevant_rank": first})
        results["metrics"][mode] = {kind: {m: round(sum(v) / len(v), 3) for m, v in ms.items()}
                                    for kind, ms in agg.items()}
    results["query_embeddings_paid"] = retr.query_embeddings
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modes", nargs="*", default=["bm25", "dense", "hybrid"])
    ap.add_argument("--k", nargs="*", type=int, default=[5, 10, 20])
    a = ap.parse_args(argv)
    res = run(a.modes, a.k)
    out = C.DATA / "eval" / "retrieval_auto.json"
    write_json(out, res)
    print("tasks:", res["n_tasks"])
    for mode, kinds in res["metrics"].items():
        for kind, m in kinds.items():
            print(f"{mode:7s} {kind:4s} " + "  ".join(f"{k}={v:.3f}" for k, v in m.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
