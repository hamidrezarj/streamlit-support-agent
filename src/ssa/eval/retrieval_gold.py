"""Recall@k on the hand-labelled evaluation cases (+ a hard leakage check).

Run:  python -m ssa.eval.retrieval_gold --split dev [--modes bm25 dense hybrid]

For every case: query = visible title + body, filters = the case's own
(before=as_of, exclude all eval issues). A hit counts if its source_id is a
labelled source, or (for 'page:/path' labels) if it is any section of that page.
Modes: flat bm25/dense/hybrid top-k, or grouped-<mode> = per-kind lists
(5 issues + 4 docs + 3 release notes); compare both at k=12.
Leakage check (hard): no returned chunk may belong to an eval issue or have
available_at >= as_of. Use --split test only for the final report.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from urllib.parse import urlparse

from ..data import config as C
from ..data.io_utils import read_json, utcnow, write_json
from ..index.search import Filters, Retriever


def match(hit, label: str) -> bool:
    if label.startswith("page:"):
        return hit.type == "doc" and urlparse(hit.url).path.rstrip("/") == label[5:].rstrip("/")
    return hit.source_id == label


def run(split: str, modes: list[str], ks=(5, 10, 12)) -> dict:
    cases = [read_json(p) for p in sorted((C.DATA / "eval" / "cases" / split).glob("*.json"))]
    retr = Retriever(mode="bm25")
    res = {"split": split, "built_at": utcnow(), "metrics": {}, "leaks": [], "per_case": []}
    for mode in modes:
        base = mode.replace("grouped-", "")
        if base != "bm25" and retr.M is None:
            print(f"skip {mode}: embeddings missing/out of date (run python -m ssa.index.embed)")
            continue
        agg = defaultdict(list)
        for c in cases:
            v, f = c["visible"], c["filters"]
            filt = Filters(before=f["before"], exclude_issues=set(f["exclude_issues"]))
            q = v["title"] + "\n" + v["body"][:1500]
            if mode.startswith("grouped-"):   # 5 issues + 4 docs + 3 release notes = 12
                g = retr.search_grouped(q, filt, mode=base)
                hits = g["issues"] + g["docs"] + g["release_notes"]
            else:
                hits = retr.search(q, filt, k=max(ks), mode=base)
            for h in hits:   # hard leakage check
                num = h.source_id.split(":")[1] if h.source_id.startswith("issue:") else None
                if (num and int(num) in filt.exclude_issues) or (h.available_at and h.available_at >= f["before"]):
                    res["leaks"].append({"case": c["case_id"], "mode": mode, "source": h.source_id})
            labels = c["gold"]["relevant_sources"]
            if not labels:
                continue
            found_rank = {}
            for i, h in enumerate(hits, 1):
                for lab in labels:
                    if lab not in found_rank and match(h, lab):
                        found_rank[lab] = i
            for k in ks:
                agg[f"recall@{k}"].append(sum(r <= k for r in found_rank.values()) / len(labels))
                agg[f"hit@{k}"].append(1.0 if any(r <= k for r in found_rank.values()) else 0.0)
            first = min(found_rank.values(), default=None)
            agg["mrr"].append(1 / first if first else 0.0)
            res["per_case"].append({"mode": mode, "case": c["case_id"], "bucket": c["bucket"],
                                    "found": found_rank, "missed": [l for l in labels if l not in found_rank]})
        res["metrics"][mode] = {m: round(sum(v) / len(v), 3) for m, v in agg.items()}
        res["metrics"][mode]["n_labelled_cases"] = len(agg["mrr"])
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", default="dev", choices=["dev", "test"])
    ap.add_argument("--modes", nargs="*",
                    default=["bm25", "dense", "hybrid", "grouped-bm25", "grouped-dense", "grouped-hybrid"])
    a = ap.parse_args(argv)
    res = run(a.split, a.modes)
    write_json(C.DATA / "eval" / f"retrieval_gold_{a.split}.json", res)
    for m, v in res["metrics"].items():
        print(f"{a.split} {m:7s} {v}")
    print("leaks:", len(res["leaks"]))
    return 1 if res["leaks"] else 0


if __name__ == "__main__":
    sys.exit(main())
